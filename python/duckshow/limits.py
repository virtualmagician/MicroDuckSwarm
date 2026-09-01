"""Validation limits for .duckshow playback.

Plain data, per docs/duckshow-format.md ("Validation limits (conservative
defaults, tune on hardware)"). Kept as a dataclass rather than scattered
module constants so a future venue/hardware profile can override a subset
without touching validator.py.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    # Locomotion (m/s, rad/s)
    max_abs_vx: float = 0.25
    max_abs_vy: float = 0.20
    max_abs_vyaw: float = 1.5

    # Head angles (rad), applies to each of neck_pitch/head_pitch/head_yaw/head_roll
    max_abs_head_angle: float = 1.2

    # Pose (m / rad / rad)
    max_abs_pose_z: float = 0.05
    max_abs_pose_roll: float = 0.5
    max_abs_pose_pitch: float = 0.5

    # Mouth open, unitless 0..1
    min_mouth_open: float = 0.0
    max_mouth_open: float = 1.0

    # Discrete event track density: minimum seconds between two events
    # in the same role's events track.
    min_event_interval_s: float = 0.25

    # Mode-switch / locomotion overlap guard (docs/duckshow-format.md,
    # "Custom .onnx policies" section): warn when a `mode` event falls
    # within this many seconds of nonzero locomotion.
    mode_locomotion_guard_s: float = 0.5


DEFAULT_LIMITS = Limits()
