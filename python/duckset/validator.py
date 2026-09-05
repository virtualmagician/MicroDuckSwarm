"""Semantic validation of a loaded Setlist against docs/setlist-format.md.

Same contract as duckshow.validator: never raises for content problems,
returns Issue records so the caller decides. Loader errors (bad JSON, wrong
major) are raised by loader.py instead.

Two checks need the filesystem (does the show exist, does it validate) and two
need the shows themselves (cast changes between entries). Both are optional:
`validate(setlist)` alone checks only what the document can say about itself,
so a setlist authored on another machine still validates as a document. Pass
`repo_root` to also check what this machine holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .model import END_BEHAVIOURS, SHOW_SUFFIX, Setlist


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    entry: Optional[str]  # entry id, or None for document-level issues
    message: str


def _error(issues: list[Issue], entry, message):
    issues.append(Issue(severity="error", entry=entry, message=message))


def _warning(issues: list[Issue], entry, message):
    issues.append(Issue(severity="warning", entry=entry, message=message))


def _resolve(repo_root: Path, show: str) -> Optional[Path]:
    """Repo-root-relative show path -> absolute path, or None if it escapes.

    Mirrors resolve_show_path in scripts/editor_server.py. A setlist is
    editable text, so a path in one is untrusted input the moment anything
    opens what it names.
    """
    cleaned = show.strip().lstrip("/")
    if not cleaned:
        return None
    root = repo_root.resolve()
    candidate = (root / cleaned).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def validate(setlist: Setlist, repo_root: Optional[Path] = None) -> list[Issue]:
    issues: list[Issue] = []

    if not setlist.meta.name.strip():
        _error(issues, None, "meta.name is empty")

    seen: dict[str, int] = {}
    for index, entry in enumerate(setlist.entries):
        where = entry.id or f"entries[{index}]"

        if not entry.id.strip():
            _error(issues, where, "entry id is empty")
        elif entry.id in seen:
            _error(issues, where,
                   f"duplicate entry id {entry.id!r} (first used at entries[{seen[entry.id]}]); "
                   "ids must be unique so reordering and operator cues stay unambiguous")
        else:
            seen[entry.id] = index

        if entry.end not in END_BEHAVIOURS:
            _error(issues, where,
                   f"unknown end behaviour {entry.end!r}: expected one of {', '.join(END_BEHAVIOURS)}")

        show = entry.show.strip()
        if not show:
            _error(issues, where, "show path is empty")
            continue
        if not show.endswith(SHOW_SUFFIX):
            _error(issues, where, f"show path must end in {SHOW_SUFFIX}, got {entry.show!r}")
            continue
        if repo_root is not None and _resolve(repo_root, show) is None:
            _error(issues, where, f"show path escapes the repository root: {entry.show!r}")

    if setlist.entries and setlist.entries[-1].end == "continue":
        # Nothing to continue to, so it behaves as hold. Worth saying, because
        # the author probably reordered and did not revisit the last block.
        _warning(issues, setlist.entries[-1].id,
                 "the last entry ends in 'continue' with nothing after it, so it holds")

    if repo_root is not None:
        issues.extend(_check_against_disk(setlist, repo_root))
    return issues


def _check_against_disk(setlist: Setlist, repo_root: Path) -> list[Issue]:
    """Warnings that need the referenced show files. Never errors: a setlist
    naming a show this machine does not have is still a valid document."""
    from duckshow import DuckShowFormatError, load_show
    from duckshow import validate as validate_show

    issues: list[Issue] = []
    previous_cast: Optional[tuple[str, ...]] = None
    previous_id: Optional[str] = None

    for entry in setlist.entries:
        path = _resolve(repo_root, entry.show)
        if path is None:
            continue  # already an error above
        if not path.is_file():
            _warning(issues, entry.id, f"show file not found on this machine: {entry.show}")
            previous_cast = None
            continue
        try:
            show = load_show(path)
        except DuckShowFormatError as exc:
            _warning(issues, entry.id, f"{entry.show} does not load: {exc}")
            previous_cast = None
            continue

        errors = [i for i in validate_show(show) if i.severity == "error"]
        if errors:
            extra = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
            _warning(issues, entry.id, f"{entry.show} fails validation: {errors[0].message}{extra}")

        cast = tuple(show.role_names())
        if previous_cast is not None and cast != previous_cast:
            added = sorted(set(cast) - set(previous_cast))
            removed = sorted(set(previous_cast) - set(cast))
            detail = ", ".join(
                part for part in (
                    f"adds {', '.join(added)}" if added else "",
                    f"drops {', '.join(removed)}" if removed else "",
                ) if part
            ) or "reorders the cast"
            _warning(issues, entry.id,
                     f"cast changes from entry {previous_id!r}: {detail}. "
                     "The operator has to know which ducks the next entry needs.")
        previous_cast = cast
        previous_id = entry.id

    return issues
