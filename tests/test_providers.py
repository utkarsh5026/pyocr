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

from pyocr.config import Settings
from pyocr.llm import ProviderError, TranscriptionRequest
from pyocr.llm.anthropic_provider import AnthropicProvider
from pyocr.llm.openai_provider import AzureOpenAIProvider, OpenAIProvider

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
