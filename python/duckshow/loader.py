"""Load and parse .duckshow/1 JSON documents into the model.py dataclasses.

Per docs/duckshow-format.md: unknown JSON fields are ignored everywhere
(forward compatibility); parsers reject unknown *major* format versions
with a clear error. This module does no semantic validation (limits,
sorting, spacing, ...) -- that is validator.py's job, run separately so
callers can choose to load-but-report-issues rather than hard-fail.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional, Union

from .model import (
    CastMember,
    Event,
    HeadKeyframe,
    LocomotionKeyframe,
    Meta,
    Music,
    MouthKeyframe,
    PolicyRequirement,
    PoseKeyframe,
    Requires,
    RoleTracks,
    ServoEvent,
    Show,
    DEFAULT_INTERP,
)

SUPPORTED_FORMAT_MAJOR = 1
_FORMAT_RE = re.compile(r"^duckshow/(\d+)$")


class DuckShowFormatError(ValueError):
    """Raised for anything that keeps a document from being loaded at
    all: missing/malformed `format`, an unsupported major version, or a
    document that isn't a JSON object.
    """


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise DuckShowFormatError(f"expected {field_name!r} to be a string, got {type(value).__name__}")
    return value


def _parse_music(d: Optional[dict[str, Any]]) -> Optional[Music]:
    if d is None:
        return None
    if not isinstance(d, dict):
        raise DuckShowFormatError("meta.music must be an object")
    return Music(
        file=d.get("file"),
        bpm=d.get("bpm"),
        beat_offset=float(d.get("beat_offset", 0.0)),
    )


def _parse_meta(d: dict[str, Any]) -> Meta:
    return Meta(
        name=d.get("name"),
        author=d.get("author"),
        created=d.get("created"),
        duration=(float(d["duration"]) if d.get("duration") is not None else None),
        music=_parse_music(d.get("music")),
    )


def _parse_policy(d: dict[str, Any]) -> PolicyRequirement:
    return PolicyRequirement(
        name=_require_str(d.get("name"), "requires.policies[].name"),
        mode=_require_str(d.get("mode"), "requires.policies[].mode"),
        file=_require_str(d.get("file"), "requires.policies[].file"),
        sha256=_require_str(d.get("sha256"), "requires.policies[].sha256"),
        slot=d.get("slot"),
    )


def _parse_requires(d: Optional[dict[str, Any]]) -> Requires:
    if d is None:
        return Requires()
    policies_raw = d.get("policies") or []
    return Requires(policies=[_parse_policy(p) for p in policies_raw])


def _parse_cast(raw: list[Any]) -> list[CastMember]:
    cast = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise DuckShowFormatError("each cast entry must be an object")
        cast.append(CastMember(role=_require_str(entry.get("role"), "cast[].role"), notes=entry.get("notes")))
    return cast


def _interp(d: dict[str, Any]) -> str:
    return d.get("interp", DEFAULT_INTERP)


def _parse_locomotion_kf(d: dict[str, Any]) -> LocomotionKeyframe:
    return LocomotionKeyframe(
        t=float(d["t"]),
        vx=float(d.get("vx", 0.0)),
        vy=float(d.get("vy", 0.0)),
        vyaw=float(d.get("vyaw", 0.0)),
        interp=_interp(d),
    )


def _parse_head_kf(d: dict[str, Any]) -> HeadKeyframe:
    return HeadKeyframe(
        t=float(d["t"]),
        neck_pitch=float(d.get("neck_pitch", 0.0)),
        head_pitch=float(d.get("head_pitch", 0.0)),
        head_yaw=float(d.get("head_yaw", 0.0)),
        head_roll=float(d.get("head_roll", 0.0)),
        interp=_interp(d),
    )


def _parse_pose_kf(d: dict[str, Any]) -> PoseKeyframe:
    return PoseKeyframe(
        t=float(d["t"]),
        z=float(d.get("z", 0.0)),
        roll=float(d.get("roll", 0.0)),
        pitch=float(d.get("pitch", 0.0)),
        active=bool(d.get("active", False)),
        interp=_interp(d),
    )


def _parse_mouth_kf(d: dict[str, Any]) -> MouthKeyframe:
    return MouthKeyframe(t=float(d["t"]), open=float(d.get("open", 0.0)), interp=_interp(d))


def _parse_event(d: dict[str, Any]) -> Event:
    return Event(
        t=float(d["t"]),
        do=d.get("do"),
        sound=d.get("sound"),
        hold=(float(d["hold"]) if d.get("hold") is not None else None),
        mode=d.get("mode"),
    )


def _parse_servo_event(d: dict[str, Any]) -> ServoEvent:
    return ServoEvent(
        t=float(d["t"]),
        mode=d.get("mode", "hold"),
        duration=(float(d["duration"]) if d.get("duration") is not None else None),
        target=d.get("target"),
    )


def _parse_role_tracks(d: dict[str, Any]) -> RoleTracks:
    return RoleTracks(
        locomotion=[_parse_locomotion_kf(k) for k in d.get("locomotion") or []],
        head=[_parse_head_kf(k) for k in d.get("head") or []],
        pose=[_parse_pose_kf(k) for k in d.get("pose") or []],
        mouth=[_parse_mouth_kf(k) for k in d.get("mouth") or []],
        events=[_parse_event(k) for k in d.get("events") or []],
        servo=[_parse_servo_event(k) for k in d.get("servo") or []],
    )


def _parse_tracks(raw: dict[str, Any]) -> dict[str, RoleTracks]:
    tracks: dict[str, RoleTracks] = {}
    for role, d in raw.items():
        if not isinstance(d, dict):
            raise DuckShowFormatError(f"tracks[{role!r}] must be an object")
        tracks[role] = _parse_role_tracks(d)
    return tracks


def _check_format_version(fmt: Any) -> None:
    if not isinstance(fmt, str):
        raise DuckShowFormatError(f"missing or non-string top-level 'format' field: {fmt!r}")
    m = _FORMAT_RE.match(fmt)
    if not m:
        raise DuckShowFormatError(
            f"unrecognized 'format' field {fmt!r}; expected 'duckshow/<major>'"
        )
    major = int(m.group(1))
    if major != SUPPORTED_FORMAT_MAJOR:
        raise DuckShowFormatError(
            f"unsupported duckshow format major version {major}; "
            f"this loader only supports duckshow/{SUPPORTED_FORMAT_MAJOR}"
        )


def loads_show(text: str) -> Show:
    """Parse a .duckshow document from a JSON string."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DuckShowFormatError(f"invalid JSON: {exc}") from exc

    if not isinstance(doc, dict):
        raise DuckShowFormatError("top-level .duckshow document must be a JSON object")

    _check_format_version(doc.get("format"))

    meta = _parse_meta(doc.get("meta") or {})
    requires = _parse_requires(doc.get("requires"))
    cast = _parse_cast(doc.get("cast") or [])
    tracks = _parse_tracks(doc.get("tracks") or {})

    return Show(format=doc["format"], meta=meta, requires=requires, cast=cast, tracks=tracks)


def load_show(path: Union[str, Path]) -> Show:
    """Parse a .duckshow document from a file path."""
    text = Path(path).read_text(encoding="utf-8")
    return loads_show(text)
