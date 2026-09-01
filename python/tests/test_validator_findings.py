"""Regression tests for python/duckshow/validator.py additions from the
CLAUDE.md findings pass: closed do/sound enums (F36), interp enum (F39),
meta.duration requirement (F40), t >= 0 for keyframes/events (F44), and
servo track diagnostics (F48). See docs/duckshow-format.md for the
underlying contract each check enforces.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duckshow.model import (  # noqa: E402
    CastMember,
    Event,
    HeadKeyframe,
    LocomotionKeyframe,
    Meta,
    Requires,
    RoleTracks,
    Show,
    ServoEvent,
)
from duckshow.validator import validate  # noqa: E402


def _issues_by_message_substr(issues, substr):
    return [i for i in issues if substr in i.message]


def _one_role_show(**overrides):
    meta = overrides.pop("meta", Meta(duration=10.0))
    tracks_kwargs = overrides
    return Show(
        format="duckshow/1",
        meta=meta,
        requires=Requires(),
        cast=[CastMember(role="lead")],
        tracks={"lead": RoleTracks(**tracks_kwargs)},
    )


class EventActionEnumTest(unittest.TestCase):
    """F36: `do`/`sound` are closed enums (docs/duckshow-format.md's event
    table, mirroring robotd-api.md's Skill/SoundTag); a typo must be an
    error, not silently accepted.
    """

    def test_unknown_skill_is_error(self) -> None:
        show = _one_role_show(events=[Event(t=1.0, do="kick_lef")])
        issues = validate(show)
        self.assertTrue(_issues_by_message_substr(issues, "not a recognized skill"))

    def test_unknown_sound_is_error(self) -> None:
        show = _one_role_show(events=[Event(t=1.0, sound="quack")])
        issues = validate(show)
        self.assertTrue(_issues_by_message_substr(issues, "not a recognized sound tag"))

    def test_known_skill_and_sound_pass(self) -> None:
        show = _one_role_show(events=[Event(t=1.0, do="kick_left"), Event(t=2.0, sound="chirp")])
        issues = validate(show)
        self.assertEqual([i for i in issues if i.severity == "error"], [])


class InterpEnumTest(unittest.TestCase):
    """F39: an unrecognized `interp` value silently samples as linear
    (sampler.py's fallback) with no validator diagnostic -- fix adds one.
    """

    def test_unrecognized_interp_is_error(self) -> None:
        show = _one_role_show(
            head=[HeadKeyframe(t=0.0, interp="Smooth"), HeadKeyframe(t=2.0, head_pitch=1.0)]
        )
        issues = validate(show)
        self.assertTrue(_issues_by_message_substr(issues, "is not one of"))

    def test_known_interp_values_pass(self) -> None:
        for interp in ("step", "linear", "smooth"):
            show = _one_role_show(
                head=[HeadKeyframe(t=0.0, interp=interp), HeadKeyframe(t=2.0, head_pitch=1.0)]
            )
            issues = validate(show)
            self.assertEqual([i for i in issues if i.severity == "error"], [], f"interp={interp!r}")


class MetaDurationTest(unittest.TestCase):
    """F40: meta.duration drives the documented end-of-show safety
    behavior (locomotion zeroed, robot.stop sent); missing/zero/negative
    must be a hard error.
    """

    def test_missing_duration_is_error(self) -> None:
        show = _one_role_show(meta=Meta(duration=None))
        issues = validate(show)
        self.assertTrue(any("meta.duration" in i.message and i.severity == "error" for i in issues))

    def test_zero_duration_is_error(self) -> None:
        show = _one_role_show(meta=Meta(duration=0.0))
        issues = validate(show)
        self.assertTrue(any("meta.duration" in i.message and i.severity == "error" for i in issues))

    def test_negative_duration_is_error(self) -> None:
        show = _one_role_show(meta=Meta(duration=-5.0))
        issues = validate(show)
        self.assertTrue(any("meta.duration" in i.message and i.severity == "error" for i in issues))

    def test_positive_duration_no_duration_error(self) -> None:
        show = _one_role_show(meta=Meta(duration=10.0))
        issues = validate(show)
        self.assertFalse(any("meta.duration" in i.message for i in issues))


class NegativeTimeTest(unittest.TestCase):
    """F44: docs/duckshow-format.md requires keyframe/event `t` to be
    >= 0; a negative t silently never fires (events_between starts at the
    play-start time) with no author-facing diagnostic.
    """

    def test_negative_keyframe_t_is_error(self) -> None:
        show = _one_role_show(
            head=[HeadKeyframe(t=-3.0, head_yaw=0.5), HeadKeyframe(t=5.0)]
        )
        issues = validate(show)
        self.assertTrue(any("must be >= 0" in i.message and i.track == "head" for i in issues))

    def test_negative_event_t_is_error(self) -> None:
        show = _one_role_show(events=[Event(t=-1.0, sound="coo")])
        issues = validate(show)
        self.assertTrue(any("must be >= 0" in i.message and i.track == "events" for i in issues))

    def test_zero_t_is_not_an_error(self) -> None:
        show = _one_role_show(locomotion=[LocomotionKeyframe(t=0.0, vx=0.1)])
        issues = validate(show)
        self.assertFalse(any("must be >= 0" in i.message for i in issues))


class ServoDiagnosticsTest(unittest.TestCase):
    """F48: neither validator previously looked at the servo track at
    all, so a negative/zero `duration` (an empty or nonsensical hold
    window) and a mode other than the only one v1 honors ("hold") passed
    silently.
    """

    def test_negative_servo_duration_is_error(self) -> None:
        show = _one_role_show(servo=[ServoEvent(t=1.0, mode="hold", duration=-2.0)])
        issues = validate(show)
        self.assertTrue(
            any(i.track == "servo" and "duration" in i.message and i.severity == "error" for i in issues)
        )

    def test_zero_servo_duration_is_error(self) -> None:
        show = _one_role_show(servo=[ServoEvent(t=1.0, mode="hold", duration=0.0)])
        issues = validate(show)
        self.assertTrue(
            any(i.track == "servo" and "duration" in i.message and i.severity == "error" for i in issues)
        )

    def test_non_hold_mode_is_warning_not_error(self) -> None:
        show = _one_role_show(servo=[ServoEvent(t=5.0, mode="laser_homing")])
        issues = validate(show)
        self.assertTrue(
            any(i.track == "servo" and "not honored" in i.message and i.severity == "warning" for i in issues)
        )
        self.assertEqual([i for i in issues if i.severity == "error"], [])

    def test_hold_with_positive_duration_is_clean(self) -> None:
        show = _one_role_show(servo=[ServoEvent(t=5.0, mode="hold", duration=2.0)])
        issues = validate(show)
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
