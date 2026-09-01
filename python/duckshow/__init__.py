"""duckshow -- parse, validate, and sample .duckshow/1 show files.

Shared by duck_agent, the tools/ master CLI, and mock_duck-facing tests.
See docs/duckshow-format.md for the format contract.
"""

from __future__ import annotations

from .limits import DEFAULT_LIMITS, Limits
from .loader import DuckShowFormatError, load_show, loads_show
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
)
from .sampler import Frame, HeadSample, LocomotionSample, MouthSample, PoseSample, Sampler
from .validator import Issue, validate

__all__ = [
    "DEFAULT_LIMITS",
    "Limits",
    "DuckShowFormatError",
    "load_show",
    "loads_show",
    "CastMember",
    "Event",
    "HeadKeyframe",
    "LocomotionKeyframe",
    "Meta",
    "Music",
    "MouthKeyframe",
    "PolicyRequirement",
    "PoseKeyframe",
    "Requires",
    "RoleTracks",
    "ServoEvent",
    "Show",
    "Frame",
    "HeadSample",
    "LocomotionSample",
    "MouthSample",
    "PoseSample",
    "Sampler",
    "Issue",
    "validate",
]
