"""Dataclasses for the .duckshow/1 document model.

These are pure data holders -- no parsing logic lives here (see loader.py)
and no interpolation logic lives here (see sampler.py). Every dataclass
accepts exactly the fields defined in docs/duckshow-format.md; the loader
is responsible for ignoring unknown JSON fields when it builds these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Interpolation mode applied "from this keyframe to the next".
INTERP_STEP = "step"
INTERP_LINEAR = "linear"
INTERP_SMOOTH = "smooth"
VALID_INTERPS = (INTERP_STEP, INTERP_LINEAR, INTERP_SMOOTH)
DEFAULT_INTERP = INTERP_LINEAR


@dataclass
class Music:
    file: Optional[str] = None
    bpm: Optional[float] = None
    beat_offset: float = 0.0


@dataclass
class Meta:
    name: Optional[str] = None
    author: Optional[str] = None
    created: Optional[str] = None
    duration: Optional[float] = None
    music: Optional[Music] = None


@dataclass
class PolicyRequirement:
    name: str
    mode: str
    file: str
    sha256: str
    slot: Optional[str] = None


@dataclass
class Requires:
    policies: list[PolicyRequirement] = field(default_factory=list)


@dataclass
class CastMember:
    role: str
    notes: Optional[str] = None


@dataclass
class LocomotionKeyframe:
    t: float
    vx: float = 0.0
    vy: float = 0.0
    vyaw: float = 0.0
    interp: str = DEFAULT_INTERP


@dataclass
class HeadKeyframe:
    t: float
    neck_pitch: float = 0.0
    head_pitch: float = 0.0
    head_yaw: float = 0.0
    head_roll: float = 0.0
    interp: str = DEFAULT_INTERP


@dataclass
class PoseKeyframe:
    t: float
    z: float = 0.0
    roll: float = 0.0
    pitch: float = 0.0
    active: bool = False
    interp: str = DEFAULT_INTERP


@dataclass
class MouthKeyframe:
    t: float
    open: float = 0.0
    interp: str = DEFAULT_INTERP


@dataclass
class Event:
    """A point event. Exactly one of do/sound/mode should be set (the
    loader preserves whatever was present in the JSON; the validator is
    responsible for flagging anything malformed).
    """

    t: float
    do: Optional[str] = None
    sound: Optional[str] = None
    hold: Optional[float] = None
    mode: Optional[str] = None

    def action_kind(self) -> Optional[str]:
        if self.do is not None:
            return "do"
        if self.sound is not None:
            return "sound"
        if self.mode is not None:
            return "mode"
        return None


@dataclass
class ServoEvent:
    """Reserved-in-v1 servo track entry. Only mode == "hold" is honored
    by v1 agents (freeze locomotion for `duration` seconds; head/pose/
    mouth curve tracks keep playing normally).
    """

    t: float
    mode: str = "hold"
    duration: Optional[float] = None
    target: Optional[str] = None


@dataclass
class RoleTracks:
    locomotion: list[LocomotionKeyframe] = field(default_factory=list)
    head: list[HeadKeyframe] = field(default_factory=list)
    pose: list[PoseKeyframe] = field(default_factory=list)
    mouth: list[MouthKeyframe] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    servo: list[ServoEvent] = field(default_factory=list)


@dataclass
class Show:
    format: str
    meta: Meta
    requires: Requires
    cast: list[CastMember]
    tracks: dict[str, RoleTracks] = field(default_factory=dict)

    def role_names(self) -> list[str]:
        return [c.role for c in self.cast]

    def tracks_for(self, role: str) -> RoleTracks:
        """Tracks for `role`, or an all-empty RoleTracks if the show has
        no entry for it (idle stand -- see docs/duckshow-format.md).
        """
        return self.tracks.get(role, RoleTracks())
