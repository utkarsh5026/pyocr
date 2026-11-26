"""Model catalog, model listing and credentials saved from the UI."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import anthropic
import httpx2
import openai
import pytest
from fastapi.testclient import TestClient

from pyocr_toolkit.api import create_app
from pyocr_toolkit.config import Settings
from pyocr_toolkit.llm import PROVIDER_CLASSES, catalog
from pyocr_toolkit.llm.anthropic_provider import AnthropicProvider
from pyocr_toolkit.llm.openai_provider import OpenAIProvider

PROVIDER_IDS = {cls.id for cls in PROVIDER_CLASSES}


# ------------------------------------------------------------------ catalog
def test_catalog_entries_are_consistent():
    ids = [m.id for m in catalog.MODELS]
    assert len(ids) == len(set(ids)), "duplicate catalog ids"
    for m in catalog.MODELS:
        assert m.rating in catalog.RATINGS and m.rating != "unrated", m.id
        assert set(m.providers) <= PROVIDER_IDS, m.id
        assert m.status in {"current", "previous", "preview", "deprecated"}, m.id
        assert m.evidence in {"benchmark", "vendor"}, m.id
        assert m.note, m.id


@pytest.mark.parametrize("name, provider, expected", [
    ("gpt-6.2-sol", "openai", "gpt-6.2-sol"),
    ("GPT-6.2-Sol", "openai", "gpt-6.2-sol"),
    ("openai/gpt-6.2-sol", "openai_compatible", "gpt-6.2-sol"),  # OpenRouter slug
    ("prod-gpt-6.2-sol", "azure_openai", "gpt-6.2-sol"),  # Azure deployment name
    ("qwen3.8:27b-q4_K_M", "openai_compatible", "qwen3.8-27b"),  # Ollama tag
    ("gpt-5.4-nano", "openai", "gpt-5.4-nano"),  # longest match, not gpt-5.4
    ("claude-opus-5-5", "anthropic", "claude-opus-5-5"),  # not claude-opus-5
    ("claude-opus-5", "anthropic", "claude-opus-5"),
    ("gemini-2.5-flash-lite", "gemini", "gemini-2.5-flash-lite"),
])
def test_lookup_matches(name, provider, expected):
    r = catalog.lookup(name, provider)
    assert r["match"] == expected
    assert r["exact"] == (name.lower() == expected)


def test_lookup_custom_names():
    custom = catalog.lookup("my-finetuned-reader", "openai_compatible")
    assert custom["match"] is None and custom["rating"] == "unrated"
    assert catalog.lookup("llava-next-custom", "openai_compatible")["note"].startswith("Not in the catalog, but")
    assert catalog.lookup("text-embedding-4", "openai")["rating"] == "unsuitable"
    assert catalog.lookup("gpt-oss-120b", "openai_compatible")["rating"] == "unsuitable"
    # "gpt-6.2-solar" is a different name, not gpt-6.2-sol
    assert catalog.lookup("gpt-6.2-solar", "openai")["match"] is None


def test_search_and_recommended():
    hits = catalog.search("gemini flash", provider_id="gemini")
    assert "gemini-3.8-flash" in [m.id for m in hits] and "gemini-4-neon" not in [m.id for m in hits]
    assert all(m.rating == "excellent" for m in catalog.search(rating="excellent"))
    assert not any(m.status == "deprecated" for m in catalog.search(include_deprecated=False))
    assert catalog.search("handwriting")  # notes are searchable
    # suggestions are current models that suit the job, best first
    rec = catalog.recommended("openai")
    assert rec and rec[0] == "gpt-6.2-sol" and "gpt-5" not in rec
    opts = catalog.model_options("azure_openai", ["my-deployment"])
    assert opts[0]["name"] == "my-deployment" and opts[0]["match"] is None
    assert all(o["rating"] != "unsuitable" for o in opts)


# ------------------------------------------------------------------ API
@pytest.fixture
def app_client(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, _env_file=None))) as c:
        c.data_dir = tmp_path
        yield c


def test_models_api(app_client):
    r = app_client.get("/api/models", params={"q": "claude", "provider": "anthropic"}).json()
    assert r["usd_inr_rate"] > 0  # the Models page shows prices in rupees too
    assert r["models"] and all(m["vendor"] == "Anthropic" for m in r["models"])
    assert r["sources"] and r["ratings"]["excellent"]
    v = app_client.get("/api/models/lookup", params={"name": "openai/gpt-6.2-sol"}).json()
    assert v["match"] == "gpt-6.2-sol" and v["rating"] == "excellent"
    cfg = app_client.get("/api/config").json()
    openai_cfg = next(p for p in cfg["providers"] if p["id"] == "openai")
    assert openai_cfg["model_options"][0]["rating_label"] and openai_cfg["can_list_models"]

    m = app_client.get("/api/providers/openai/models").json()
    assert m["catalog"] and m["live"] is None and "OPENAI_API_KEY" in m["live_error"]
    m = app_client.get("/api/providers/azure_openai/models").json()
    assert not m["live_supported"] and m["live_error"] is None
    assert app_client.get("/api/providers/nope/models").status_code == 404
    assert "Excellent for OCR" in app_client.get("/models").text


def test_openai_list_models_mocked():
    import httpx

    def handler(request):
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"object": "list", "data": [
            {"id": "gpt-6.2-sol", "object": "model", "created": 0, "owned_by": "openai"},
            {"id": "text-embedding-3-large", "object": "model", "created": 0, "owned_by": "openai"},
        ]})

    provider = OpenAIProvider(Settings(_env_file=None, openai_api_key="test"))
    provider._client = openai.AsyncOpenAI(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert asyncio.run(provider.list_models()) == ["gpt-6.2-sol", "text-embedding-3-large"]


def test_claude_list_models_mocked():
    def handler(request):
        assert request.url.path == "/v1/models"
        return httpx2.Response(200, json={"data": [
            {"id": "claude-opus-5-5", "type": "model", "display_name": "Claude Opus 5.5",
             "created_at": "2026-09-22T00:00:00Z"}], "has_more": False, "first_id": "a", "last_id": "a"})

    provider = AnthropicProvider(Settings(_env_file=None, anthropic_api_key="test"))
    provider._client = anthropic.AsyncAnthropic(
        api_key="test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    assert asyncio.run(provider.list_models()) == ["claude-opus-5-5"]


# ------------------------------------------------------------------ credentials
SECRET = "sk-ui-secret-987"


def test_credentials_saved_from_ui(app_client):
    c = app_client
    assert not next(p for p in c.get("/api/config").json()["providers"] if p["id"] == "openai")["configured"]
    r = c.post("/api/providers/openai/credentials",
               json={"values": {"OPENAI_API_KEY": SECRET, "OPENAI_MODEL": "gpt-6.2-sol", "OPENAI_BASE_URL": ""}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"]["configured"] and body["provider"]["default_model"] == "gpt-6.2-sol"
    key = next(s for s in body["settings"] if s["name"] == "OPENAI_API_KEY")
    assert key["saved"] and key["secret"] and "value" not in key
    # the secret is stored on disk but never sent to the browser
    path = c.data_dir / "credentials.json"
    assert json.loads(path.read_text())["OPENAI_API_KEY"] == SECRET
    if sys.platform != "win32":
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    for url in ["/providers", "/api/config", "/api/providers/openai/credentials", "/models"]:
        assert SECRET not in c.get(url).text, url
    assert "saved here" in c.get("/providers").text

    # clearing falls back to the environment (here: unset)
    r = c.post("/api/providers/openai/credentials", json={"clear": ["OPENAI_API_KEY"]}).json()
    assert not r["provider"]["configured"] and "OPENAI_API_KEY" not in json.loads(path.read_text())


def test_credentials_persist_and_override_env(tmp_path):
    (tmp_path / "credentials.json").write_text(json.dumps({"GEMINI_API_KEY": "saved", "BOGUS": "x"}))
    app = create_app(Settings(data_dir=tmp_path, _env_file=None, gemini_api_key="from-env"))
    assert app.state.settings.gemini_api_key == "saved"
    with TestClient(app) as c:
        c.post("/api/providers/gemini/credentials", json={"clear": ["GEMINI_API_KEY"]})
    assert app.state.settings.gemini_api_key == "from-env"


def test_credentials_rejects_bad_requests(app_client):
    c = app_client
    url = "/api/providers/openai/credentials"
    # a setting that belongs to another provider
    assert c.post(url, json={"values": {"GEMINI_API_KEY": "x"}}).status_code == 400
    assert c.post(url, json={"values": {"OPENAI_API_KEY": "a\nb"}}).status_code == 400
    assert c.post("/api/providers/nope/credentials", json={"values": {}}).status_code == 404
    # cross-site writes are refused (no auth on localhost, so this is the CSRF guard)
    r = c.post(url, json={"values": {"OPENAI_BASE_URL": "https://evil.example"}},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.post(url, content=json.dumps({"values": {"OPENAI_API_KEY": "x"}}), headers={"Content-Type": "text/plain"})
    assert r.status_code in (415, 422)
    assert not (c.data_dir / "credentials.json").exists()
