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


SHOWS_DIR = Path(__file__).resolve().parent.parent.parent / "shows"


class ShippedShowsValidateCleanTest(unittest.TestCase):
    """Every show this repo ships must validate clean, not just demo.

    Only demo was gated, and only for errors. shows/octet and shows/showcase
    were covered by nothing at all -- every locomotion value in octet was
    rewritten during the speed-limit change and no test would have caught a
    mistake. Warnings count too here: a shipped show carrying a warning is
    either a real authoring problem or a validator rule that needs revisiting,
    and either way it should not sit there unnoticed.
    """

    def _shipped_shows(self) -> list[Path]:
        # shows/fixtures/ is deliberately excluded: several fixtures are
        # invalid on purpose, which is their whole job.
        found = sorted(
            p for p in SHOWS_DIR.rglob("*.duckshow.json")
            if p.relative_to(SHOWS_DIR).parts[0] != "fixtures"
        )
        self.assertTrue(found, "no shipped shows found -- this test would be vacuous")
        return found

    def test_every_shipped_show_has_no_errors(self) -> None:
        for path in self._shipped_shows():
            with self.subTest(show=path.name):
                issues = validate(load_show(path))
                errors = [i for i in issues if i.severity == "error"]
                self.assertEqual(errors, [], f"{path.name}: {[i.message for i in errors]}")

    def test_every_shipped_show_has_no_warnings(self) -> None:
        for path in self._shipped_shows():
            with self.subTest(show=path.name):
                issues = validate(load_show(path))
                warnings = [i for i in issues if i.severity == "warning"]
                self.assertEqual(warnings, [], f"{path.name}: {[i.message for i in warnings]}")

    def test_the_three_named_shows_are_actually_covered(self) -> None:
        """Guards the discovery above: if a show is renamed or moved out of
        shows/, this fails rather than silently testing less."""
        names = {p.name for p in self._shipped_shows()}
        for expected in ("demo.duckshow.json", "octet.duckshow.json", "showcase.duckshow.json"):
            self.assertIn(expected, names)


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


class SkillOccupancyOverlapTest(unittest.TestCase):
    """A `do` skill occupies the robot for its full episodic duration
    (docs/duckshow-format.md "Skill durations and occupancy"); a second
    skill scheduled inside that window is a WARNING naming both skills,
    the overlap, and the occupying skill's duration -- distinct from the
    0.25s _check_event_density spacing rule, which still applies to
    every discrete event regardless of type.
    """

    def _show_with_events(self, events, duration=10.0):
        return Show(
            format="duckshow/1",
            meta=Meta(duration=duration),
            requires=Requires(),
            cast=[CastMember(role="lead")],
            tracks={"lead": RoleTracks(events=events)},
        )

    def test_second_skill_inside_occupancy_window_warns_naming_both_and_overlap(self) -> None:
        show = self._show_with_events(
            [Event(t=0.0, do="ground_pick"), Event(t=0.5, do="kick_left")]
        )
        issues = validate(show)
        warnings = [i for i in issues if i.severity == "warning"]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertEqual(
            warnings[0].message,
            "do='kick_left' at t=0.5 begins 2.3s into the 2.8s execution of do='ground_pick' at t=0.0",
        )
        self.assertEqual(warnings[0].t, 0.5)
        self.assertEqual([i for i in issues if i.severity == "error"], [])

    def test_second_skill_after_occupancy_window_does_not_warn(self) -> None:
        # Exactly at the boundary: the first skill's occupancy has ended.
        show = self._show_with_events(
            [Event(t=0.0, do="ground_pick"), Event(t=2.8, do="kick_left")]
        )
        issues = validate(show)
        self.assertFalse(_issues_by_message_substr(issues, "execution of do="))

    def test_roulade_followed_by_roulade_does_not_warn(self) -> None:
        # manifest.json marks roulade.onnx "chain": true -- a repeat
        # immediately after itself is the documented way to keep
        # rolling, not two skills contending for one window, even though
        # 0.5s is inside roulade's own 1.0s duration.
        show = self._show_with_events(
            [Event(t=0.0, do="roulade"), Event(t=0.5, do="roulade")]
        )
        issues = validate(show)
        self.assertEqual([i for i in issues if i.severity == "warning"], [])
        self.assertEqual([i for i in issues if i.severity == "error"], [])

    def test_ground_pick_duration_depends_on_preceding_mode_event(self) -> None:
        # Same events, only the drive mode differs: in roller mode
        # ground_pick occupies 3.5s (roller_crouch.onnx) instead of 2.8s
        # (alpha_ground_pick.onnx), resolved from the mode event
        # preceding it -- long enough to newly overlap a skill that a
        # walk-mode ground_pick would not have reached.
        walk_show = self._show_with_events(
            [Event(t=1.0, do="ground_pick"), Event(t=4.0, do="kick_right")]
        )
        self.assertEqual([i for i in validate(walk_show) if i.severity == "warning"], [])

        roller_show = self._show_with_events(
            [
                Event(t=0.0, mode="roller"),
                Event(t=1.0, do="ground_pick"),
                Event(t=4.0, do="kick_right"),
            ]
        )
        warnings = [i for i in validate(roller_show) if i.severity == "warning"]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertEqual(
            warnings[0].message,
            "do='kick_right' at t=4.0 begins 0.5s into the 3.5s execution of do='ground_pick' at t=1.0",
        )

    def test_sit_toggle_has_no_duration_so_never_occupies(self) -> None:
        # sit_toggle (alpha_sitstand.onnx) is "scripted", not "episodic" --
        # no confirmed duration_s in the manifest -- so it never warns as
        # the *occupying* skill, no matter how soon the next skill fires
        # (as long as the unrelated 0.25s density rule is still honored).
        show = self._show_with_events(
            [Event(t=0.0, do="sit_toggle"), Event(t=0.3, do="kick_left")]
        )
        issues = validate(show)
        self.assertEqual([i for i in issues if i.severity == "warning"], [])
        self.assertEqual([i for i in issues if i.severity == "error"], [])

    def test_sit_toggle_can_still_be_the_interrupting_skill(self) -> None:
        # sit_toggle has no duration of its own, but it can still be the
        # *later* skill that begins inside another skill's window.
        show = self._show_with_events(
            [Event(t=0.0, do="ground_pick"), Event(t=0.5, do="sit_toggle")]
        )
        warnings = [i for i in validate(show) if i.severity == "warning"]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("do='sit_toggle' at t=0.5", warnings[0].message)
        self.assertIn("execution of do='ground_pick' at t=0.0", warnings[0].message)

    def test_kicks_and_density_rule_are_independent(self) -> None:
        # The pre-existing 0.25s _check_event_density rule still fires on
        # its own terms, unaffected by this check.
        show = self._show_with_events(
            [Event(t=0.0, do="kick_left"), Event(t=0.1, do="kick_right")]
        )
        issues = validate(show)
        self.assertTrue(_issues_by_message_substr(issues, "is less than 0.25s after previous event"))


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
