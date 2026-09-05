"""Semantic validation of a loaded Show against docs/duckshow-format.md.

validate() never raises for content problems; it returns a list of Issue
records (severity "error" or "warning") so callers -- CLI tools, tests,
the duck-agent's LOAD handler -- can decide what to do with them. Loader
errors (bad JSON, wrong format major version) are a separate, harder
failure raised by loader.py itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .limits import CHAINING_SKILLS, DEFAULT_LIMITS, DRIVE_MODES, Limits, SKILLS, SOUND_TAGS, skill_duration_s
from .model import VALID_INTERPS, Show
from .sampler import Sampler

_EPS = 1e-9


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    role: Optional[str]
    track: Optional[str]
    t: Optional[float]
    message: str


def _error(issues: list[Issue], role, track, t, message):
    issues.append(Issue(severity="error", role=role, track=track, t=t, message=message))


def _warning(issues: list[Issue], role, track, t, message):
    issues.append(Issue(severity="warning", role=role, track=track, t=t, message=message))


def _check_sorted_unique(issues: list[Issue], role: str, track: str, keyframes) -> None:
    prev_t = None
    for kf in keyframes:
        if not math.isfinite(kf.t):
            _error(issues, role, track, kf.t, f"t={kf.t} is not a finite number")
        elif kf.t < 0:
            _error(issues, role, track, kf.t, f"{track} keyframe t={kf.t} must be >= 0")
        if prev_t is not None:
            if kf.t < prev_t:
                _error(issues, role, track, kf.t, f"{track} keyframes are not sorted by t")
            elif kf.t == prev_t:
                _error(issues, role, track, kf.t, f"duplicate t={kf.t} in {track} track")
        prev_t = kf.t


def _check_interp_valid(issues: list[Issue], role: str, track: str, keyframes) -> None:
    for kf in keyframes:
        if kf.interp not in VALID_INTERPS:
            _error(
                issues,
                role,
                track,
                kf.t,
                f"{track} keyframe interp={kf.interp!r} is not one of {VALID_INTERPS}",
            )


def _check_scalar_limit(issues, role, track, t, name, value, limit) -> None:
    if not math.isfinite(value):
        _error(issues, role, track, t, f"{name}={value} is not a finite number")
        return
    if abs(value) > limit + _EPS:
        _error(issues, role, track, t, f"{name}={value} exceeds limit of +/-{limit}")


def _check_range(issues, role, track, t, name, value, lo, hi) -> None:
    if not math.isfinite(value):
        _error(issues, role, track, t, f"{name}={value} is not a finite number")
        return
    if value < lo - _EPS or value > hi + _EPS:
        _error(issues, role, track, t, f"{name}={value} outside allowed range [{lo}, {hi}]")


def _check_locomotion_limits(issues, role, keyframes, limits: Limits) -> None:
    for kf in keyframes:
        _check_scalar_limit(issues, role, "locomotion", kf.t, "vx", kf.vx, limits.max_abs_vx)
        _check_scalar_limit(issues, role, "locomotion", kf.t, "vy", kf.vy, limits.max_abs_vy)
        _check_scalar_limit(issues, role, "locomotion", kf.t, "vyaw", kf.vyaw, limits.max_abs_vyaw)


def _check_head_limits(issues, role, keyframes, limits: Limits) -> None:
    for kf in keyframes:
        for name in ("neck_pitch", "head_pitch", "head_yaw", "head_roll"):
            _check_scalar_limit(issues, role, "head", kf.t, name, getattr(kf, name), limits.max_abs_head_angle)


def _check_pose_limits(issues, role, keyframes, limits: Limits) -> None:
    for kf in keyframes:
        _check_scalar_limit(issues, role, "pose", kf.t, "z", kf.z, limits.max_abs_pose_z)
        _check_scalar_limit(issues, role, "pose", kf.t, "roll", kf.roll, limits.max_abs_pose_roll)
        _check_scalar_limit(issues, role, "pose", kf.t, "pitch", kf.pitch, limits.max_abs_pose_pitch)


def _check_mouth_limits(issues, role, keyframes, limits: Limits) -> None:
    for kf in keyframes:
        _check_range(issues, role, "mouth", kf.t, "open", kf.open, limits.min_mouth_open, limits.max_mouth_open)


def _check_event_density(issues, role, events, limits: Limits) -> None:
    ordered = sorted(events, key=lambda e: e.t)
    prev_t = None
    for e in ordered:
        if prev_t is not None and (e.t - prev_t) < limits.min_event_interval_s - _EPS:
            _error(
                issues,
                role,
                "events",
                e.t,
                f"event at t={e.t} is less than {limits.min_event_interval_s}s after previous event at t={prev_t}",
            )
        prev_t = e.t


def _check_event_action(issues, role, e) -> None:
    kind = e.action_kind()
    present = [k for k in ("do", "sound", "mode") if getattr(e, k) is not None]
    if len(present) == 0:
        _error(issues, role, "events", e.t, "event has no action key (one of do/sound/mode required)")
    elif len(present) > 1:
        _error(issues, role, "events", e.t, f"event has more than one action key: {present}")
    if e.do is not None and e.do not in SKILLS:
        _error(issues, role, "events", e.t, f"do={e.do!r} is not a recognized skill (expected one of {SKILLS})")
    if e.sound is not None and e.sound not in SOUND_TAGS:
        _error(
            issues,
            role,
            "events",
            e.t,
            f"sound={e.sound!r} is not a recognized sound tag (expected one of {SOUND_TAGS})",
        )


def _check_event_fields(issues, role, e) -> None:
    if not math.isfinite(e.t):
        _error(issues, role, "events", e.t, f"t={e.t} is not a finite number")
    elif e.t < 0:
        _error(issues, role, "events", e.t, f"event t={e.t} must be >= 0")
    if e.hold is not None and not math.isfinite(e.hold):
        _error(issues, role, "events", e.t, f"hold={e.hold} is not a finite number")


def _check_mode_value(issues, role, events) -> None:
    """A `mode` event's value must be a real robotd drive mode -- real
    hardware accepts exactly "walk" or "roller" over the wire and has no
    mechanism to register a custom-named mode (docs/robotd-api.md
    "Custom .onnx policies & modes"; docs/duckshow-format.md "Custom
    .onnx policies"). A custom-trained gait is installed by pointing a
    fixed policy *slot* at a different .onnx file (requires.policies[]),
    never by inventing a new mode string, so `requires.policies` plays
    no part in whether a `mode` event is valid.
    """
    for e in events:
        if e.mode is not None and e.mode not in DRIVE_MODES:
            _error(
                issues,
                role,
                "events",
                e.t,
                f"mode={e.mode!r} is not a valid drive mode (expected one of {DRIVE_MODES})",
            )


def _locomotion_nonzero_in_window(sampler: Sampler, lo: float, hi: float) -> bool:
    times = {lo, hi}
    for kf in sampler.tracks.locomotion:
        if lo <= kf.t <= hi:
            times.add(kf.t)
    for tt in sorted(times):
        frame = sampler.at(tt)
        if frame.locomotion is None:
            continue
        v = frame.locomotion
        if abs(v.vx) > _EPS or abs(v.vy) > _EPS or abs(v.vyaw) > _EPS:
            return True
    return False


def _check_mode_locomotion_overlap(issues, role, show: Show, events, limits: Limits) -> None:
    if not events:
        return
    sampler = Sampler(show, role)
    guard = limits.mode_locomotion_guard_s
    for e in events:
        if e.mode is None:
            continue
        lo = max(0.0, e.t - guard)
        hi = e.t + guard
        if _locomotion_nonzero_in_window(sampler, lo, hi):
            _warning(
                issues,
                role,
                "events",
                e.t,
                f"mode event {e.mode!r} at t={e.t} overlaps nonzero locomotion within +/-{guard}s",
            )


def _check_skill_occupancy_overlap(issues, role, show: Show, events) -> None:
    """A `do` skill runs its whole episodic clip to completion once
    started (docs/duckshow-format.md's "Skill durations and occupancy",
    sourced from assets/microduck/policies/manifest.json's duration_s
    figures) -- unlike `_check_event_density`'s 0.25 s spacing rule
    (about command flooding and applying to every discrete event, any
    type), scheduling a second skill *inside* that window schedules
    against a duck that physically cannot have finished the first one
    yet. The robot will still accept the command and something will
    happen, so this is a WARNING, not an error -- nothing here is
    unsafe, the author just probably did not mean it.

    Only consecutive pairs of `do` events are compared (mirroring
    `_check_event_density`'s prev_t walk), each against the skill
    immediately before it in time order -- not every earlier skill.
    `roulade` is "chain": true in the manifest: a `roulade` immediately
    following a `roulade` is the documented way to keep rolling, not two
    skills contending for one window (limits.CHAINING_SKILLS), so that
    specific pairing never warns. `sit_toggle` has no confirmed duration
    (limits.skill_duration_s returns None for it) and so never opens a
    window here. It can still be the later, interrupting event.
    """
    skill_events = sorted((e for e in events if e.do is not None), key=lambda e: e.t)
    if len(skill_events) < 2:
        return
    sampler = Sampler(show, role)
    prev = skill_events[0]
    for cur in skill_events[1:]:
        if not (prev.do in CHAINING_SKILLS and cur.do == prev.do):
            duration = skill_duration_s(prev.do, sampler.mode_at(prev.t))
            if duration is not None:
                end = prev.t + duration
                if cur.t < end - _EPS:
                    overlap = end - cur.t
                    _warning(
                        issues,
                        role,
                        "events",
                        cur.t,
                        f"do={cur.do!r} at t={cur.t} begins {overlap}s into the {duration}s "
                        f"execution of do={prev.do!r} at t={prev.t}",
                    )
        prev = cur


def _check_meta_duration(issues: list[Issue], show: Show) -> None:
    """meta.duration is not optional in practice: docs/duckshow-format.md
    documents real playback-ending semantics for it ("playback ends here
    regardless of track contents ... locomotion is zeroed and robot.stop
    is sent"), unlike meta.music which is explicitly optional. A missing
    duration means that safety behavior never runs (the sampler only
    zeroes locomotion when duration is not None); zero/negative means it
    runs on the very first tick.
    """
    duration = show.meta.duration
    if duration is None:
        _error(issues, None, None, None, "meta.duration is required")
        return
    if not math.isfinite(duration) or duration <= 0:
        _error(issues, None, None, None, f"meta.duration={duration} must be a finite number > 0")


def _check_servo(issues: list[Issue], role: str, entries) -> None:
    """Servo track is "reserved in v1": docs/duckshow-format.md says only
    `{"mode": "hold"}` is honored by v1 agents, and sampler.servo_at's
    no-`duration` window semantics ("extends until the next servo entry,
    or forever") aren't spelled out anywhere an author can see them
    before load. Neither is a format-contract violation (nothing here
    disagrees with the doc), but both are silent authoring footguns
    worth a preflight diagnostic.
    """
    for e in entries:
        if not math.isfinite(e.t):
            _error(issues, role, "servo", e.t, f"t={e.t} is not a finite number")
        elif e.t < 0:
            _error(issues, role, "servo", e.t, f"servo t={e.t} must be >= 0")
        if e.duration is not None:
            if not math.isfinite(e.duration):
                _error(issues, role, "servo", e.t, f"duration={e.duration} is not a finite number")
            elif e.duration <= 0:
                _error(issues, role, "servo", e.t, f"servo duration={e.duration} must be > 0")
        if e.mode != "hold":
            _warning(
                issues,
                role,
                "servo",
                e.t,
                f"servo mode {e.mode!r} is not honored by v1 agents (only 'hold' has any effect)",
            )


def validate(show: Show, limits: Limits = DEFAULT_LIMITS) -> list[Issue]:
    issues: list[Issue] = []

    _check_meta_duration(issues, show)

    for member in show.cast:
        role = member.role
        if role not in show.tracks:
            _error(issues, role, None, None, f"cast role {role!r} has no tracks entry")
            continue

        tracks = show.tracks[role]

        _check_sorted_unique(issues, role, "locomotion", tracks.locomotion)
        _check_sorted_unique(issues, role, "head", tracks.head)
        _check_sorted_unique(issues, role, "pose", tracks.pose)
        _check_sorted_unique(issues, role, "mouth", tracks.mouth)

        _check_interp_valid(issues, role, "locomotion", tracks.locomotion)
        _check_interp_valid(issues, role, "head", tracks.head)
        _check_interp_valid(issues, role, "pose", tracks.pose)
        _check_interp_valid(issues, role, "mouth", tracks.mouth)

        _check_locomotion_limits(issues, role, tracks.locomotion, limits)
        _check_head_limits(issues, role, tracks.head, limits)
        _check_pose_limits(issues, role, tracks.pose, limits)
        _check_mouth_limits(issues, role, tracks.mouth, limits)

        for e in tracks.events:
            _check_event_action(issues, role, e)
            _check_event_fields(issues, role, e)
        _check_event_density(issues, role, tracks.events, limits)
        _check_mode_value(issues, role, tracks.events)
        _check_mode_locomotion_overlap(issues, role, show, tracks.events, limits)
        _check_skill_occupancy_overlap(issues, role, show, tracks.events)

        _check_servo(issues, role, tracks.servo)

    return issues
