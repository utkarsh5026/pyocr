"""Exercise the real SDK clients against mocked HTTP endpoints.

This checks the request we send (model, image block, effort, fallbacks) and
that we parse the provider's response correctly - without network or keys.
"""

from __future__ import annotations

import asyncio
import json

import anthropic
import httpx2
import openai
import pytest

from pyocr_toolkit.config import Settings
from pyocr_toolkit.llm import ProviderError, TranscriptionRequest
from pyocr_toolkit.llm.anthropic_provider import AnthropicProvider
from pyocr_toolkit.llm.openai_provider import AzureOpenAIProvider, OpenAIProvider

REQ = TranscriptionRequest(image=b"\x89PNGfake", mime_type="image/png", system_prompt="SYS",
                           user_prompt="Transcribe this page.")


def _sse(events: list[tuple[str, dict]]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def _claude_stream(text: str, model: str, stop_reason: str = "end_turn") -> bytes:
    return _sse([
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1500, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                           "usage": {"output_tokens": 42}}),
        ("message_stop", {"type": "message_stop"}),
    ])


def _claude_provider(model: str, handler, settings_kw=None):
    settings = Settings(_env_file=None, anthropic_api_key="test", anthropic_model=model, **(settings_kw or {}))
    provider = AnthropicProvider(settings)
    provider._client = anthropic.AsyncAnthropic(
        api_key="test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    return provider


def test_claude_request_and_response():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        return httpx2.Response(200, content=_claude_stream("Hello page", "claude-opus-5-5"),
                               headers={"content-type": "text/event-stream"})

    result = asyncio.run(_claude_provider("claude-opus-5-5", handler).transcribe(REQ))
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["system"] == "SYS"
    image, text = body["messages"][0]["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
    assert text == {"type": "text", "text": "Transcribe this page."}
    assert body["output_config"] == {"effort": "medium"}
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in seen["beta"]
    assert body["stream"] is True
    assert result.text == "Hello page"
    assert (result.input_tokens, result.output_tokens) == (1500, 42)


def test_claude_older_model_gets_no_effort_or_fallback():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, content=_claude_stream("x", "claude-haiku-4-5"),
                               headers={"content-type": "text/event-stream"})

    asyncio.run(_claude_provider("claude-haiku-4-5", handler).transcribe(REQ))
    assert "output_config" not in seen["body"] and "fallbacks" not in seen["body"]


def test_claude_refusal_and_truncation():
    def refusal(_request):
        return httpx2.Response(200, content=_claude_stream("", "claude-opus-5-5", "refusal"),
                               headers={"content-type": "text/event-stream"})

    with pytest.raises(ProviderError, match="declined"):
        asyncio.run(_claude_provider("claude-opus-5-5", refusal).transcribe(REQ))

    def truncated(_request):
        return httpx2.Response(200, content=_claude_stream("partial", "claude-opus-5-5", "max_tokens"),
                               headers={"content-type": "text/event-stream"})

    res = asyncio.run(_claude_provider("claude-opus-5-5", truncated).transcribe(REQ))
    assert res.warnings and "truncated" in res.warnings[0]


def test_claude_auth_error():
    def handler(_request):
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})

    with pytest.raises(ProviderError, match="authentication"):
        asyncio.run(_claude_provider("claude-opus-5-5", handler).transcribe(REQ))


def _chat_completion(text: str, model: str, finish: str = "stop") -> dict:
    return {
        "id": "c1", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": text, "refusal": None}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 30, "total_tokens": 930},
    }


def _openai_http(handler):
    import httpx  # the openai SDK is built on httpx
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_openai_request_and_response():
    import httpx
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_chat_completion("Page text", "gpt-5"))

    settings = Settings(_env_file=None, openai_api_key="test", openai_model="gpt-5")
    provider = OpenAIProvider(settings)
    provider._client = openai.AsyncOpenAI(api_key="test", http_client=_openai_http(handler))
    result = asyncio.run(provider.transcribe(REQ))
    body = seen["body"]
    assert seen["url"].endswith("/chat/completions")
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    image_part = body["messages"][1]["content"][1]
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
    assert body["max_completion_tokens"] == 16000
    assert result.text == "Page text" and result.input_tokens == 900


def test_azure_openai_uses_deployment_url():
    import httpx
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_chat_completion("ok", "gpt-4o", finish="length"))

    settings = Settings(_env_file=None, azure_openai_api_key="k", azure_openai_endpoint="https://x.openai.azure.com",
                        azure_openai_deployment="my-gpt4o")
    provider = AzureOpenAIProvider(settings)
    provider._client = openai.AsyncAzureOpenAI(
        api_key="k", azure_endpoint="https://x.openai.azure.com", api_version="2024-10-21",
        http_client=_openai_http(handler))
    result = asyncio.run(provider.transcribe(REQ))
    assert "/openai/deployments/my-gpt4o/chat/completions" in seen["url"]
    assert "api-version=2024-10-21" in seen["url"]
    assert result.warnings  # finish_reason=length -> truncated warning


