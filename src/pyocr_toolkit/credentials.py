"""Provider and OCR engine credentials and settings saved from the web UI.

Environment variables and ``.env`` stay the primary way to configure providers, but
editing a file and restarting the server is a poor experience for a local tool. The
Providers page can therefore save values into ``<DATA_DIR>/credentials.json``; they are
applied on top of the environment at startup and immediately when saved.

Why a separate file in the data dir rather than rewriting ``.env``: the ``.env`` may be
shared or hand-edited (comments, ordering), the data dir is already per-user and
git-ignored, and keeping UI values separate makes "clear and fall back to the
environment" trivial. The file is created with owner-only permissions.

Secrets still never reach the browser: callers get set / missing / saved-here states,
plus the value only for settings that aren't secrets (model names, endpoints).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import Settings
from .llm.base import Provider
from .ocr.base import OCREngine

#: Anything with ``id``, ``label``, ``env_vars``, ``env_status()`` and ``reset_client()``.
Owner = Provider | OCREngine


def is_secret(name: str) -> bool:
    return "KEY" in name or "SECRET" in name or "TOKEN" in name


class CredentialStore:
    FILENAME = "credentials.json"

    def __init__(self, settings: Settings, providers: dict[str, Provider],
                 ocr_engines: dict[str, OCREngine] | None = None) -> None:
        self.settings = settings
        self.providers = providers
        #: providers and OCR engines (their ids don't overlap), keyed by id
        self.owners: dict[str, Owner] = {**(ocr_engines or {}), **providers}
        self.path = Path(settings.data_dir) / self.FILENAME
        #: env var name -> owner id, for every setting a provider or OCR engine declares
        self.allowed: dict[str, str] = {}
        fields = type(settings).model_fields
        for p in self.owners.values():
            for name, _required, _desc in p.env_vars:
                # Only text settings: values are stored as strings, and "false" would be a
                # truthy string for a boolean like OCR_GPU (those stay env-only).
                field = fields.get(name.lower())
                if field is not None and field.annotation in (str, str | None):
                    self.allowed.setdefault(name, p.id)
        # What the environment / .env / defaults gave us, so "clear" can restore it.
        self._base: dict[str, Any] = {n: getattr(settings, n.lower()) for n in self.allowed}
        self.saved: dict[str, str] = self._read()
        for name, value in self.saved.items():
            setattr(settings, name.lower(), value)

    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in self.allowed and isinstance(v, str) and v}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.saved, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def update(self, provider_id: str, values: dict[str, str | None], clear: list[str] | None = None) -> None:
        """Save non-empty ``values`` and drop ``clear``-ed names (back to env/default).

        Empty strings in ``values`` mean "leave unchanged", so a form can send every field
        without re-typing secrets it doesn't show.
        """
        provider = self.owners.get(provider_id)
        if provider is None:
            raise ValueError(f"unknown provider '{provider_id}'")
        own = {name for name, _r, _d in provider.env_vars if name in self.allowed}
        for name in [*values, *(clear or [])]:
            if name not in own:
                raise ValueError(f"{name} is not a setting of {provider.label}")
        for name in clear or []:
            self.saved.pop(name, None)
            setattr(self.settings, name.lower(), self._base[name])
        for name, value in values.items():
            value = (value or "").strip()
            if not value:
                continue
            if any(c in value for c in "\r\n\0"):
                raise ValueError(f"{name} must be a single line")
            self.saved[name] = value
            setattr(self.settings, name.lower(), value)
        self._write()
        # A setting can be shared (OCR_GPU): reset everyone that declares a changed one.
        changed = {*values, *(clear or [])}
        for owner in self.owners.values():
            if any(name in changed for name, _r, _d in owner.env_vars):
                owner.reset_client()

    def status(self, provider: Owner) -> list[dict]:
        """``env_status()`` plus where each value comes from and, for non-secrets, the value."""
        out = []
        for e in provider.env_status():
            name = e["name"]
            secret = is_secret(name)
            value = getattr(self.settings, name.lower(), None)
            e = {**e, "secret": secret, "saved": name in self.saved,
                 "editable": name in self.allowed,
                 "from_env": bool(self._base.get(name)) and self._base.get(name) != e.get("default")}
            if not secret and isinstance(value, (str, int, float)):
                e["value"] = value
            out.append(e)
        return out
