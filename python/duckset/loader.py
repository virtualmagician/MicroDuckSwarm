"""Load and parse .duckset/1 JSON documents into the model.py dataclasses.

Same split as python/duckshow: this module fails only on things that keep a
document from being loaded at all (missing or malformed `format`, an
unsupported major, a document that is not a JSON object, a field of the wrong
JSON type). Everything semantic is validator.py's job, run separately, so a
caller can load a setlist and report its problems rather than refusing it.

Unknown fields are ignored, per docs/setlist-format.md and CLAUDE.md rule 4.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Union

from .model import DEFAULT_END, Entry, SetMeta, Setlist

SUPPORTED_FORMAT_MAJOR = 1
_FORMAT_RE = re.compile(r"^duckset/(\d+)$")


class DuckSetFormatError(ValueError):
    """Raised for anything that keeps a document from being loaded at all."""


def _require_dict(value: Any, field_name: str) -> dict:
    if not isinstance(value, dict):
        raise DuckSetFormatError(f"expected {field_name!r} to be an object, got {type(value).__name__}")
    return value


def _optional_str(value: Any, field_name: str) -> Union[str, None]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DuckSetFormatError(f"expected {field_name!r} to be a string, got {type(value).__name__}")
    return value


def _parse_entry(raw: Any, index: int) -> Entry:
    obj = _require_dict(raw, f"entries[{index}]")
    entry_id = obj.get("id")
    if not isinstance(entry_id, str):
        raise DuckSetFormatError(f"entries[{index}].id must be a string")
    show = obj.get("show")
    if not isinstance(show, str):
        raise DuckSetFormatError(f"entries[{index}].show must be a string")
    end = obj.get("end", DEFAULT_END)
    if not isinstance(end, str):
        raise DuckSetFormatError(f"entries[{index}].end must be a string")
    # Note: `end` is NOT checked against the enum here. An unknown value is a
    # semantic problem the validator reports with a location, not a parse
    # failure, so a setlist written by a newer editor still opens in this one.
    return Entry(id=entry_id, show=show, end=end, label=_optional_str(obj.get("label"), f"entries[{index}].label"))


def loads_setlist(text: str) -> Setlist:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DuckSetFormatError(f"not valid JSON: {exc}") from None
    return parse_setlist(doc)


def parse_setlist(doc: Any) -> Setlist:
    obj = _require_dict(doc, "document")
    fmt = obj.get("format")
    if not isinstance(fmt, str):
        raise DuckSetFormatError('missing "format": expected "duckset/1"')
    match = _FORMAT_RE.match(fmt)
    if match is None:
        raise DuckSetFormatError(f"unrecognised format {fmt!r}: expected \"duckset/N\"")
    major = int(match.group(1))
    if major != SUPPORTED_FORMAT_MAJOR:
        # Equality, not "<= supported": a future major means fields this
        # loader would silently drop. CLAUDE.md rule 4 wants a migration.
        raise DuckSetFormatError(
            f"unsupported format major {major}: this build reads duckset/{SUPPORTED_FORMAT_MAJOR}"
        )

    meta_raw = _require_dict(obj.get("meta", {}), "meta")
    name = meta_raw.get("name")
    if not isinstance(name, str):
        raise DuckSetFormatError("meta.name must be a string")
    meta = SetMeta(name=name, notes=_optional_str(meta_raw.get("notes"), "meta.notes"))

    entries_raw = obj.get("entries", [])
    if not isinstance(entries_raw, list):
        raise DuckSetFormatError(f"expected 'entries' to be a list, got {type(entries_raw).__name__}")
    entries = [_parse_entry(raw, i) for i, raw in enumerate(entries_raw)]

    return Setlist(format=fmt, meta=meta, entries=entries)


def load_setlist(path: Union[str, Path]) -> Setlist:
    return loads_setlist(Path(path).read_text(encoding="utf-8"))
