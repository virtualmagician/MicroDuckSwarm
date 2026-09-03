"""Tests for python/duckshow/loader.py against the demo show + synthetic docs."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duckshow.loader import DuckShowFormatError, load_show, loads_show  # noqa: E402

DEMO_SHOW_PATH = Path(__file__).resolve().parent.parent.parent / "shows" / "demo" / "demo.duckshow.json"


class LoadDemoShowTest(unittest.TestCase):
    def test_loads_demo_show(self) -> None:
        show = load_show(DEMO_SHOW_PATH)
        self.assertEqual(show.format, "duckshow/1")
        self.assertEqual(show.meta.name, "Demo Waddle")
        self.assertEqual(show.meta.duration, 20.0)
        self.assertEqual(show.meta.music.file, "demo.wav")
        self.assertEqual(show.meta.music.bpm, 120.0)
        self.assertEqual([c.role for c in show.cast], ["lead", "wing"])
        self.assertIn("lead", show.tracks)
        self.assertIn("wing", show.tracks)

    def test_lead_track_contents(self) -> None:
        show = load_show(DEMO_SHOW_PATH)
        lead = show.tracks["lead"]
        self.assertEqual(len(lead.head), 8)
        self.assertEqual(len(lead.locomotion), 4)
        self.assertEqual(len(lead.mouth), 3)
        self.assertEqual(len(lead.events), 4)
        # First head keyframe explicit interp, last omits it (defaults to linear).
        self.assertEqual(lead.head[0].interp, "smooth")
        self.assertEqual(lead.head[-1].interp, "linear")

    def test_event_action_kinds(self) -> None:
        show = load_show(DEMO_SHOW_PATH)
        events = show.tracks["lead"].events
        kinds = [e.action_kind() for e in events]
        self.assertEqual(kinds, ["sound", "sound", "do", "do"])


class LoadsShowStringTest(unittest.TestCase):
    MINIMAL = """
    {
      "format": "duckshow/1",
      "meta": {"duration": 5.0},
      "cast": [{"role": "lead"}],
      "tracks": {"lead": {}}
    }
    """

    def test_minimal_document(self) -> None:
        show = loads_show(self.MINIMAL)
        self.assertEqual(show.meta.duration, 5.0)
        self.assertEqual(show.role_names(), ["lead"])
        self.assertEqual(show.tracks["lead"].locomotion, [])

    def test_unknown_top_level_fields_are_ignored(self) -> None:
        text = """
        {
          "format": "duckshow/1",
          "meta": {"duration": 5.0},
          "cast": [{"role": "lead"}],
          "tracks": {"lead": {}},
          "totally_unknown_field": {"nested": [1, 2, 3]}
        }
        """
        show = loads_show(text)  # must not raise
        self.assertEqual(show.role_names(), ["lead"])

    def test_unknown_nested_fields_are_ignored(self) -> None:
        text = """
        {
          "format": "duckshow/1",
          "meta": {"duration": 5.0, "unknown_meta_field": 42},
          "cast": [{"role": "lead", "unknown_cast_field": true}],
          "tracks": {
            "lead": {
              "locomotion": [
                {"t": 0.0, "vx": 0.1, "vy": 0.0, "vyaw": 0.0, "interp": "linear", "extra": "ignored"}
              ]
            }
          }
        }
        """
        show = loads_show(text)
        self.assertEqual(show.tracks["lead"].locomotion[0].vx, 0.1)

    def test_missing_format_field_raises(self) -> None:
        text = '{"meta": {"duration": 1.0}, "cast": [], "tracks": {}}'
        with self.assertRaises(DuckShowFormatError):
            loads_show(text)

    def test_unsupported_major_version_raises_clear_error(self) -> None:
        text = '{"format": "duckshow/2", "meta": {"duration": 1.0}, "cast": [], "tracks": {}}'
        with self.assertRaises(DuckShowFormatError) as ctx:
            loads_show(text)
        msg = str(ctx.exception)
        self.assertIn("2", msg)
        self.assertIn("duckshow/1", msg)

    def test_malformed_format_field_raises(self) -> None:
        text = '{"format": "not-a-duckshow-format", "meta": {}, "cast": [], "tracks": {}}'
        with self.assertRaises(DuckShowFormatError):
            loads_show(text)

    def test_invalid_json_raises(self) -> None:
        with self.assertRaises(DuckShowFormatError):
            loads_show("{not json")

    def test_non_object_document_raises(self) -> None:
        with self.assertRaises(DuckShowFormatError):
            loads_show("[1, 2, 3]")

    def test_default_interp_is_linear(self) -> None:
        text = """
        {
          "format": "duckshow/1",
          "meta": {"duration": 5.0},
          "cast": [{"role": "lead"}],
          "tracks": {"lead": {"mouth": [{"t": 0.0, "open": 0.5}]}}
        }
        """
        show = loads_show(text)
        self.assertEqual(show.tracks["lead"].mouth[0].interp, "linear")

    def test_event_hold_parsed_from_raw_json(self) -> None:
        # duckshow-format.md: sound events may carry an optional
        # "hold": <seconds> (a duration, not robotd's boolean start/stop
        # flag of the same name -- see agent.py's _fire_event). Every
        # other loader test builds Event dataclasses directly rather than
        # parsing this field from JSON (F72).
        text = """
        {
          "format": "duckshow/1",
          "meta": {"duration": 5.0},
          "cast": [{"role": "lead"}],
          "tracks": {"lead": {"events": [{"t": 1.0, "sound": "coo", "hold": 2.5}]}}
        }
        """
        show = loads_show(text)
        event = show.tracks["lead"].events[0]
        self.assertEqual(event.sound, "coo")
        self.assertEqual(event.hold, 2.5)

    def test_servo_entry_parsed_from_raw_json_with_default_mode(self) -> None:
        text = """
        {
          "format": "duckshow/1",
          "meta": {"duration": 30.0},
          "cast": [{"role": "lead"}],
          "tracks": {"lead": {"servo": [
            {"t": 5.0, "mode": "hold", "duration": 2.0},
            {"t": 20.0, "duration": 1.0}
          ]}}
        }
        """
        show = loads_show(text)
        servo = show.tracks["lead"].servo
        self.assertEqual(servo[0].t, 5.0)
        self.assertEqual(servo[0].mode, "hold")
        self.assertEqual(servo[0].duration, 2.0)
        # "mode" omitted in the JSON -> defaults to "hold"
        # (docs/duckshow-format.md: "v1 agents only honor {"mode": "hold"}").
        self.assertEqual(servo[1].mode, "hold")
        self.assertEqual(servo[1].duration, 1.0)

    def test_policies_parsed(self) -> None:
        text = """
        {
          "format": "duckshow/1",
          "meta": {"duration": 5.0},
          "requires": {"policies": [
            {"name": "moonwalk", "file": "policies/moonwalk.onnx",
             "sha256": "abc123", "slot": "walk"}
          ]},
          "cast": [{"role": "lead"}],
          "tracks": {"lead": {}}
        }
        """
        show = loads_show(text)
        self.assertEqual(len(show.requires.policies), 1)
        p = show.requires.policies[0]
        # "name" is a human label only (docs/duckshow-format.md "Custom
        # .onnx policies"), never sent over the wire; "slot" is what
        # matters. There is no per-policy "mode" -- see duckshow/model.py.
        self.assertEqual(p.name, "moonwalk")
        self.assertEqual(p.slot, "walk")


if __name__ == "__main__":
    unittest.main()
