"""Structured extraction: fill a JSON Schema from one page instead of transcribing it.

Why JSON Schema (and not just a prompt, or just Pydantic):

* A prompt alone gives JSON-ish text whose shape drifts from page to page and
  model to model (renamed keys, code fences, missing fields), so results can't be
  validated or compared field by field.
* Pydantic models can't be the stored format: users define schemas in the
  browser at runtime, and the schema has to be saved and hashed into the cache key.
  The built-in presets *are* written as Pydantic models (readable, typed) and
  turned into JSON Schema with ``model_json_schema()``.
* JSON Schema is what every provider can enforce natively (Claude
  ``output_config.format``, OpenAI ``response_format``, Gemini
  ``response_json_schema``), so it's the one format that travels everywhere.

Every schema goes through :func:`normalize` first: ``$ref`` inlined, every object
closed (``additionalProperties: false``) with all properties required and the
optional ones made nullable. That is the strictest common subset, so the same
normalized schema works with every provider's strict mode, and its canonical JSON
is what goes into the LLM cache key.

Values in the presets are strings on purpose: "copy what's printed" (``$1,234.00``,
``03/04/25``) keeps extraction honest the same way the verbatim prompt keeps
transcription honest. Converting to numbers or dates is a later, separate step.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


# ------------------------------------------------------------------ presets
class _LineItem(BaseModel):
    description: str | None = Field(None, description="item or service description as printed")
    quantity: str | None = Field(None, description="quantity as printed")
    unit_price: str | None = Field(None, description="unit price as printed, with currency symbol if shown")
    amount: str | None = Field(None, description="line total as printed")


class Invoice(BaseModel):
    """An invoice or bill."""

    vendor_name: str | None = Field(None, description="company or person issuing the invoice")
    vendor_address: str | None = Field(None, description="issuer's address, lines joined with ', '")
    vendor_tax_id: str | None = Field(None, description="issuer's VAT / GST / tax number")
    invoice_number: str | None = None
    invoice_date: str | None = Field(None, description="as printed, don't reformat")
    due_date: str | None = Field(None, description="as printed, don't reformat")
    bill_to: str | None = Field(None, description="customer name and address, lines joined with ', '")
    currency: str | None = Field(None, description="currency code or symbol as printed")
    line_items: list[_LineItem] = Field(default_factory=list, description="one entry per printed row")
    subtotal: str | None = None
    tax: str | None = Field(None, description="total tax as printed")
    total: str | None = Field(None, description="amount due / grand total as printed")


class _ReceiptItem(BaseModel):
    description: str | None = None
    quantity: str | None = None
    price: str | None = Field(None, description="as printed")


class Receipt(BaseModel):
    """A till / shop receipt."""

    merchant_name: str | None = None
    merchant_address: str | None = Field(None, description="lines joined with ', '")
    date: str | None = Field(None, description="as printed, don't reformat")
    time: str | None = Field(None, description="as printed")
    items: list[_ReceiptItem] = Field(default_factory=list, description="one entry per printed item line")
    subtotal: str | None = None
    tax: str | None = None
    tip: str | None = None
    total: str | None = None
    payment_method: str | None = Field(None, description="e.g. VISA **** 1234, cash")


class _FormField(BaseModel):
    label: str = Field(description="the printed label or question, exactly as printed")
    value: str | None = Field(None, description="the filled-in value; null if left blank")


class FormFields(BaseModel):
    """Any form: every label and its filled-in value, in reading order."""

    fields: list[_FormField] = Field(default_factory=list)


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    model: type[BaseModel]

    @property
    def description(self) -> str:
        return (self.model.__doc__ or "").strip()

    @property
    def schema(self) -> dict[str, Any]:
        return normalize(self.model.model_json_schema())


PRESETS: dict[str, Preset] = {p.id: p for p in [
    Preset("invoice", "Invoice", Invoice),
    Preset("receipt", "Receipt", Receipt),
    Preset("form", "Form fields (label → value)", FormFields),
]}


def presets_json() -> list[dict[str, Any]]:
    return [{"id": p.id, "label": p.label, "description": p.description, "schema": p.schema} for p in PRESETS.values()]


# ------------------------------------------------------------------ normalizing
class SchemaError(ValueError):
    """A user-supplied schema can't be used; the message says what to fix."""


