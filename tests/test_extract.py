"""Structured extraction: schema normalizing/validation, the cache key, and the API end to end."""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from pyocr_toolkit import extract
from pyocr_toolkit.api import create_app
from pyocr_toolkit.config import Settings
from pyocr_toolkit.documents import OpenDocument
from pyocr_toolkit.llm import EXTRACT_USER_PROMPT, USER_PROMPT
from pyocr_toolkit.llm.base import Provider, TranscriptionResult

INVOICE_REPLY = {
    "vendor_name": "Acme Ltd", "vendor_address": None, "vendor_tax_id": None, "invoice_number": "2024-117",
    "invoice_date": None, "due_date": None, "bill_to": None, "currency": "$",
    "line_items": [{"description": "Fox jumping", "quantity": "1", "unit_price": "$1,250.00", "amount": "$1,250.00"}],
    "subtotal": None, "tax": None, "total": "$1,250.00",
}


# ------------------------------------------------------------------ schema handling
def test_presets_normalize_to_strict_self_contained_schemas():
    for preset in extract.PRESETS.values():
        schema = preset.schema
        text = json.dumps(schema)
        assert "$ref" not in text and "$defs" not in text and '"title"' not in text and '"default"' not in text

        def check(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node["additionalProperties"] is False
                    assert node["required"] == list(node["properties"])
                for v in node.values():
                    check(v)
            elif isinstance(node, list):
                for v in node:
                    check(v)

        check(schema)
    # display order = the model's field order
    assert list(extract.PRESETS["invoice"].schema["properties"])[:2] == ["vendor_name", "vendor_address"]


def test_custom_schema_optional_fields_become_nullable():
    schema = extract.normalize({
        "type": "object", "title": "Thing",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer", "minimum": 0},
                       "kind": {"enum": ["a", "b"]}, "tags": {"type": "array"}},
        "required": ["name"],
    })
    props = schema["properties"]
    assert props["name"] == {"type": "string"}  # required: not nullable
    assert props["age"]["type"] == ["integer", "null"] and props["age"]["minimum"] == 0
    assert props["kind"] == {"anyOf": [{"enum": ["a", "b"]}, {"type": "null"}]}
    assert props["tags"]["items"] == {"type": "string"}
    assert schema["required"] == ["name", "age", "kind", "tags"]
    # constraints strict modes reject are stripped before sending, kept for local validation
    assert "minimum" not in extract.provider_schema(schema)["properties"]["age"]
    assert extract.validate({"name": "x", "age": -1, "kind": None, "tags": []}, schema) == [
        {"path": "age", "message": "below the minimum 0"}]


@pytest.mark.parametrize("bad, message", [
    ([1, 2], "must be a JSON object"),
    ({"type": "string"}, "root must be an object"),
    ({"type": "object", "properties": {"n": {"$ref": "#/$defs/N"}}, "$defs": {"N": {"type": "object", "properties": {
        "child": {"$ref": "#/$defs/N"}}}}}, "recursive"),
    ({"type": "object", "properties": {"x": {"type": "text"}}}, "unknown type 'text'"),
    ({"type": "object", "properties": {"x": {"$ref": "https://example.com/s.json"}}}, "can't resolve"),
])
def test_unusable_schemas_say_what_to_fix(bad, message):
    with pytest.raises(extract.SchemaError, match=message):
        extract.normalize(bad)


def test_resolve_preset_json_text_and_errors():
    schema, label, preset = extract.resolve("receipt")
    assert (label, preset) == ("Receipt", "receipt") and "merchant_name" in schema["properties"]
    _, label, preset = extract.resolve('{"title": "Ticket", "type": "object", "properties": {"seat": {"type": "string"}}}')
    assert (label, preset) == ("Custom: Ticket", None)
    with pytest.raises(extract.SchemaError, match="needs a schema"):
        extract.resolve("")
    with pytest.raises(extract.SchemaError, match="isn't valid JSON"):
        extract.resolve("{nope")


