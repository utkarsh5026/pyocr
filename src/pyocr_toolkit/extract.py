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
        for key, value in node.items():
            if key in _DROP or key in ("$defs", "definitions"):
                continue
            if key == "properties":
                out[key] = {k: walk(v, f"{path}.{k}" if path else k, stack) for k, v in value.items()}
            elif key == "items":
                out[key] = walk(value, f"{path}[]", stack)
            elif key in ("anyOf", "oneOf", "allOf"):
                out["anyOf" if key == "oneOf" else key] = walk(value, path, stack)
            else:
                out[key] = value
        types = out.get("type")
        for t in types if isinstance(types, list) else [types] if types else []:
            if t not in _TYPES:
                raise SchemaError(f"{path or 'schema'}: unknown type '{t}'")
        if out.get("type") == "object" or "properties" in out:
            out["type"] = "object"
            props = out.get("properties", {})
            required = set(out.get("required", []))
            out["properties"] = {k: v if k in required else _nullable(v) for k, v in props.items()}
            out["required"] = list(props)
            out["additionalProperties"] = False
        if out.get("type") == "array" and "items" not in out:
            out["items"] = {"type": "string"}
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


__all__ = ["PRESETS", "Preset", "SchemaError", "canonical", "count_fields", "normalize", "parse_reply",
           "presets_json", "provider_schema", "resolve", "validate"]
