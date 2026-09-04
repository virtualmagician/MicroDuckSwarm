"""Validation limits for .duckshow playback.

Plain data, per docs/duckshow-format.md ("Validation limits (conservative
defaults, tune on hardware)"). Kept as a dataclass rather than scattered
module constants so a future venue/hardware profile can override a subset
without touching validator.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Closed enums from docs/duckshow-format.md's "Event track" table -- these
# mirror robotd-api.md's Skill / SoundTag enums (docs/robotd-api.md), so an
# event referencing anything outside these sets can never succeed on real
# hardware. Kept here as data, per "Limits live in
# python/duckshow/limits.py as data, not scattered constants."
SKILLS = (
    "ground_pick",
    "kick_left",
    "kick_right",
    "sit_toggle",
    "roulade",
)

SOUND_TAGS = (
    "alarm",
    "greet",
    "inquire",
    "peck",
    "chirp",
    "coo",
    "wheee",
)

# The only two drive-mode strings real robotd accepts over the wire
# (docs/robotd-api.md "Custom .onnx policies & modes"). There is no
# mechanism to register a custom-named mode -- a custom-trained gait is
# installed by pointing a fixed policy *slot* at a different .onnx file
# (requires.policies[].slot), never by inventing a new mode string. A
# `mode` event's value must be one of these two.
DRIVE_MODES = (
    "walk",
    "roller",
)

# Per-skill occupancy durations (seconds), sourced from
# assets/microduck/policies/manifest.json (schema_version 2, control_hz
# 50; see docs/duckshow-format.md's "Skill durations and occupancy" for
# the full authoring mapping table). Each of these `do` skills is an
# *episodic* policy clip: once started, it runs to completion -- a
# discrete event scheduled inside that window is scheduling against a
# duck that physically cannot have finished the first skill yet (see
# validator.py's _check_skill_occupancy_overlap). This is a different
# concern from `min_event_interval_s` below (command flooding).
#
# `sit_toggle` (alpha_sitstand.onnx) is deliberately absent: the
# manifest marks it "kind": "scripted", not "episodic", and gives it a
# ramp_s/unwind_s posture transition rather than a fixed duration_s --
# docs/bake-format.md records that the hand-off semantics for a second
# sit_toggle mid-ramp are unverified (shows/octet/octet.duckshow.json's
# `reed` role fires two 2.0 s apart on purpose, to exercise exactly that
# unresolved case). There is no confirmed number to warn against, so
# sit_toggle never occupies for the purposes of this check -- neither as
# the earlier (occupying) skill nor as the later (interrupting) one.
SKILL_DURATIONS_S = {
    "ground_pick": 2.8,  # alpha_ground_pick.onnx, walk-mode duration
    "roulade": 1.0,  # roulade.onnx
    "kick_left": 0.5,  # ball_kick_left.onnx
    "kick_right": 0.5,  # ball_kick_right.onnx
}

# ground_pick's occupancy in roller mode: the robot runs roller_crouch.onnx
# instead of alpha_ground_pick.onnx (docs/duckshow-format.md's authoring
# mapping table names roller_crouch as "the roller-mode variant of ground
# pick", never itself authored directly by a `do` event) -- a longer clip,
# not just a renamed one. Which mode is "in effect" for a given
# ground_pick event is resolved from the mode event(s) preceding it, the
# same rule late-join/seek uses for gait (Sampler.mode_at).
GROUND_PICK_ROLLER_DURATION_S = 3.5

# Skills whose manifest.json entry is "chain": true -- manifest.json
# marks roulade.onnx this way, and docs/bake-format.md's own reading is
# "chained to something else the manifest doesn't specify": a repeat of
# one of these immediately after itself is the documented way to keep
# the effect going, not an authoring mistake, so the occupancy-overlap
# check below must never warn about that specific pairing.
CHAINING_SKILLS = ("roulade",)


def skill_duration_s(skill: str, mode: Optional[str]) -> Optional[float]:
    """Occupancy duration (seconds) for a `do` skill event, given the
    drive mode active when it starts (`mode` is a `Sampler.mode_at()`
    result: `"walk"`, `"roller"`, or `None` when no `mode` event
    precedes it). Returns `None` when no confirmed duration exists
    (currently only `sit_toggle` -- see `SKILL_DURATIONS_S` above).
    """
    if skill == "ground_pick" and mode == "roller":
        return GROUND_PICK_ROLLER_DURATION_S
    return SKILL_DURATIONS_S.get(skill)


@dataclass(frozen=True)
class Limits:
    # Locomotion (m/s, rad/s).
    #
    # 0.40 is the edge of alpha_walking.onnx's own training distribution
    # (microduck_rl sampled lin_vel_x uniformly from (-0.4, 0.4)), not an
    # arbitrary loosening. The previous 0.25/0.20 were picked as cautious
    # stage speeds before anything was measured; measurement showed they sat
    # BELOW the policy's stand/walk gate on three of four axes (backward
    # -0.326, lateral 0.312), so they did not produce slow motion, they
    # produced none -- while the show still validated clean. See
    # docs/duckshow-format.md "Why the translation limits are 0.40" and
    # docs/bake-format.md "The low-speed problem".
    #
    # vyaw stays 1.5: already above its 1.047 rad/s gate, already usable.
    #
    # Measured in the baker's simulated plant, NOT on hardware. Retune from a
    # real duck on day one (ramp each axis, record where stepping begins).
    max_abs_vx: float = 0.40
    max_abs_vy: float = 0.40
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