def test_parse_reply_tolerates_fences_and_chatter():
    assert extract.parse_reply('{"a": 1}') == ({"a": 1}, None)
    assert extract.parse_reply('```json\n{"a": 1}\n```') == ({"a": 1}, None)
    assert extract.parse_reply('Here you go:\n{"a": 1}\nHope that helps') == ({"a": 1}, None)
    assert extract.parse_reply("no json here")[1] == "the reply contains no JSON object"
    assert "isn't valid JSON" in extract.parse_reply('{"a": }')[1]


def test_validate_reports_paths():
    schema = extract.PRESETS["invoice"].schema
    assert extract.validate(INVOICE_REPLY, schema) == []
    bad = {**INVOICE_REPLY, "total": 1250, "extra": "x", "line_items": [{"description": "d"}]}
    del bad["tax"]
    errors = {e["path"]: e["message"] for e in extract.validate(bad, schema)}
    assert errors["total"] == "expected string or null, got integer"
    assert errors["extra"] == "not in the schema"
    assert errors["tax"] == "missing"
    assert errors["line_items[0].amount"] == "missing"
    assert extract.count_fields(INVOICE_REPLY, schema) == (5, 12)


def test_cache_key_unchanged_without_schema():
    """Plain transcriptions must keep their pre-extraction keys (no orphaned caches)."""
    digest = hashlib.sha256(b"p\0m\0prompt").hexdigest()[:16]
    assert OpenDocument.llm_cache_key("p", "m", "prompt") == f"p__m__{digest}"
    assert OpenDocument.llm_cache_key("p", "m", "prompt", schema="{}") != f"p__m__{digest}"
    assert (OpenDocument.llm_cache_key("p", "m", "prompt", schema='{"a":1}')
            != OpenDocument.llm_cache_key("p", "m", "prompt", schema='{"a":2}'))


# ------------------------------------------------------------------ API end to end
class JSONProvider(Provider):
    """Answers with ``reply`` when asked for structured output, plain text otherwise."""

    id = "fakejson"
    label = "Fake JSON"

    def __init__(self):
        self.calls = []
        self.reply = json.dumps(INVOICE_REPLY)

    @property
    def default_model(self):
        return "fake-1"

    def missing_settings(self):
        return []

    async def _transcribe(self, req, model):
        self.calls.append(req)
        text = self.reply if req.output_schema else "plain transcription"
        return TranscriptionResult(text=text, provider=self.id, model=model, latency_ms=0,
                                   input_tokens=10, output_tokens=20)


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, _env_file=None))
    fake = JSONProvider()
    app.state.providers[fake.id] = fake
    with TestClient(app) as c:
        c.fake = fake
        yield c


