"""duckset -- parse and validate .duckset/1 setlist files.

A setlist is an ordered list of shows with an end behaviour on each. See
docs/setlist-format.md for the format contract and for why chapter boundaries
live between shows rather than inside one.
"""

from __future__ import annotations

from .loader import (
    SUPPORTED_FORMAT_MAJOR,
    DuckSetFormatError,
    load_setlist,
    loads_setlist,
    parse_setlist,
)
from .model import DEFAULT_END, END_BEHAVIOURS, SHOW_SUFFIX, Entry, SetMeta, Setlist
from .validator import Issue, validate

__all__ = [
    "SUPPORTED_FORMAT_MAJOR",
    "DuckSetFormatError",
    "load_setlist",
    "loads_setlist",
    "parse_setlist",
    "DEFAULT_END",
    "END_BEHAVIOURS",
    "SHOW_SUFFIX",
    "Entry",
    "SetMeta",
    "Setlist",
    "Issue",
    "validate",
]
