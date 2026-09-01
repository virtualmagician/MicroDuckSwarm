"""Sample a Show's per-role curve tracks at an arbitrary show-time, and
resolve point events / mode / servo state at that time.

Interpolation semantics (docs/duckshow-format.md "Curve tracks"):
  - Keyframes are sorted by t (validated elsewhere; sampler assumes it).
  - `interp` on keyframe i describes the segment from keyframe i to i+1:
    "step" (hold i's value), "linear" (default), or "smooth" (smoothstep).
  - Booleans (pose.active) always step, regardless of the keyframe's
    `interp` value.
  - Before the first keyframe: hold the first keyframe's values.
  - After the last keyframe: hold the last keyframe's values -- except
    locomotion at/after meta.duration, which is always zeroed (playback
    ends at meta.duration "regardless of track contents").
  - A track with zero keyframes emits nothing: Sampler.at() leaves the
    corresponding Frame field as None so the agent sends no notification
    for it ("the duck's defaults rule").
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Optional, Sequence

from .model import Event, RoleTracks, Show

_EPS = 1e-9


def _smoothstep(x: float) -> float:
    x = 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)
    return x * x * (3.0 - 2.0 * x)


def _interp_scalar(v0: float, v1: float, frac: float, interp: str) -> float:
    if interp == "step":
        return v0
    if interp == "smooth":
        frac = _smoothstep(frac)
    else:  # "linear" and any unrecognized value fall back to linear
        frac = 0.0 if frac < 0.0 else (1.0 if frac > 1.0 else frac)
    return v0 + (v1 - v0) * frac


def _locate(keyframes: Sequence, t: float):
    """Returns ("hold", kf) if t is before the first or at/after the last
    keyframe, or ("segment", kf0, kf1, frac) for t strictly between two
    keyframes (or exactly on an interior keyframe, which yields frac=0).
    """
    if not keyframes:
        return None
    if t <= keyframes[0].t:
        return ("hold", keyframes[0])
    if t >= keyframes[-1].t:
        return ("hold", keyframes[-1])
    ts = [k.t for k in keyframes]
    i = bisect.bisect_right(ts, t) - 1
    kf0, kf1 = keyframes[i], keyframes[i + 1]
    span = kf1.t - kf0.t
    frac = 0.0 if span <= 0 else (t - kf0.t) / span
    return ("segment", kf0, kf1, frac)


def _sample_fields(keyframes: Sequence, t: float, fields: Sequence[tuple[str, bool]]) -> Optional[dict]:
    loc = _locate(keyframes, t)
    if loc is None:
        return None
    if loc[0] == "hold":
        kf = loc[1]
        return {name: getattr(kf, name) for name, _ in fields}
    _, kf0, kf1, frac = loc
    result = {}
    for name, is_bool in fields:
        v0 = getattr(kf0, name)
        if is_bool:
            result[name] = v0  # booleans always step: hold kf0's value until kf1 is reached
        else:
            result[name] = _interp_scalar(v0, getattr(kf1, name), frac, kf0.interp)
    return result


_LOCOMOTION_FIELDS = (("vx", False), ("vy", False), ("vyaw", False))
_HEAD_FIELDS = (("neck_pitch", False), ("head_pitch", False), ("head_yaw", False), ("head_roll", False))
_POSE_FIELDS = (("z", False), ("roll", False), ("pitch", False), ("active", True))
_MOUTH_FIELDS = (("open", False),)


@dataclass
class LocomotionSample:
    vx: float
    vy: float
    vyaw: float


@dataclass
class HeadSample:
    neck_pitch: float
    head_pitch: float
    head_yaw: float
    head_roll: float


@dataclass
class PoseSample:
    z: float
    roll: float
    pitch: float
    active: bool


@dataclass
class MouthSample:
    open: float


@dataclass
class Frame:
    t: float
    locomotion: Optional[LocomotionSample] = None
    head: Optional[HeadSample] = None
    pose: Optional[PoseSample] = None
    mouth: Optional[MouthSample] = None


class Sampler:
    """Samples one role's tracks from a loaded Show."""

    def __init__(self, show: Show, role: str):
        self.show = show
        self.role = role
        self.tracks: RoleTracks = show.tracks_for(role)

    # -- curve sampling --------------------------------------------------

    def at(self, t: float) -> Frame:
        locomotion = None
        if self.tracks.locomotion:
            vals = _sample_fields(self.tracks.locomotion, t, _LOCOMOTION_FIELDS)
            locomotion = LocomotionSample(**vals)

        duration = self.show.meta.duration
        if locomotion is not None and duration is not None and t >= duration:
            # meta.duration: "playback ends here regardless of track
            # contents (locomotion is zeroed and robot.stop is sent)".
            locomotion = LocomotionSample(vx=0.0, vy=0.0, vyaw=0.0)

        head = None
        if self.tracks.head:
            vals = _sample_fields(self.tracks.head, t, _HEAD_FIELDS)
            head = HeadSample(**vals)

        pose = None
        if self.tracks.pose:
            vals = _sample_fields(self.tracks.pose, t, _POSE_FIELDS)
            pose = PoseSample(**vals)

        mouth = None
        if self.tracks.mouth:
            vals = _sample_fields(self.tracks.mouth, t, _MOUTH_FIELDS)
            mouth = MouthSample(**vals)

        return Frame(t=t, locomotion=locomotion, head=head, pose=pose, mouth=mouth)

    # -- events ------------------------------------------------------------

    def events_between(self, t0: float, t1: float) -> list[Event]:
        """Events with t in (t0, t1], sorted by t -- i.e. tick-edge firing:
        an event exactly at t1 fires on this tick, one exactly at t0 fired
        on the *previous* tick and must not fire again.
        """
        return sorted((e for e in self.tracks.events if t0 < e.t <= t1), key=lambda e: e.t)

    def mode_at(self, t: float) -> Optional[str]:
        """The latest `mode` event with t <= the given time (for seek /
        late-join: the duck must already be in the right gait).
        """
        candidates = [e for e in self.tracks.events if e.mode is not None and e.t <= t]
        if not candidates:
            return None
        return max(candidates, key=lambda e: e.t).mode

    # -- servo (reserved in v1; only mode == "hold" is meaningful) --------

    def servo_at(self, t: float):
        """The servo track entry whose window contains `t`, or None.

        A window is [entry.t, entry.t + entry.duration) when `duration`
        is given, else it extends until the next servo entry (or forever).
        """
        entries = sorted(self.tracks.servo, key=lambda e: e.t)
        active = None
        for i, entry in enumerate(entries):
            if entry.t > t:
                break
            end = entry.t + entry.duration if entry.duration is not None else math.inf
            if i + 1 < len(entries):
                end = min(end, entries[i + 1].t)
            if entry.t <= t < end:
                active = entry
        return active