# ------------------------------------------------------------------ structured extraction
SCHEMA = {"type": "object", "properties": {"total": {"type": ["string", "null"]}}, "required": ["total"],
          "additionalProperties": False}
EXTRACT_REQ = TranscriptionRequest(image=b"\x89PNGfake", mime_type="image/png", system_prompt="SYS",
                                   user_prompt="Extract the data from this page.", output_schema=SCHEMA)


def _claude_capture(model: str):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, content=_claude_stream('{"total": "$5"}', model),
                               headers={"content-type": "text/event-stream"})

    return seen, _claude_provider(model, handler)


def test_claude_structured_output_uses_output_config_format():
    seen, provider = _claude_capture("claude-opus-5-5")
    result = asyncio.run(provider.transcribe(EXTRACT_REQ))
    body = seen["body"]
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "medium"}
    assert body["system"] == "SYS"  # the schema is enforced, not repeated in the prompt
    assert result.text == '{"total": "$5"}' and result.warnings == []


def test_claude_without_structured_outputs_gets_schema_in_prompt():
    seen, provider = _claude_capture("claude-opus-4-6")
    result = asyncio.run(provider.transcribe(EXTRACT_REQ))
    body = seen["body"]
    assert "format" not in body.get("output_config", {})
    assert body["system"].startswith("SYS") and '"total"' in body["system"]
    assert result.warnings and "prompt" in result.warnings[0]


def test_claude_structured_output_model_support():
    from pyocr_toolkit.llm.anthropic_provider import _supports_structured_outputs as ok

    assert all(ok(m) for m in ["claude-fable-5-2", "claude-opus-5-5", "claude-opus-4-8", "claude-sonnet-5-5",
                               "claude-sonnet-5", "claude-haiku-5-5", "claude-haiku-4-5", "claude-opus-4-1"])
    assert not any(ok(m) for m in ["claude-opus-4-6", "claude-opus-4-7", "claude-sonnet-4-6", "my-deployment"])


def test_openai_structured_output_uses_strict_response_format():
    import httpx
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_chat_completion('{"total": null}', "gpt-5"))

    provider = OpenAIProvider(Settings(_env_file=None, openai_api_key="test", openai_model="gpt-5"))
    provider._client = openai.AsyncOpenAI(api_key="test", http_client=_openai_http(handler))
    asyncio.run(provider.transcribe(EXTRACT_REQ))
    assert seen["body"]["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "page_data", "schema": SCHEMA, "strict": True}}
    assert seen["body"]["messages"][0]["content"] == "SYS"

    asyncio.run(provider.transcribe(REQ))  # plain transcription: no response_format
    assert "response_format" not in seen["body"]


def test_openai_compatible_also_spells_schema_out():
    import httpx

    from pyocr_toolkit.llm.openai_provider import OpenAICompatibleProvider
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_chat_completion("{}", "qwen"))

    provider = OpenAICompatibleProvider(Settings(_env_file=None, openai_compatible_base_url="http://localhost:1/v1",
                                                 openai_compatible_model="qwen"))
    provider._client = openai.AsyncOpenAI(api_key="x", base_url="http://localhost:1/v1", http_client=_openai_http(handler))
    asyncio.run(provider.transcribe(EXTRACT_REQ))
    assert seen["body"]["response_format"]["json_schema"]["schema"] == SCHEMA
    assert '"total"' in seen["body"]["messages"][0]["content"]  # servers that ignore response_format


def test_gemini_structured_output_config():
    from types import SimpleNamespace

    from pyocr_toolkit.llm.gemini_provider import GeminiProvider
    seen = {}

    async def generate_content(model, contents, config):
        seen["config"] = config
        return SimpleNamespace(candidates=[], text='{"total": "$5"}', usage_metadata=None)

    provider = GeminiProvider(Settings(_env_file=None, gemini_api_key="k", gemini_model="gemini-x"))
    provider._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    result = asyncio.run(provider.transcribe(EXTRACT_REQ))
    assert seen["config"].response_mime_type == "application/json"
    assert seen["config"].response_json_schema == SCHEMA
    assert result.text == '{"total": "$5"}'
    asyncio.run(provider.transcribe(REQ))
    assert seen["config"].response_json_schema is None


def test_text_only_requests_send_no_image():
    import httpx

    text_only = TranscriptionRequest(image=b"", mime_type="", system_prompt="SYS", user_prompt="Draft fields.")
    seen, provider = _claude_capture("claude-opus-5-5")
    asyncio.run(provider.transcribe(text_only))
    assert seen["body"]["messages"][0]["content"] == [{"type": "text", "text": "Draft fields."}]

    def handler(request: httpx.Request):
        seen["openai"] = json.loads(request.content)
        return httpx.Response(200, json=_chat_completion("{}", "gpt-5"))

    openai_provider = OpenAIProvider(Settings(_env_file=None, openai_api_key="test", openai_model="gpt-5"))
    openai_provider._client = openai.AsyncOpenAI(api_key="test", http_client=_openai_http(handler))
    asyncio.run(openai_provider.transcribe(text_only))
    assert seen["openai"]["messages"][1]["content"] == [{"type": "text", "text": "Draft fields."}]
