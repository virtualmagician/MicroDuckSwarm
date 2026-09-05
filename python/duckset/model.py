"""Dataclasses for a loaded .duckset/1 document. See docs/setlist-format.md."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

#: docs/setlist-format.md, "End behaviours". `hold` is the default because the
#: gap where the cast gets picked up and repositioned is what a setlist is for.
END_BEHAVIOURS = ("hold", "loop", "continue")
DEFAULT_END = "hold"

SHOW_SUFFIX = ".duckshow.json"


@dataclass
class SetMeta:
    name: str
    notes: Optional[str] = None


@dataclass
class Entry:
    #: Unique within the setlist and stable across reordering. Not the show:
    #: the same show can appear twice in one set (a reprise).
    id: str
    #: Repo-root-relative, leading "/", ending in .duckshow.json. A path, not a
    #: show id: a setlist names files on the machine that edits it, and the
    #: master reads the id out of the file when it loads one.
    show: str
    end: str = DEFAULT_END
    #: Display name for the block. None means "use the show's meta.name".
    label: Optional[str] = None


@dataclass
class Setlist:
    format: str
    meta: SetMeta
    entries: list[Entry] = field(default_factory=list)

    def entry_ids(self) -> list[str]:
        return [e.id for e in self.entries]

    def show_paths(self) -> list[str]:
        """Every referenced show path, in order, with duplicates kept: a set
        that plays one show twice loads it twice."""
        return [e.show for e in self.entries]
