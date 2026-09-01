"""Semantic validation of a loaded Show against docs/duckshow-format.md.

validate() never raises for content problems; it returns a list of Issue
records (severity "error" or "warning") so callers -- CLI tools, tests,
the duck-agent's LOAD handler -- can decide what to do with them. Loader
errors (bad JSON, wrong format major version) are a separate, harder
failure raised by loader.py itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .limits import DEFAULT_LIMITS, Limits
from .model import Show
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
        if prev_t is not None:
            if kf.t < prev_t:
                _error(issues, role, track, kf.t, f"{track} keyframes are not sorted by t")
            elif kf.t == prev_t:
                _error(issues, role, track, kf.t, f"duplicate t={kf.t} in {track} track")
        prev_t = kf.t


def _check_scalar_limit(issues, role, track, t, name, value, limit) -> None:
    if abs(value) > limit + _EPS:
        _error(issues, role, track, t, f"{name}={value} exceeds limit of +/-{limit}")


def _check_range(issues, role, track, t, name, value, lo, hi) -> None:
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


def _check_mode_declared(issues, role, events, declared_modes) -> None:
    for e in events:
        if e.mode is not None and e.mode not in declared_modes:
            _warning(
                issues,
                role,
                "events",
                e.t,
                f"mode event references {e.mode!r}, not declared in requires.policies",
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


def validate(show: Show, limits: Limits = DEFAULT_LIMITS) -> list[Issue]:
    issues: list[Issue] = []

    declared_modes = {p.mode for p in show.requires.policies}

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

        _check_locomotion_limits(issues, role, tracks.locomotion, limits)
        _check_head_limits(issues, role, tracks.head, limits)
        _check_pose_limits(issues, role, tracks.pose, limits)
        _check_mouth_limits(issues, role, tracks.mouth, limits)

        for e in tracks.events:
            _check_event_action(issues, role, e)
        _check_event_density(issues, role, tracks.events, limits)
        _check_mode_declared(issues, role, tracks.events, declared_modes)
        _check_mode_locomotion_overlap(issues, role, show, tracks.events, limits)

    return issues
