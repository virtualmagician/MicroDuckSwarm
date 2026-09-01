"""Tests for python/duckshow/sampler.py: interpolation, hold, events, mode."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duckshow.loader import load_show  # noqa: E402
from duckshow.model import (  # noqa: E402
    CastMember,
    Event,
    HeadKeyframe,
    LocomotionKeyframe,
    Meta,
    MouthKeyframe,
    PoseKeyframe,
    Requires,
    RoleTracks,
    Show,
    ServoEvent,
)
from duckshow.sampler import Sampler, _smoothstep  # noqa: E402

DEMO_SHOW_PATH = Path(__file__).resolve().parent.parent.parent / "shows" / "demo" / "demo.duckshow.json"


def _show(role="lead", duration=10.0, **tracks_kwargs):
    return Show(
        format="duckshow/1",
        meta=Meta(duration=duration),
        requires=Requires(),
        cast=[CastMember(role=role)],
        tracks={role: RoleTracks(**tracks_kwargs)},
    )


class LinearInterpTest(unittest.TestCase):
    def test_linear_midpoint(self) -> None:
        show = _show(locomotion=[LocomotionKeyframe(t=0.0, vx=0.0, interp="linear"), LocomotionKeyframe(t=2.0, vx=1.0)])
        s = Sampler(show, "lead")
        f = s.at(1.0)
        self.assertAlmostEqual(f.locomotion.vx, 0.5)

    def test_linear_quarter_point(self) -> None:
        show = _show(locomotion=[LocomotionKeyframe(t=0.0, vx=0.0, interp="linear"), LocomotionKeyframe(t=4.0, vx=2.0)])
        s = Sampler(show, "lead")
        f = s.at(1.0)
        self.assertAlmostEqual(f.locomotion.vx, 0.5)


class StepInterpTest(unittest.TestCase):
    def test_step_holds_first_value_until_next_keyframe(self) -> None:
        show = _show(locomotion=[LocomotionKeyframe(t=0.0, vx=0.0, interp="step"), LocomotionKeyframe(t=2.0, vx=1.0)])
        s = Sampler(show, "lead")
        self.assertAlmostEqual(s.at(0.0).locomotion.vx, 0.0)
        self.assertAlmostEqual(s.at(1.0).locomotion.vx, 0.0)
        self.assertAlmostEqual(s.at(1.999).locomotion.vx, 0.0)
        self.assertAlmostEqual(s.at(2.0).locomotion.vx, 1.0)


class SmoothInterpTest(unittest.TestCase):
    def test_smooth_midpoint_matches_smoothstep_formula(self) -> None:
        show = _show(head=[HeadKeyframe(t=0.0, head_pitch=0.0, interp="smooth"), HeadKeyframe(t=2.0, head_pitch=1.0)])
        s = Sampler(show, "lead")
        f = s.at(1.0)
        expected = _smoothstep(0.5)  # 0.5 at the midpoint of a smoothstep curve too
        self.assertAlmostEqual(f.head.head_pitch, expected)
        self.assertAlmostEqual(f.head.head_pitch, 0.5)

    def test_smooth_quarter_point_is_not_linear(self) -> None:
        show = _show(head=[HeadKeyframe(t=0.0, head_pitch=0.0, interp="smooth"), HeadKeyframe(t=4.0, head_pitch=1.0)])
        s = Sampler(show, "lead")
        f = s.at(1.0)  # frac = 0.25
        expected = _smoothstep(0.25)
        self.assertAlmostEqual(f.head.head_pitch, expected)
        self.assertNotAlmostEqual(f.head.head_pitch, 0.25)  # would be 0.25 if it were linear

    def test_smooth_quarter_point_matches_pinned_literal(self) -> None:
        # Pins the actual canonical smoothstep(0.25) = 3*0.25^2 - 2*0.25^3
        # = 0.15625 as a literal, without importing _smoothstep -- so a
        # regression that breaks the *formula itself* (e.g. swapping in
        # x*x) is caught. test_smooth_quarter_point_is_not_linear above
        # computes its "expected" value by calling the very function under
        # test, so it can only catch the sampler diverging from
        # _smoothstep, never _smoothstep itself being wrong (F73).
        show = _show(head=[HeadKeyframe(t=0.0, head_pitch=0.0, interp="smooth"), HeadKeyframe(t=4.0, head_pitch=1.0)])
        s = Sampler(show, "lead")
        f = s.at(1.0)  # frac = 0.25
        self.assertAlmostEqual(f.head.head_pitch, 0.15625)

    def test_smooth_midpoint_matches_pinned_literal(self) -> None:
        show = _show(head=[HeadKeyframe(t=0.0, head_pitch=0.0, interp="smooth"), HeadKeyframe(t=2.0, head_pitch=1.0)])
        s = Sampler(show, "lead")
        self.assertAlmostEqual(s.at(1.0).head.head_pitch, 0.5)

    def test_smooth_endpoints_are_exact(self) -> None:
        show = _show(head=[HeadKeyframe(t=0.0, head_pitch=0.3, interp="smooth"), HeadKeyframe(t=2.0, head_pitch=0.9)])
        s = Sampler(show, "lead")
        self.assertAlmostEqual(s.at(0.0).head.head_pitch, 0.3)
        self.assertAlmostEqual(s.at(2.0).head.head_pitch, 0.9)


class HoldSemanticsTest(unittest.TestCase):
    def test_hold_before_first_keyframe(self) -> None:
        show = _show(mouth=[MouthKeyframe(t=5.0, open=0.7)])
        s = Sampler(show, "lead")
        self.assertAlmostEqual(s.at(0.0).mouth.open, 0.7)
        self.assertAlmostEqual(s.at(4.999).mouth.open, 0.7)

    def test_hold_after_last_keyframe(self) -> None:
        show = _show(duration=100.0, mouth=[MouthKeyframe(t=1.0, open=0.2), MouthKeyframe(t=2.0, open=0.9)])
        s = Sampler(show, "lead")
        self.assertAlmostEqual(s.at(2.0).mouth.open, 0.9)
        self.assertAlmostEqual(s.at(50.0).mouth.open, 0.9)

    def test_locomotion_zeroed_at_and_after_duration(self) -> None:
        show = _show(duration=5.0, locomotion=[LocomotionKeyframe(t=0.0, vx=0.2), LocomotionKeyframe(t=1.0, vx=0.2)])
        s = Sampler(show, "lead")
        # Would hold 0.2 after the last keyframe were it not for meta.duration...
        self.assertAlmostEqual(s.at(4.999).locomotion.vx, 0.2)
        # ...but at/after duration it's forced to zero regardless of track contents.
        f = s.at(5.0)
        self.assertEqual((f.locomotion.vx, f.locomotion.vy, f.locomotion.vyaw), (0.0, 0.0, 0.0))
        f2 = s.at(10.0)
        self.assertEqual((f2.locomotion.vx, f2.locomotion.vy, f2.locomotion.vyaw), (0.0, 0.0, 0.0))

    def test_missing_track_yields_none(self) -> None:
        show = _show(mouth=[MouthKeyframe(t=0.0, open=0.5)])
        s = Sampler(show, "lead")
        f = s.at(0.0)
        self.assertIsNone(f.locomotion)
        self.assertIsNone(f.head)
        self.assertIsNone(f.pose)
        self.assertIsNotNone(f.mouth)


class BooleanStepSemanticsTest(unittest.TestCase):
    def test_pose_active_always_steps_even_with_smooth_interp(self) -> None:
        show = _show(
            pose=[
                PoseKeyframe(t=0.0, z=0.0, active=True, interp="smooth"),
                PoseKeyframe(t=2.0, z=1.0, active=False),
            ]
        )
        s = Sampler(show, "lead")
        # z interpolates smoothly, but `active` must hold the earlier
        # keyframe's boolean value until the next keyframe is reached.
        self.assertTrue(s.at(0.0).pose.active)
        self.assertTrue(s.at(1.0).pose.active)
        self.assertTrue(s.at(1.999).pose.active)
        self.assertFalse(s.at(2.0).pose.active)
        # meanwhile z at the midpoint follows smoothstep, not step:
        self.assertAlmostEqual(s.at(1.0).pose.z, 0.5)


class EventsBetweenTest(unittest.TestCase):
    def _show_with_events(self):
        return _show(events=[Event(t=1.0, sound="chirp"), Event(t=2.0, do="kick_left"), Event(t=2.0, sound="peck")])

    def test_event_exactly_at_t1_fires(self) -> None:
        show = self._show_with_events()
        s = Sampler(show, "lead")
        fired = s.events_between(0.5, 1.0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].t, 1.0)

    def test_event_exactly_at_t0_does_not_refire(self) -> None:
        show = self._show_with_events()
        s = Sampler(show, "lead")
        # Simulate: tick already processed up to t=1.0 (fired above); a
        # zero-width or repeated window starting there must not refire it.
        fired = s.events_between(1.0, 1.0)
        self.assertEqual(fired, [])

    def test_events_sorted_and_multiple_at_same_t_both_fire(self) -> None:
        show = self._show_with_events()
        s = Sampler(show, "lead")
        fired = s.events_between(1.0, 2.0)
        self.assertEqual(len(fired), 2)
        self.assertEqual({e.t for e in fired}, {2.0})

    def test_late_join_skips_earlier_events(self) -> None:
        # Late join: playback starts at show_time=1.5, so the events_between
        # window should start there, and the t=1.0 sound must never fire.
        show = self._show_with_events()
        s = Sampler(show, "lead")
        fired = s.events_between(1.5, 3.0)
        self.assertEqual([e.t for e in fired], [2.0, 2.0])


class ModeAtTest(unittest.TestCase):
    def test_mode_at_returns_none_before_any_mode_event(self) -> None:
        show = _show(events=[Event(t=5.0, mode="roller")])
        s = Sampler(show, "lead")
        self.assertIsNone(s.mode_at(0.0))
        self.assertIsNone(s.mode_at(4.999))

    def test_mode_at_returns_latest_mode_leq_t(self) -> None:
        show = _show(events=[Event(t=5.0, mode="roller"), Event(t=10.0, mode="legs")])
        s = Sampler(show, "lead")
        self.assertEqual(s.mode_at(5.0), "roller")
        self.assertEqual(s.mode_at(7.0), "roller")
        self.assertEqual(s.mode_at(10.0), "legs")
        self.assertEqual(s.mode_at(100.0), "legs")

    def test_mode_at_ignores_non_mode_events(self) -> None:
        show = _show(events=[Event(t=1.0, sound="chirp"), Event(t=2.0, mode="roller")])
        s = Sampler(show, "lead")
        self.assertIsNone(s.mode_at(1.5))
        self.assertEqual(s.mode_at(2.0), "roller")


class ServoAtTest(unittest.TestCase):
    def test_servo_hold_window(self) -> None:
        show = _show(servo=[ServoEvent(t=5.0, mode="hold", duration=2.0)])
        s = Sampler(show, "lead")
        self.assertIsNone(s.servo_at(4.999))
        self.assertIsNotNone(s.servo_at(5.0))
        self.assertIsNotNone(s.servo_at(6.999))
        self.assertIsNone(s.servo_at(7.0))


class DemoShowSamplerSmokeTest(unittest.TestCase):
    def test_samples_across_whole_duration_without_error(self) -> None:
        show = load_show(DEMO_SHOW_PATH)
        for role in show.role_names():
            s = Sampler(show, role)
            t = 0.0
            while t <= show.meta.duration + 1.0:
                s.at(t)
                t += 0.02  # 50 Hz


if __name__ == "__main__":
    unittest.main()