# Annotation-only keywords: dropped so they don't change the cache key or trip strict modes.
_DROP = {"title", "default", "examples", "$schema", "$id", "$comment", "readOnly", "writeOnly", "deprecated"}
# Constraints providers' strict modes reject; kept for local validation, stripped before sending.
_LOCAL_ONLY = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength",
               "maxLength", "pattern", "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties"}
_FORMATS = {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
# Keywords copied through as they are; anything not listed anywhere here is dropped (and reported by lint()).
_KEEP = {"type", "required", "enum", "const", "description", "additionalProperties", "format"} | _LOCAL_ONLY
_STRUCTURE = {"properties", "items", "anyOf", "oneOf", "allOf", "$ref", "$defs", "definitions", "nullable"}
KEYWORDS = _DROP | _KEEP | _STRUCTURE
# Real JSON Schema, but no provider's strict mode accepts it.
_UNSUPPORTED = {"not", "if", "then", "else", "patternProperties", "propertyNames", "dependentRequired",
                "dependentSchemas", "prefixItems", "contains", "minContains", "maxContains", "unevaluatedProperties",
                "unevaluatedItems", "contentEncoding", "contentMediaType", "$anchor", "$dynamicRef"}
# Type names people write from other languages -> the JSON Schema name.
TYPE_ALIASES = {"str": "string", "text": "string", "char": "string", "date": "string", "datetime": "string",
                "int": "integer", "long": "integer", "float": "number", "double": "number", "decimal": "number",
                "money": "number", "bool": "boolean", "list": "array", "tuple": "array", "dict": "object",
                "map": "object", "none": "null", "nil": "null"}


def _type_hint(t: Any) -> str:
    alias = TYPE_ALIASES.get(str(t).lower())
    if alias:
        return f" (did you mean '{alias}'?)"
    close = difflib.get_close_matches(str(t), _TYPES, n=1, cutoff=0.6)
    return f" (did you mean '{close[0]}'?)" if close else f" (use one of: {', '.join(sorted(_TYPES))})"


def _allows_null(node: dict[str, Any]) -> bool:
    t = node.get("type")
    if t == "null" or (isinstance(t, list) and "null" in t):
        return True
    return any(_allows_null(o) for o in node.get("anyOf", []))


def _nullable(node: dict[str, Any]) -> dict[str, Any]:
    if _allows_null(node):
        return node
    if "anyOf" in node:
        return {**node, "anyOf": [*node["anyOf"], {"type": "null"}]}
    if isinstance(node.get("type"), str) and "enum" not in node and "const" not in node:
        return {**node, "type": [node["type"], "null"]}
    desc = node.get("description")
    inner = {k: v for k, v in node.items() if k != "description"}
    return {"anyOf": [inner, {"type": "null"}], **({"description": desc} if desc else {})}


def normalize(schema: Any) -> dict[str, Any]:
    """Return the strict, self-contained form of ``schema`` (see the module docstring).

    Raises :class:`SchemaError` for schemas no provider could enforce (root not an
    object, recursive ``$ref``, unknown types).
    """
    if not isinstance(schema, dict):
        raise SchemaError("the schema must be a JSON object")
    defs = {**schema.get("definitions", {}), **schema.get("$defs", {})}

    def walk(node: Any, path: str, stack: tuple[str, ...]) -> Any:
        if isinstance(node, list):
            return [walk(x, path, stack) for x in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            ref = node["$ref"]
            name = ref.rsplit("/", 1)[-1]
            if not ref.startswith(("#/$defs/", "#/definitions/")) or name not in defs:
                raise SchemaError(f"{path or 'schema'}: can't resolve $ref '{ref}' (only local #/$defs/... refs work)")
            if name in stack:
                raise SchemaError(f"{path or 'schema'}: recursive schemas aren't supported ('{name}' refers to itself)")
            extra = {k: v for k, v in node.items() if k not in ("$ref", *_DROP)}
            return walk({**defs[name], **extra}, path, (*stack, name))
        out: dict[str, Any] = {}
        where = path or "schema"
        for key, value in node.items():
            if key == "properties":
                if not isinstance(value, dict):
                    raise SchemaError(f"{where}: 'properties' must be an object of field name -> schema")
                out[key] = {k: walk(v, f"{path}.{k}" if path else k, stack) for k, v in value.items()}
            elif key == "items":
                out[key] = walk(value, f"{path}[]", stack)
            elif key in ("anyOf", "oneOf", "allOf"):
                if not isinstance(value, list):
                    raise SchemaError(f"{where}: '{key}' must be a list of schemas")
                out["anyOf" if key == "oneOf" else key] = walk(value, path, stack)
            elif key == "required" and not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
                raise SchemaError(f"{where}: 'required' must be a list of field names, e.g. [\"total\"]")
            elif key in _KEEP:
                out[key] = value
        types = out.get("type")
        for t in types if isinstance(types, list) else [types] if types else []:
            if t not in _TYPES:
                raise SchemaError(f"{where}: unknown type '{t}'{_type_hint(t)}")
        if out.get("type") == "object" or "properties" in out:
            out["type"] = "object"
            props = out.get("properties", {})
            required = set(out.get("required", []))
            out["properties"] = {k: v if k in required else _nullable(v) for k, v in props.items()}
            out["required"] = list(props)
            out["additionalProperties"] = False
        if out.get("type") == "array" and "items" not in out:
            out["items"] = {"type": "string"}
        if node.get("nullable") is True:  # OpenAPI style; JSON Schema says type: [x, "null"]
            out = _nullable(out)
        return out

    result = walk(schema, "", ())
    if result.get("type") != "object" or not result.get("properties"):
        raise SchemaError("the schema's root must be an object with at least one property")
    return result


def provider_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """``schema`` minus the constraints strict modes reject (they're still checked by :func:`validate`)."""

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for k, v in node.items():
            if k in _LOCAL_ONLY or (k == "format" and v not in _FORMATS):
                continue
            out[k] = {pk: walk(pv) for pk, pv in v.items()} if k == "properties" else walk(v)
        return out

    return walk(copy.deepcopy(schema))


def canonical(schema: dict[str, Any]) -> str:
    """Stable text of a normalized schema, for the cache key. Property order is kept: it's the display order."""
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


def resolve(spec: str | dict | None) -> tuple[dict[str, Any], str, str | None]:
    """``spec`` (a preset id, JSON Schema text or dict) -> (normalized schema, label, preset id or None)."""
    if isinstance(spec, str) and spec.strip() in PRESETS:
        p = PRESETS[spec.strip()]
        return p.schema, p.label, p.id
    if isinstance(spec, str):
        if not spec.strip():
            raise SchemaError(f"structured extraction needs a schema: a preset ({', '.join(PRESETS)}) or a JSON Schema")
        try:
            spec = json.loads(spec)
        except json.JSONDecodeError as exc:
            raise SchemaError(f"the schema isn't valid JSON (line {exc.lineno}, column {exc.colno}: {exc.msg})") from exc
    if spec is None:
        raise SchemaError(f"structured extraction needs a schema: a preset ({', '.join(PRESETS)}) or a JSON Schema")
    schema = normalize(spec)
    title = spec.get("title") if isinstance(spec, dict) else None
    return schema, f"Custom: {title}" if isinstance(title, str) and title.strip() else "Custom schema", None


# ------------------------------------------------------------------ parsing + validation
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n?(.*?)\n?\s*```\s*$", re.S)


def parse_reply(text: str) -> tuple[Any, str | None]:
    """Model reply -> (data, error). Tolerates code fences and chatter around one JSON object."""
    body = text.strip()
    m = _FENCE.match(body)
    if m:
        body = m.group(1).strip()
    try:
        return json.loads(body), None
    except json.JSONDecodeError:
        pass
    start, end = body.find("{"), body.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(body[start:end + 1]), None
        except json.JSONDecodeError as exc:
            return None, f"the reply isn't valid JSON ({exc.msg} at line {exc.lineno})"
    return None, "the reply contains no JSON object"


def _type_ok(value: Any, t: str) -> bool:
    if t == "null":
        return value is None
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, {"string": str, "array": list, "object": dict}[t])


def validate(data: Any, schema: dict[str, Any]) -> list[dict[str, str]]:
    """Check ``data`` against a normalized schema; ``[{"path": "line_items[0].amount", "message": ...}]``.

    Covers the subset :func:`normalize` produces (type, enum, const, anyOf/allOf,
    properties, items, and the usual numeric/length limits), which is enough to
    catch a model that ignored the schema; no extra dependency needed.
    """
    errors: list[dict[str, str]] = []

    def err(path: str, message: str) -> None:
        errors.append({"path": path or "(root)", "message": message})

    def check(value: Any, node: dict[str, Any], path: str) -> None:
        if "anyOf" in node:
            if not any(not _collect(value, o, path) for o in node["anyOf"]):
                options = " or ".join(_describe(o) for o in node["anyOf"])
                err(path, f"expected {options}, got {_describe_value(value)}")
                return
        for sub in node.get("allOf", []):
            check(value, sub, path)
        t = node.get("type")
        types = t if isinstance(t, list) else [t] if t else []
        if types and not any(_type_ok(value, x) for x in types):
            err(path, f"expected {' or '.join(types)}, got {_describe_value(value)}")
            return
        if "enum" in node and value not in node["enum"]:
            err(path, f"must be one of {', '.join(json.dumps(e) for e in node['enum'])}")
        if "const" in node and value != node["const"]:
            err(path, f"must be {json.dumps(node['const'])}")
        if isinstance(value, dict):
            props = node.get("properties", {})
            for key in node.get("required", []):
                if key not in value:
                    err(f"{path}.{key}" if path else key, "missing")
            for key, v in value.items():
                sub_path = f"{path}.{key}" if path else key
                if key in props:
                    check(v, props[key], sub_path)
                elif node.get("additionalProperties") is False:
                    err(sub_path, "not in the schema")
        elif isinstance(value, list):
            if "items" in node:
                for i, v in enumerate(value):
                    check(v, node["items"], f"{path}[{i}]")
            if "minItems" in node and len(value) < node["minItems"]:
                err(path, f"needs at least {node['minItems']} item(s)")
            if "maxItems" in node and len(value) > node["maxItems"]:
                err(path, f"allows at most {node['maxItems']} item(s)")
        elif isinstance(value, str):
            if "minLength" in node and len(value) < node["minLength"]:
                err(path, f"shorter than {node['minLength']} characters")
            if "maxLength" in node and len(value) > node["maxLength"]:
                err(path, f"longer than {node['maxLength']} characters")
            if "pattern" in node and not re.search(node["pattern"], value):
                err(path, f"doesn't match {node['pattern']}")
        elif _type_ok(value, "number"):
            if "minimum" in node and value < node["minimum"]:
                err(path, f"below the minimum {node['minimum']}")
            if "maximum" in node and value > node["maximum"]:
                err(path, f"above the maximum {node['maximum']}")

    def _collect(value: Any, node: dict[str, Any], path: str) -> list:
        before = len(errors)
        check(value, node, path)
        found = errors[before:]
        del errors[before:]
        return found

    check(data, schema, "")
    return errors


def _describe(node: dict[str, Any]) -> str:
    t = node.get("type")
    return "/".join(t) if isinstance(t, list) else t or "a matching value"


def _describe_value(value: Any) -> str:
    if value is None:
        return "null"
    return {bool: "boolean", int: "integer", float: "number", str: "string", list: "array", dict: "object"}.get(
        type(value), type(value).__name__)


def count_fields(data: Any, schema: dict[str, Any]) -> tuple[int, int]:
    """(filled, total) leaf fields of the top-level object, for a "9 of 12 fields found" summary.

    A list counts as one field (filled when it has entries).
    """
    if not isinstance(data, dict):
        return 0, len(schema.get("properties", {}))
    filled = total = 0

    def walk(value: Any, node: dict[str, Any]) -> None:
        nonlocal filled, total
        props = node.get("properties")
        if props is None:
            for o in node.get("anyOf", []):
                if o.get("properties"):
                    props = o["properties"]
        if props is not None and isinstance(value, dict):
            for k, sub in props.items():
                walk(value.get(k), sub)
            return
        total += 1
        if value not in (None, "", [], {}):
            filled += 1

    walk(data, schema)
    return filled, total

# ------------------------------------------------------------------ helping people write schemas
# What the schema editor shows: notes ("did you mean 'properties'?"), an example of the
# output, and a repaired version of JSON that doesn't parse. Kept here (not in the
# browser) so it's tested and has one implementation.

def _note(level: str, path: str, message: str) -> dict[str, str]:
    """``level``: ``error`` (can't be used), ``warn`` (probably not what you meant), ``info``, ``ok``."""
    return {"level": level, "path": path or "(root)", "message": message}


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def lint(raw: Any) -> list[dict[str, str]]:
    """Mistakes in a schema as written: unknown keywords (with suggestions), bad ``required``, untyped fields.

    Runs on the raw schema, before :func:`normalize`, which would silently drop
    what it doesn't know. Paths read like ``line_items[].amount``.
    """
    notes: list[dict[str, str]] = []
    if not isinstance(raw, dict):
        return notes

    def walk(node: Any, path: str, is_field: bool) -> None:
        if not isinstance(node, dict):
            if is_field:
                notes.append(_note("error", path, "a field's schema must be an object, e.g. {\"type\": \"string\"}"))
            return
        for key, value in node.items():
            if key in KEYWORDS:
                continue
            if key in _UNSUPPORTED:
                notes.append(_note("warn", path, f"'{key}' isn't supported by providers' strict modes, so it's ignored"))
                continue
            close = difflib.get_close_matches(key, KEYWORDS, n=1, cutoff=0.7)
            hint = f" (did you mean '{close[0]}'?)" if close else ""
            if not close and isinstance(value, dict) and ("type" in value or "properties" in value):
                hint = " (fields go inside \"properties\")"
            notes.append(_note("warn", path, f"'{key}' isn't a JSON Schema keyword, so it's ignored{hint}"))
        props = node.get("properties")
        if isinstance(props, dict):
            required = node.get("required", [])
            for name in required if isinstance(required, list) else []:
                if isinstance(name, str) and name not in props:
                    notes.append(_note("warn", path, f"'required' lists '{name}', which isn't in 'properties'"))
            if not props and path:
                notes.append(_note("warn", path, "has no fields, so it will always be {}"))
            for name, sub in props.items():
                walk(sub, _join(path, name), True)
        elif node.get("type") == "object" and path:
            notes.append(_note("warn", path, "is an object without 'properties', so it will always be {}"))
        if "items" in node:
            walk(node["items"], f"{path}[]", True)
        for key in ("anyOf", "oneOf", "allOf"):
            for sub in node.get(key, []) if isinstance(node.get(key), list) else []:
                walk(sub, path, False)
        if node.get("oneOf") is not None:
            notes.append(_note("info", path, "'oneOf' is treated as 'anyOf' (strict modes only support anyOf)"))
        if "additionalProperties" in node and node["additionalProperties"] is not False:
            notes.append(_note("info", path, "'additionalProperties' is always false here: strict modes need closed objects"))
        if node.get("enum") == []:
            notes.append(_note("warn", path, "'enum' is empty, so no value can match"))
        if is_field and not ({"type", "enum", "const", "anyOf", "oneOf", "allOf", "$ref", "properties"} & node.keys()):
            notes.append(_note("warn", path, "has no type, so the model may return anything; set \"type\""))
        for name, sub in {**node.get("$defs", {}), **node.get("definitions", {})}.items() if path == "" else []:
            walk(sub, f"$defs.{name}", False)

    walk(raw, "", False)
    return notes


def _fields(schema: dict[str, Any], path: str = "", depth: int = 1):
    """Yield (path, node, depth) for every field of a normalized schema, nullable wrappers unwrapped."""
    for name, node in (schema.get("properties") or {}).items():
        inner = next((o for o in node.get("anyOf", []) if o.get("type") != "null"), node) if "anyOf" in node else node
        sub = _join(path, name)
        yield sub, node, depth
        if inner.get("properties"):
            yield from _fields(inner, sub, depth + 1)
        items = inner.get("items")
        if isinstance(items, dict):
            items = next((o for o in items.get("anyOf", []) if o.get("type") != "null"), items)
            if items.get("properties"):
                yield from _fields(items, f"{sub}[]", depth + 1)


def schema_notes(schema: dict[str, Any]) -> list[dict[str, str]]:
    """Things worth knowing about a normalized schema before paying for a run."""
    notes: list[dict[str, str]] = []
    fields = list(_fields(schema))
    local, formats, numbers, required = [], [], [], []
    for path, node, _ in fields:
        flat = json.dumps(node)
        local += [f"{path} ({k})" for k in _LOCAL_ONLY if f'"{k}"' in flat and not node.get("properties")]
        fmt = node.get("format") or next((o.get("format") for o in node.get("anyOf", []) if o.get("format")), None)
        if fmt and fmt not in _FORMATS:
            formats.append(f"{path} ('{fmt}')")
        if re.search(r'"type": (\[)?"(number|integer)"', flat) and not node.get("properties") and "items" not in node:
            numbers.append(path)
        if not _allows_null(node):
            required.append(path)
    if local:
        notes.append(_note("info", "", "Not sent to the model, because strict modes don't accept them; pyocr checks them "
                                       "after the reply: " + ", ".join(sorted(set(local))[:8])))
    if formats:
        notes.append(_note("warn", "", "Unknown 'format', ignored: " + ", ".join(formats)
                           + f". Supported: {', '.join(sorted(_FORMATS))}"))
    if numbers:
        notes.append(_note("info", "", "Number fields make the model convert what's printed ('$1,250.00' → 1250): "
                                       + ", ".join(numbers[:8]) + ". Use text to keep values exactly as printed."))
    if required:
        notes.append(_note("info", "", "Always need a value (never null), so the model has to fill them even when they "
                                       "aren't on the page: " + ", ".join(required[:8])
                           + (" …" if len(required) > 8 else "")))
    depth = max((d for _, _, d in fields), default=0)
    if depth > 5:
        notes.append(_note("warn", "", f"Nested {depth} levels deep. Some providers limit nesting, and flat schemas "
                                       "are easier for models to fill"))
    if len(fields) > 100:
        notes.append(_note("warn", "", f"{len(fields)} fields: large schemas cost more tokens per page, and some "
                                       "providers limit their size"))
    return notes


_FORMAT_EXAMPLES = {"date": "2025-03-14", "date-time": "2025-03-14T09:30:00Z", "time": "09:30:00",
                    "duration": "P3D", "email": "name@example.com", "hostname": "example.com",
                    "uri": "https://example.com", "ipv4": "192.0.2.1", "ipv6": "2001:db8::1",
                    "uuid": "123e4567-e89b-12d3-a456-426614174000"}


def example(schema: dict[str, Any]) -> Any:
    """A sample reply for a normalized schema, to show what the output will look like.

    Every list gets one entry and every field a placeholder of its type (nullable
    fields show their non-null type; the editor says missing fields come back null).
    """

    def walk(node: dict[str, Any]) -> Any:
        if "const" in node:
            return node["const"]
        if node.get("enum"):
            return node["enum"][0]
        options = [o for o in node.get("anyOf", []) if o.get("type") != "null"]
        if options:
            return walk({**options[0], **{k: v for k, v in node.items() if k != "anyOf"}})
        for sub in node.get("allOf", []):
            node = {**node, **sub}
        t = node.get("type")
        if isinstance(t, list):
            t = next((x for x in t if x != "null"), "null")
        if t == "object" or "properties" in node:
            return {k: walk(v) for k, v in node.get("properties", {}).items()}
        if t == "array":
            return [walk(node.get("items") or {"type": "string"})]
        if t == "integer":
            return 3
        if t == "number":
            return 12.5
        if t == "boolean":
            return True
        if t == "null":
            return None
        return _FORMAT_EXAMPLES.get(node.get("format", ""), "text as printed")

    return walk(schema)


# --- repairing JSON people typed by hand
_TOKEN = re.compile(r"""
    (?P<ws>\s+) | (?P<line>//[^\n]*|\#[^\n]*) | (?P<block>/\*.*?(?:\*/|$)) |
    (?P<p>[{}\[\]:,]) |
    (?P<num>-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?) |
    (?P<word>[A-Za-z_$][\w$.\-]*)
""", re.X | re.S)
_QUOTES = {'"': '"', "'": "'", "“": "”", "‘": "’"}
_LITERALS = {"true": "true", "false": "false", "null": "null", "True": "true", "False": "false", "None": "null",
             "undefined": "null"}


def repair_json(text: str) -> tuple[str | None, list[str]]:
    """Fix the usual hand-typing mistakes; returns (pretty JSON or None, what was changed).

    Handles code fences, comments, single or curly quotes, unquoted keys and words,
    Python ``True``/``False``/``None``, trailing or missing commas, and missing or
    extra closing brackets. Returns ``None`` when the result still isn't JSON.
    """
    changes: list[str] = []

    def changed(what: str) -> None:
        if what not in changes:
            changes.append(what)

    body = text.strip()
    m = _FENCE.match(body)
    if m:
        body = m.group(1)
        changed("removed the ``` code fence")
    elif body.startswith("```"):  # an opening fence without its closing one
        body = body.split("\n", 1)[1] if "\n" in body else ""
        changed("removed the ``` code fence")
    tokens: list[tuple[str, str]] = []  # (kind, json text): kind is open/close/colon/comma/value/key-candidate
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c in _QUOTES:
            close, j, raw = _QUOTES[c], i + 1, []
            while j < n and body[j] != close and body[j] != "\n":
                if body[j] == "\\" and j + 1 < n:
                    raw.append(body[j:j + 2])
                    j += 2
                    continue
                raw.append(body[j])
                j += 1
            closed = j < n and body[j] == close
            if not closed:
                changed("closed an unterminated string")
            content = "".join(raw)
            if c != '"':
                changed("used double quotes for strings" if c == "'" else "replaced curly quotes with straight ones")
                content = re.sub(r'(?<!\\)"', r'\\"', content.replace("\\'", "'"))
            try:
                value = json.loads(f'"{content}"', strict=False)
            except json.JSONDecodeError:
                value = content
            tokens.append(("str", json.dumps(value, ensure_ascii=False)))
            i = j + 1 if closed else j
            continue
        m = _TOKEN.match(body, i)
        if not m:
            return None, changes
        i = m.end()
        kind = m.lastgroup
        if kind in ("line", "block"):
            changed("removed comments")
        elif kind == "p":
            tokens.append(({"{": "open", "[": "open", "}": "close", "]": "close", ":": "colon", ",": "comma"}[m.group()],
                           m.group()))
        elif kind == "num":
            num = m.group()
            fixed = ("0" + num if num.startswith(".") else num.replace("-.", "-0.")).rstrip(".")
            tokens.append(("val", fixed))
        elif kind == "word":
            tokens.append(("word", m.group()))

    out: list[str] = []
    stack: list[str] = []
    prev = "start"  # what the last emitted token was: start/open/value/colon/comma
    for k, (kind, tok) in enumerate(tokens):
        nxt = tokens[k + 1][0] if k + 1 < len(tokens) else "end"
        if kind == "word":
            if nxt == "colon":
                kind, tok = "str", json.dumps(tok)
                changed("quoted keys")
            elif tok in _LITERALS:
                if tok != _LITERALS[tok]:
                    changed(f"wrote {tok} as {_LITERALS[tok]}")
                kind, tok = "val", _LITERALS[tok]
            else:
                kind, tok = "str", json.dumps(tok)
                changed("quoted bare words")
        if kind in ("str", "val", "open"):
            if prev == "value":
                out.append(",")
                changed("added missing commas")
            if kind == "open":
                stack.append("}" if tok == "{" else "]")
                prev = "open"
            else:
                prev = "value"
            out.append(tok)
        elif kind == "close":
            if not stack:
                changed("removed an extra closing bracket")
                continue
            want = stack.pop()
            if tok != want:
                changed(f"replaced a mismatched '{tok}' with '{want}'")
            out.append(want)
            prev = "value"
        elif kind == "comma":
            if nxt in ("close", "end"):
                changed("removed trailing commas")
                continue
            if prev in ("open", "comma", "start", "colon"):
                changed("removed extra commas")
                continue
            out.append(",")
            prev = "comma"
        elif kind == "colon":
            out.append(":")
            prev = "colon"
    if stack:
        changed(f"closed {len(stack)} unclosed bracket{'s' if len(stack) > 1 else ''}")
        out.extend(reversed(stack))
    try:
        data = json.loads("".join(out))
    except json.JSONDecodeError:
        return None, changes
    return json.dumps(data, ensure_ascii=False, indent=2), changes


def _fix_type_names(raw: Any) -> tuple[Any, list[str]]:
    """Replace type names from other languages (``str``, ``bool``, ``float``...) with JSON Schema's."""
    changes: list[str] = []

    def fix(t: Any) -> Any:
        if isinstance(t, str) and t not in _TYPES and t.lower() in TYPE_ALIASES:
            new = TYPE_ALIASES[t.lower()]
            if f"type '{t}' → '{new}'" not in changes:
                changes.append(f"type '{t}' → '{new}'")
            return new
        return t

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for k, v in node.items():
            if k == "type":
                out[k] = [fix(x) for x in v] if isinstance(v, list) else fix(v)
            elif k == "properties" and isinstance(v, dict):
                out[k] = {name: walk(sub) for name, sub in v.items()}
            else:
                out[k] = walk(v)
        return out

    return walk(raw), changes


def check(spec: str | dict) -> dict[str, Any]:
    """Everything the schema editor shows for ``spec`` (a preset id, JSON text or a dict).

    Never raises: an unusable schema comes back with ``ok: False`` and an ``error``
    (plus ``line``/``column`` and a ``repaired`` version when the JSON itself is broken).
    """
    out: dict[str, Any] = {"ok": False, "error": None, "line": None, "column": None, "repaired": None,
                           "repairs": [], "notes": [], "schema": None, "example": None, "label": None,
                           "preset": None, "fields": 0}
    raw: Any = spec
    if isinstance(spec, str) and spec.strip() in PRESETS:
        raw = PRESETS[spec.strip()].model.model_json_schema()
        out["preset"] = spec.strip()
    elif isinstance(spec, str):
        if not spec.strip():
            out["error"] = "the schema is empty: add a field, or pick a preset to start from"
            return out
        try:
            raw = json.loads(spec)
        except json.JSONDecodeError as exc:
            out.update(error=f"not valid JSON: {exc.msg}", line=exc.lineno, column=exc.colno)
            fixed, changes = repair_json(spec)
            if fixed is not None:
                out.update(repaired=fixed, repairs=changes)
            return out
    out["notes"] = lint(raw) if out["preset"] is None else []
    try:
        schema, label, _ = resolve(out["preset"] or raw)
    except SchemaError as exc:
        out["error"] = str(exc)
        fixed, changes = _fix_type_names(raw)
        if changes:
            try:
                normalize(fixed)
            except SchemaError:
                pass
            else:
                out.update(repaired=json.dumps(fixed, ensure_ascii=False, indent=2), repairs=changes)
        return out
    out.update(ok=True, schema=schema, label=label, example=example(schema), fields=count_fields({}, schema)[1])
    out["notes"] += schema_notes(schema)
    return out


__all__ = ["KEYWORDS", "PRESETS", "Preset", "SchemaError", "canonical", "check", "count_fields", "example", "lint",
           "normalize", "parse_reply", "presets_json", "provider_schema", "repair_json", "resolve", "schema_notes",
           "validate"]
