"""Stage start marks -- mirrors editor/duckshow-viewer.js's
resolveMark/defaultMarkFor exactly (not reimplemented from memory: read
directly off that file, 2026-09-03). Kept as a tiny, obviously-correct
port rather than a shared module because the source is JS and this is
Python with no build step linking them -- if that file's algorithm ever
changes, this one needs updating too; there is no way around that split
without a cross-language build step this repo's rules forbid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_MARK_SPACING = 0.8


@dataclass(frozen=True)
class Mark:
    x: float
    y: float
    heading: float


def default_mark_for(index: int, total: int) -> Mark:
    y = (index - (total - 1) / 2.0) * DEFAULT_MARK_SPACING if total > 1 else 0.0
    return Mark(x=0.0, y=y, heading=0.0)


def _as_float(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f else 0.0  # NaN guard, mirrors JS `Number(x) || 0`


def get_mark(show_doc: dict, role: str) -> Mark:
    m = (show_doc.get("editor") or {}).get("marks", {}).get(role)
    if not isinstance(m, dict):
        m = {}
    return Mark(x=_as_float(m.get("x")), y=_as_float(m.get("y")), heading=_as_float(m.get("heading")))


def has_explicit_mark(show_doc: dict, role: str) -> bool:
    marks = (show_doc.get("editor") or {}).get("marks")
    return isinstance(marks, dict) and role in marks


def resolve_mark(show_doc: dict, role: str, index: int = 0, total: int = 1) -> Mark:
    if has_explicit_mark(show_doc, role):
        return get_mark(show_doc, role)
    return default_mark_for(index, total)