@pytest.fixture
def doc_id(client, sample_pdf):
    r = client.post("/api/documents", files={"file": ("sample.pdf", sample_pdf, "application/pdf")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _run(client, doc_id, page=0, **body):
    return client.post(f"/api/documents/{doc_id}/pages/{page}/llm",
                       json={"provider": "fakejson", "prompt_mode": "extract", **body})


def test_extract_preset_end_to_end(client, doc_id):
    r = _run(client, doc_id, extract_schema="invoice")
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["data"] == INVOICE_REPLY and res["data_errors"] == []
    assert (res["schema_label"], res["schema_preset"]) == ("Invoice", "invoice")
    assert (res["fields_filled"], res["fields_total"]) == (5, 12)
    assert json.loads(res["text"]) == INVOICE_REPLY  # pretty JSON, so text views/search/export still work
    req = client.fake.calls[-1]
    assert req.user_prompt == EXTRACT_USER_PROMPT
    assert req.output_schema["properties"]["total"]["anyOf"] == [{"type": "string"}, {"type": "null"}]

    assert _run(client, doc_id, extract_schema="invoice").json()["cached"] is True
    other = _run(client, doc_id, extract_schema="receipt").json()
    assert other["key"] != res["key"] and other["cached"] is False

    plain = client.post(f"/api/documents/{doc_id}/pages/0/llm", json={"provider": "fakejson"}).json()
    assert plain["text"] == "plain transcription" and "data" not in plain
    assert client.fake.calls[-1].user_prompt == USER_PROMPT and client.fake.calls[-1].output_schema is None

    sources = client.get(f"/api/documents/{doc_id}/sources").json()
    labels = [s["label"] for s in sources if s["kind"] == "llm"]
    assert any("(Invoice)" in label for label in labels) and any("(Receipt)" in label for label in labels)

    exported = client.get(f"/api/documents/{doc_id}/export",
                          params={"source": f"llm:{res['key']}", "fmt": "json"}).json()
    assert exported["pages"][0]["data"] == INVOICE_REPLY and exported["pages"][1]["data"] is None
    lines = client.get(f"/api/documents/{doc_id}/export", params={"source": f"llm:{res['key']}", "fmt": "jsonl"}).text
    assert json.loads(lines.splitlines()[0])["data"]["total"] == "$1,250.00"
    plain_export = client.get(f"/api/documents/{doc_id}/export", params={"source": f"llm:{plain['key']}", "fmt": "json"})
    assert "data" not in plain_export.json()["pages"][0]


def test_extract_custom_schema_and_bad_replies(client, doc_id):
    schema = {"type": "object", "properties": {"total": {"type": "string"}}, "required": ["total"]}
    client.fake.reply = '```json\n{"total": 5}\n```'
    res = _run(client, doc_id, extract_schema=schema).json()
    assert res["schema_label"] == "Custom schema" and res["schema_preset"] is None
    assert res["data"] == {"total": 5}
    assert res["data_errors"] == [{"path": "total", "message": "expected string, got integer"}]

    client.fake.reply = "Sorry, I can't read that."
    res = _run(client, doc_id, extract_schema=schema, force=True).json()
    assert res["data"] is None and res["text"] == "Sorry, I can't read that."  # raw reply kept for debugging
    assert res["data_errors"][0]["path"] == "(reply)"


def test_extract_needs_a_usable_schema(client, doc_id):
    r = _run(client, doc_id)
    assert r.status_code == 400 and "needs a schema" in r.json()["detail"]
    r = _run(client, doc_id, extract_schema='{"type": "array"}')
    assert r.status_code == 400 and "root must be an object" in r.json()["detail"]
    r = client.post(f"/api/documents/{doc_id}/jobs", json={
        "kind": "llm", "llm": {"provider": "fakejson", "prompt_mode": "extract", "extract_schema": "{bad"}})
    assert r.status_code == 400 and "valid JSON" in r.json()["detail"]
    assert client.fake.calls == []


def test_check_schema_endpoint_and_config(client):
    presets = client.get("/api/config").json()["extract_presets"]
    assert [p["id"] for p in presets] == ["invoice", "receipt", "form"]
    ok = client.post("/api/extract/check-schema", json={"extract_schema": "receipt"}).json()
    assert ok["ok"] and ok["preset"] == "receipt" and ok["fields"] == 10
    assert ok["example"]["items"] == [{"description": "text as printed", "quantity": "text as printed",
                                       "price": "text as printed"}]
    bad = client.post("/api/extract/check-schema", json={"extract_schema": "[]"})
    assert bad.status_code == 200 and not bad.json()["ok"] and "JSON object" in bad.json()["error"]

    # the chosen provider/model says how it will handle the schema
    def provider_note(provider, model):
        r = client.post("/api/extract/check-schema",
                        json={"extract_schema": "invoice", "provider": provider, "model": model}).json()
        return r["notes"][0]

    assert provider_note("anthropic", "claude-opus-5-5")["level"] == "ok"
    assert provider_note("anthropic", "claude-opus-4-6")["level"] == "warn"
    assert provider_note("openai", "gpt-5")["level"] == "ok"
    assert provider_note("openai_compatible", "qwen")["level"] == "warn"
    assert provider_note("gemini", None)["level"] == "ok"

    broken = client.post("/api/extract/check-schema", json={"extract_schema": "{type: 'object',}"}).json()
    assert not broken["ok"] and (broken["line"], broken["column"]) == (1, 2)
    assert json.loads(broken["repaired"]) == {"type": "object"}


def test_extract_job_runs_every_page(client, doc_id):
    import time

    r = client.post(f"/api/documents/{doc_id}/jobs", json={
        "kind": "llm", "llm": {"provider": "fakejson", "prompt_mode": "extract", "extract_schema": "invoice"}})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(0.02)
    assert job["status"] == "done", job
    assert job["label"].endswith("extract (Invoice)")
    results = client.get(f"/api/documents/{doc_id}/pages/1/llm").json()
    assert results[0]["data"] == INVOICE_REPLY


# ------------------------------------------------------------------ helping write schemas
@pytest.mark.parametrize("text, expected, change", [
    ("{name: 'Acme', n: 1.5,}", {"name": "Acme", "n": 1.5}, "quoted keys"),
    ('{"a": True, "b": None}', {"a": True, "b": None}, "wrote True as true"),
    ('{"a": "x"\n "b": [1 2]}', {"a": "x", "b": [1, 2]}, "added missing commas"),
    ('{"a": [1, 2,],}', {"a": [1, 2]}, "removed trailing commas"),
    ('{"a": {"b": 1}', {"a": {"b": 1}}, "closed 1 unclosed bracket"),
    ('{"a": 1}}', {"a": 1}, "removed an extra closing bracket"),
    ('{"a": 1 // why\n /* note */}', {"a": 1}, "removed comments"),
    ('```json\n{"a": 1}\n```', {"a": 1}, "removed the ``` code fence"),
    ("{\u201ca\u201d: \u201cx\u201d}", {"a": "x"}, "replaced curly quotes with straight ones"),
    ('{"type": string}', {"type": "string"}, "quoted bare words"),
    ("{'it\\'s': 'say \"hi\"'}", {"it's": 'say "hi"'}, "used double quotes for strings"),
])
def test_repair_json(text, expected, change):
    fixed, changes = extract.repair_json(text)
    assert json.loads(fixed) == expected and change in changes


def test_repair_json_gives_up_on_non_json():
    assert extract.repair_json("hello <world>")[0] is None


def test_lint_suggests_fixes():
    notes = extract.lint({
        "type": "object", "propertes": {}, "required": ["ghost"],
        "properties": {"a": {"type": "string", "maxLenght": 3}, "b": {}, "c": {"type": "object"},
                       "d": {"type": "string", "not": {"const": "x"}}},
    })
    messages = [(n["path"], n["message"]) for n in notes]
    assert ("(root)", "'propertes' isn't a JSON Schema keyword, so it's ignored (did you mean 'properties'?)") in messages
    assert ("(root)", "'required' lists 'ghost', which isn't in 'properties'") in messages
    assert ("a", "'maxLenght' isn't a JSON Schema keyword, so it's ignored (did you mean 'maxLength'?)") in messages
    assert ("b", "has no type, so the model may return anything; set \"type\"") in messages
    assert ("c", "is an object without 'properties', so it will always be {}") in messages
    assert ("d", "'not' isn't supported by providers' strict modes, so it's ignored") in messages
    # unknown keywords never reach a provider
    schema = extract.normalize({"type": "object", "properties": {"a": {"type": "string", "maxLenght": 3}}})
    assert schema["properties"]["a"] == {"type": ["string", "null"]}


def test_type_typos_and_openapi_nullable():
    with pytest.raises(extract.SchemaError, match="unknown type 'str' \\(did you mean 'string'\\?\\)"):
        extract.normalize({"type": "object", "properties": {"a": {"type": "str"}}})
    with pytest.raises(extract.SchemaError, match="did you mean 'number'"):
        extract.normalize({"type": "object", "properties": {"a": {"type": "float"}}})
    with pytest.raises(extract.SchemaError, match="'required' must be a list"):
        extract.normalize({"type": "object", "properties": {"a": {"type": "string"}}, "required": "a"})
    schema = extract.normalize({"type": "object", "properties": {"a": {"type": "string", "nullable": True}},
                                "required": ["a"]})
    assert schema["properties"]["a"] == {"type": ["string", "null"]}


def test_example_and_notes():
    schema = extract.normalize({"type": "object", "properties": {
        "total": {"type": "number", "minimum": 0}, "when": {"type": "string", "format": "date"},
        "kind": {"enum": ["a", "b"]}, "paid": {"type": "boolean"}, "code": {"type": "string", "format": "iban"},
        "rows": {"type": "array", "items": {"type": "object", "properties": {"n": {"type": "integer"}}}},
    }, "required": ["total"]})
    assert extract.example(schema) == {"total": 12.5, "when": "2025-03-14", "kind": "a", "paid": True,
                                       "code": "text as printed", "rows": [{"n": 3}]}
    notes = " | ".join(n["message"] for n in extract.schema_notes(schema))
    assert "total (minimum)" in notes and "code ('iban')" in notes
    assert "Number fields" in notes and "rows[].n" in notes
    assert "never null" in notes and ": total" in notes


def test_check_reports_without_raising():
    assert extract.check("")["error"].startswith("the schema is empty")
    r = extract.check('{"type": "object", "properties": {"x": {"type": "int"}, "y": {"type": ["bool", "null"]}}}')
    assert not r["ok"] and "did you mean 'integer'" in r["error"]
    assert r["repairs"] == ["type 'int' → 'integer'", "type 'bool' → 'boolean'"]
    assert extract.check(r["repaired"])["ok"]
    r = extract.check('{"type": "object", "properties": {"x": {"type": "string"}}, "titel": "T"}')
    assert r["ok"] and r["fields"] == 1 and r["example"] == {"x": "text as printed"}
    assert any("did you mean 'title'" in n["message"] for n in r["notes"])


# ------------------------------------------------------------------ drafting from plain words
DRAFT = {
    "title": "Purchase order",
    "fields": [
        {"name": "po number", "type": "string", "description": "", "required": True, "options": [], "children": []},
        {"name": "status", "type": "enum", "description": "", "required": False, "options": ["open", "open", "closed"],
         "children": []},
        {"name": "vague", "type": "enum", "description": "", "required": False, "options": [], "children": []},
        {"name": "lines", "type": "table", "description": "one per row", "required": False, "options": [],
         "children": [{"name": "qty", "type": "integer", "description": "", "required": False, "options": []}]},
        {"name": "empty_table", "type": "table", "description": "", "required": False, "options": [], "children": []},
        {"name": "address", "type": "object", "description": "", "required": False, "options": [],
         "children": [{"name": "city", "type": "string", "description": "", "required": False, "options": []}]},
        {"name": "status", "type": "string", "description": "duplicate", "required": False, "options": [],
         "children": []},
    ],
    "notes": "Assumed amounts are kept as printed.",
}


def test_draft_to_schema():
    schema = extract.draft_to_schema(DRAFT)
    assert schema == {
        "title": "Purchase order", "type": "object", "required": ["po_number"],
        "properties": {
            "po_number": {"type": "string"},
            "status": {"enum": ["open", "closed"]},
            "vague": {"type": "string"},
            "lines": {"description": "one per row", "type": "array",
                      "items": {"type": "object", "properties": {"qty": {"type": "integer"}}}},
            "empty_table": {"type": "array", "items": {"type": "string"}},
            "address": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
    assert extract.check(schema)["ok"]
    # the draft shape itself is a valid strict schema the providers can enforce
    assert extract.draft_schema()["properties"]["fields"]["items"]["required"][:2] == ["name", "type"]
    with pytest.raises(extract.SchemaError, match="didn't propose any fields"):
        extract.draft_to_schema({"title": "x", "fields": []})
    with pytest.raises(extract.SchemaError, match="no list of fields"):
        extract.draft_to_schema(["nope"])


def _draft(client, **body):
    return client.post("/api/extract/draft-schema", json={"provider": "fakejson", "description": "po number etc",
                                                           **body})


def test_draft_schema_endpoint(client, doc_id):
    from pyocr_toolkit.llm import DRAFT_SYSTEM

    client.fake.reply = json.dumps(DRAFT)
    r = _draft(client, doc_id=doc_id, page=1)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["schema"]["properties"]["po_number"] == {"type": "string"}
    assert res["notes"] == "Assumed amounts are kept as printed." and res["used_page"] is True
    assert res["check"]["ok"] and res["check"]["fields"] == 6 and res["input_tokens"] == 10
    req = client.fake.calls[-1]
    assert req.system_prompt == DRAFT_SYSTEM and req.image and req.max_output_tokens == 4000
    assert "po number etc" in req.user_prompt and "sample page" in req.user_prompt
    assert req.output_schema["properties"]["fields"]["type"] == "array"

    # no page, refining the current schema
    res = _draft(client, current_schema='{"type": "object", "properties": {"a": {"type": "string"}}}').json()
    req = client.fake.calls[-1]
    assert req.image == b"" and res["used_page"] is False
    assert "Current schema" in req.user_prompt and '"a"' in req.user_prompt and "sample page" not in req.user_prompt


def test_draft_schema_errors(client, doc_id):
    assert _draft(client, description="   ").json()["detail"].startswith("describe what to extract")
    assert _draft(client, provider="nope").status_code == 400
    assert "not configured" in _draft(client, provider="anthropic").json()["detail"]
    assert _draft(client, doc_id=doc_id, page=9).status_code == 400
    client.fake.reply = "I think you want an invoice number."
    r = _draft(client)
    assert r.status_code == 502 and "couldn't use the draft" in r.json()["detail"]
    client.fake.reply = json.dumps({"title": "x", "fields": [], "notes": ""})
    assert "didn't propose any fields" in _draft(client).json()["detail"]
    # it spends money, so another site can't trigger it
    r = client.post("/api/extract/draft-schema", json={"provider": "fakejson", "description": "x"},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_extraction_is_one_page_per_request(client, doc_id):
    """A batched reply is split into pages by marker lines; extraction needs one JSON object per page."""
    r = _run(client, doc_id, extract_schema="invoice", pages_per_request=2)
    assert r.status_code == 400 and "one page per request" in r.json()["detail"]
    r = client.post(f"/api/documents/{doc_id}/jobs", json={"kind": "llm", "llm": {
        "provider": "fakejson", "prompt_mode": "extract", "extract_schema": "invoice", "pages_per_request": 3}})
    assert r.status_code == 400 and "one page per request" in r.json()["detail"]
    assert client.fake.calls == []


def test_text_only_request_has_no_page_images():
    from pyocr_toolkit.llm import TranscriptionRequest

    assert TranscriptionRequest(image=b"", mime_type="", system_prompt="S", user_prompt="U").page_images() == []
    assert len(TranscriptionRequest(image=b"x", mime_type="image/png", system_prompt="S",
                                    user_prompt="U").page_images()) == 1


def test_export_all_sources_includes_extracted_fields(client, doc_id):
    res = _run(client, doc_id, extract_schema="invoice").json()
    client.post(f"/api/documents/{doc_id}/pages/0/llm", json={"provider": "fakejson"})  # a plain transcription too
    all_json = client.get(f"/api/documents/{doc_id}/export/all", params={"fmt": "json"}).json()
    source = f"llm:{res['key']}"
    assert {s["id"]: s.get("schema_label") for s in all_json["sources"]}[source] == "Invoice"
    page1 = all_json["pages"][0]
    assert page1["data"] == {source: INVOICE_REPLY}  # only extraction sources get data
    assert all_json["pages"][1]["data"] == {source: None}
    lines = [json.loads(line) for line in
             client.get(f"/api/documents/{doc_id}/export/all", params={"fmt": "jsonl"}).text.splitlines()]
    assert [line["data"]["total"] for line in lines if line["source"] == source] == ["$1,250.00"]
    assert all("data" not in line for line in lines if line["source"] != source)
