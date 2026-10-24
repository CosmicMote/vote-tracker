"""Write settings back to config.toml, keeping its comments and layout (via tomlkit)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.items import Table

from .config import DEFAULT_BASE_WEIGHTS, Settings

# Fields whose value is a free-form mapping: replaced wholesale, so removed keys disappear.
MAPPING_FIELDS = {("scoring", "base_weights"), ("scoring", "topic_boost")}


def settings_to_data(settings: Settings) -> dict[str, Any]:
    """Settings as TOML-ready data: no secrets, no nulls, and only non-default base weights."""
    data = settings.model_dump(mode="json", exclude={"secrets"})
    weights = data["scoring"]["base_weights"]
    data["scoring"]["base_weights"] = {k: v for k, v in weights.items() if DEFAULT_BASE_WEIGHTS.get(k) != v}
    return data


def _apply(table: Table | tomlkit.TOMLDocument, data: dict[str, Any], path: tuple[str, ...] = ()) -> None:
    for key, value in data.items():
        here = (*path, key)
        if value is None:
            if key in table:
                del table[key]
        elif isinstance(value, dict) and here in MAPPING_FIELDS:
            sub = table.get(key)
            if not isinstance(sub, Table):
                sub = tomlkit.table()
                table[key] = sub
            for stale in [k for k in sub if k not in value]:
                del sub[stale]
            for k, v in value.items():
                sub[k] = v
        elif isinstance(value, dict):
            sub = table.get(key)
            if not isinstance(sub, Table):
                sub = tomlkit.table()
                table[key] = sub
            _apply(sub, value, here)
        elif key not in table or table[key] != value:
            table[key] = value  # assigning in place keeps the line's trailing comment


def _changes(old: dict[str, Any], new: dict[str, Any], path: tuple[str, ...] = ()) -> dict[str, Any]:
    """The parts of `new` that differ from `old`. Mapping fields are kept whole when they differ."""
    out: dict[str, Any] = {}
    for key, value in new.items():
        here = (*path, key)
        before = old.get(key)
        if isinstance(value, dict) and here not in MAPPING_FIELDS and isinstance(before, dict):
            sub = _changes(before, value, here)
            if sub:
                out[key] = sub
        elif value != before:
            out[key] = value
    return out


def save_settings(path: Path, settings: Settings) -> None:
    """Update config.toml in place, touching only values that changed, so unchanged lines,
    comments and keys the tracker does not know about are left exactly as they were."""
    if path.exists():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        old = settings_to_data(Settings.model_validate({k: v for k, v in doc.unwrap().items() if k != "secrets"}))
    else:
        doc, old = tomlkit.document(), {}
    changes = _changes(old, settings_to_data(settings))
    if not changes:
        return
    _apply(doc, changes)
    text = tomlkit.dumps(doc)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    os.replace(tmp, path)
