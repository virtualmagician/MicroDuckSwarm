"""Tests for python/duckshow/validator.py."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duckshow.limits import DEFAULT_LIMITS  # noqa: E402
from duckshow.loader import load_show  # noqa: E402
from duckshow.model import (  # noqa: E402
    CastMember,
    Event,
    HeadKeyframe,
    LocomotionKeyframe,
    Meta,
    MouthKeyframe,
    PolicyRequirement,
    PoseKeyframe,
    Requires,
    RoleTracks,
    Show,
)
from duckshow.validator import validate  # noqa: E402

DEMO_SHOW_PATH = Path(__file__).resolve().parent.parent.parent / "shows" / "demo" / "demo.duckshow.json"
FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "shows" / "fixtures"


def _issues_by_message_substr(issues, substr):
    return [i for i in issues if substr in i.message]


class DemoShowValidatesCleanTest(unittest.TestCase):
    def test_demo_show_has_no_errors(self) -> None:
        show = load_show(DEMO_SHOW_PATH)
        issues = validate(show)
        errors = [i for i in issues if i.severity == "error"]
        self.assertEqual(errors, [], f"unexpected errors: {errors}")


class MissingTracksEntryTest(unittest.TestCase):
    def test_cast_role_without_tracks_entry_is_error(self) -> None:
        show = Show(
            format="duckshow/1",
            meta=Meta(duration=10.0),
            requires=Requires(),
            cast=[CastMember(role="lead"), CastMember(role="ghost")],
            tracks={"lead": RoleTracks()},
        )
        issues = validate(show)
        errors = _issues_by_message_substr(issues, "no tracks entry")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].role, "ghost")
        self.assertEqual(errors[0].severity, "error")


class SortedUniqueTest(unittest.TestCase):
    def _show_with_locomotion(self, keyframes):
        return Show(
            format="duckshow/1",
            meta=Meta(duration=10.0),
            requires=Requires(),
            cast=[CastMember(role="lead")],
            tracks={"lead": RoleTracks(locomotion=keyframes)},
        )

    def test_unsorted_keyframes_error(self) -> None:
        show = self._show_with_locomotion(
            [LocomotionKeyframe(t=1.0), LocomotionKeyframe(t=0.5)]
        )
        issues = validate(show)
        self.assertTrue(_issues_by_message_substr(issues, "not sorted"))

    def test_duplicate_t_error(self) -> None:
        show = self._show_with_locomotion(
            [LocomotionKeyframe(t=1.0), LocomotionKeyframe(t=1.0)]
        )
        issues = validate(show)
        self.assertTrue(_issues_by_message_substr(issues, "duplicate t"))

    def test_sorted_unique_keyframes_pass(self) -> None:
        show = self._show_with_locomotion(
            [LocomotionKeyframe(t=0.0), LocomotionKeyframe(t=1.0)]
        )
        issues = validate(show)
        self.assertEqual([i for i in issues if i.severity == "error"], [])


class LimitViolationTest(unittest.TestCase):
    def _one_role_show(self, **tracks_kwargs):
        return Show(
            format="duckshow/1",
            meta=Meta(duration=10.0),
            requires=Requires(),
            cast=[CastMember(role="lead")],
            tracks={"lead": RoleTracks(**tracks_kwargs)},
        )

    def test_vx_over_limit(self) -> None:
        show = self._one_role_show(locomotion=[LocomotionKeyframe(t=0.0, vx=0.5)])
        issues = validate(show)
        self.assertTrue(_issues_by_message_substr(issues, "vx=0.5"))

    def test_vx_at_limit_passes(self) -> None:
        show = self._one_role_show(
            locomotion=[LocomotionKeyframe(t=0.0, vx=DEFAULT_LIMITS.max_abs_vx)]
        )
        issues = validate(show)
        self.assertEqual([i for i in issues if i.severity == "error"], [])

    def test_head_angle_over_limit(self) -> None:
        show = self._one_role_show(head=[HeadKeyframe(t=0.0, head_yaw=2.0)])
        issues = validate(show)
        self.assertTrue(_issues_by_message_substr(issues, "head_yaw=2.0"))

    def test_pose_z_over_limit(self) -> None:
        show = self._one_role_show(pose=[PoseKeyframe(t=0.0, z=1.0)])
        issues = validate(show)
        self.assertTrue(_issues_by_message_substr(issues, "z=1.0"))

    def test_mouth_open_out_of_range(self) -> None:
        show = self._one_role_show(mouth=[MouthKeyframe(t=0.0, open=1.5)])
        issues = validate(show)
        self.assertTrue(_issues_by_message_substr(issues, "open=1.5"))

    def test_mouth_open_negative_out_of_range(self) -> None:
        show = self._one_role_show(mouth=[MouthKeyframe(t=0.0, open=-0.1)])
        issues = validate(show)
        self.assertTrue(_issues_by_message_substr(issues, "outside allowed range"))


class EventValidationTest(unittest.TestCase):
    def _show_with_events(self, events):
        return Show(
            format="duckshow/1",
            meta=Meta(duration=10.0),
            requires=Requires(),
            cast=[CastMember(role="lead")],
            tracks={"lead": RoleTracks(events=events)},
        )

    def test_events_too_close_is_error(self) -> None:
        show = self._show_with_events([Event(t=1.0, sound="chirp"), Event(t=1.1, sound="coo")])
        issues = validate(show)
        self.assertTrue(_issues_by_message_substr(issues, "less than"))

    def test_events_spaced_ok(self) -> None:
        show = self._show_with_events([Event(t=1.0, sound="chirp"), Event(t=1.3, sound="coo")])
        issues = validate(show)
        self.assertEqual([i for i in issues if i.severity == "error"], [])

    def test_event_with_no_action_key_is_error(self) -> None:
        show = self._show_with_events([Event(t=1.0)])
        issues = validate(show)
        self.assertTrue(_issues_by_message_substr(issues, "no action key"))

    def test_event_with_two_action_keys_is_error(self) -> None:
        show = self._show_with_events([Event(t=1.0, do="kick_left", sound="chirp")])
        issues = validate(show)
        self.assertTrue(_issues_by_message_substr(issues, "more than one action key"))

    def test_mode_walk_validates_clean(self) -> None:
        # Real robotd accepts exactly "walk"/"roller" over the wire
        # (docs/robotd-api.md "Custom .onnx policies & modes") --
        # requires.policies plays no part in whether this is valid.
        show = self._show_with_events([Event(t=1.0, mode="walk")])
        issues = validate(show)
        self.assertEqual([i for i in issues if i.severity == "error"], [])

    def test_mode_roller_validates_clean(self) -> None:
        show = self._show_with_events([Event(t=1.0, mode="roller")])
        issues = validate(show)
        self.assertEqual([i for i in issues if i.severity == "error"], [])

    def test_unknown_mode_is_error(self) -> None:
        # BUG 1: a mode event must never carry anything but a real drive
        # mode -- a custom-named mode (e.g. from a mis-authored show
        # patterned on a custom policy's *label*) would be refused by
        # real robotd, even though it passes against the mock.
        show = self._show_with_events([Event(t=1.0, mode="moonwalk")])
        issues = validate(show)
        errors = _issues_by_message_substr(issues, "not a valid drive mode")
        self.assertEqual(len(errors), 1)
        self.assertIn("walk", errors[0].message)
        self.assertIn("roller", errors[0].message)

    def test_custom_policy_label_with_walk_mode_event_validates_clean(self) -> None:
        # A show that declares a custom policy (whose `name` is a human
        # label only) and drives it at runtime with an ordinary "walk"
        # mode event -- the documented, correct pattern -- validates
        # clean. The policy's label is never referenced by the event.
        show = Show(
            format="duckshow/1",
            meta=Meta(duration=10.0),
            requires=Requires(
                policies=[
                    PolicyRequirement(name="moonwalk", file="policies/moonwalk.onnx", sha256="abc", slot="walk")
                ]
            ),
            cast=[CastMember(role="lead")],
            tracks={"lead": RoleTracks(events=[Event(t=0.0, mode="walk")])},
        )
        issues = validate(show)
        self.assertEqual([i for i in issues if i.severity == "error"], [])


class ModeLocomotionOverlapTest(unittest.TestCase):
    def test_mode_overlapping_nonzero_locomotion_warns(self) -> None:
        show = Show(
            format="duckshow/1",
            meta=Meta(duration=10.0),
            requires=Requires(),
            cast=[CastMember(role="lead")],
            tracks={
                "lead": RoleTracks(
                    locomotion=[LocomotionKeyframe(t=0.0, vx=0.1), LocomotionKeyframe(t=2.0, vx=0.0)],
                    events=[Event(t=1.8, mode="roller")],
                )
            },
        )
        issues = validate(show)
        warnings = _issues_by_message_substr(issues, "overlaps nonzero locomotion")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].severity, "warning")

    def test_mode_with_zero_locomotion_nearby_no_warning(self) -> None:
        show = Show(
            format="duckshow/1",
            meta=Meta(duration=10.0),
            requires=Requires(),
            cast=[CastMember(role="lead")],
            tracks={
                "lead": RoleTracks(
                    locomotion=[LocomotionKeyframe(t=0.0, vx=0.0), LocomotionKeyframe(t=2.0, vx=0.0)],
                    events=[Event(t=1.0, mode="roller")],
                )
            },
        )
        issues = validate(show)
        self.assertFalse(_issues_by_message_substr(issues, "overlaps nonzero locomotion"))

    def test_mode_far_from_locomotion_no_warning(self) -> None:
        show = Show(
            format="duckshow/1",
            meta=Meta(duration=10.0),
            requires=Requires(),
            cast=[CastMember(role="lead")],
            tracks={
                "lead": RoleTracks(
                    locomotion=[LocomotionKeyframe(t=0.0, vx=0.1), LocomotionKeyframe(t=1.0, vx=0.0)],
                    events=[Event(t=5.0, mode="roller")],
                )
            },
        )
        issues = validate(show)
        self.assertFalse(_issues_by_message_substr(issues, "overlaps nonzero locomotion"))


class FixtureParityTest(unittest.TestCase):
    """Data-driven regression coverage for shows/fixtures/*.duckshow.json
    against shows/fixtures/expected.json (F67): each fixture is a small,
    focused document with one thing wrong (or one thing that's actually
    fine but easy to mistake for wrong -- see 'valid-unsorted-events').
    These are also loaded from SwarmLink/Tests/SwarmLinkTests -- see
    DuckShowFixtureTests.swift and expected.json's own "_comment". A
    fixture whose Swift result diverges from this one gets a 'divergent-'
    filename prefix; there is currently no such fixture.
    """

    @classmethod
    def setUpClass(cls) -> None:
        with open(FIXTURES_DIR / "expected.json", "r", encoding="utf-8") as f:
            cls.expected = json.load(f)

    def _fixture_names(self):
        return sorted(k for k in self.expected if not k.startswith("_"))

    def test_every_fixture_file_has_an_expected_entry_and_vice_versa(self) -> None:
        on_disk = {p.stem.removesuffix(".duckshow") for p in FIXTURES_DIR.glob("*.duckshow.json")}
        self.assertEqual(on_disk, set(self._fixture_names()))

    def test_fixtures_match_expected_python_validation(self) -> None:
        for name in self._fixture_names():
            with self.subTest(fixture=name):
                spec = self.expected[name]
                show = load_show(FIXTURES_DIR / f"{name}.duckshow.json")
                issues = validate(show)
                errors = [i for i in issues if i.severity == "error"]
                warnings = [i for i in issues if i.severity == "warning"]
                self.assertEqual(len(errors), spec["errors"], f"{name}: errors={errors}")
                self.assertEqual(len(warnings), spec["warnings"], f"{name}: warnings={warnings}")
                if "error_substr" in spec:
                    self.assertTrue(
                        any(spec["error_substr"] in e.message for e in errors),
                        f"{name}: no error contains {spec['error_substr']!r}: {errors}",
                    )
                if "warning_substr" in spec:
                    self.assertTrue(
                        any(spec["warning_substr"] in w.message for w in warnings),
                        f"{name}: no warning contains {spec['warning_substr']!r}: {warnings}",
                    )


if __name__ == "__main__":
    unittest.main()
