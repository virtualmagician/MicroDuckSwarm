"""Regression tests for python/duckshow/loader.py: every malformed-document
shape must raise DuckShowFormatError -- and only DuckShowFormatError -- never
a raw KeyError/ValueError/TypeError/AttributeError. An uncaught exception at
load time would kill the duck-agent's UDP receive thread (see agent.py's
_handle_load / _recv_loop), leaving the duck unable to process any further
command, panic included. See CLAUDE.md finding F35.

Also covers F37: NaN/Infinity/-Infinity are not valid JSON (RFC 8259) and
must be rejected at parse time rather than silently accepted and later
poisoning validation/robotd notifications/Sampler output.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duckshow.loader import DuckShowFormatError, loads_show  # noqa: E402

_BASE = {
    "format": "duckshow/1",
    "meta": {"duration": 10.0},
    "cast": [{"role": "lead"}],
}


def _load(doc: dict) -> None:
    loads_show(json.dumps(doc))


class MalformedDocumentsRaiseFormatErrorTest(unittest.TestCase):
    """Every scenario here must raise DuckShowFormatError, not leak a raw
    stdlib exception -- assertRaises(DuckShowFormatError) fails the test
    if e.g. a bare KeyError propagates instead.
    """

    def test_keyframe_missing_t(self) -> None:
        doc = {**_BASE, "tracks": {"lead": {"head": [{"neck_pitch": 0.0}]}}}
        with self.assertRaises(DuckShowFormatError):
            _load(doc)

    def test_non_numeric_t(self) -> None:
        doc = {**_BASE, "tracks": {"lead": {"head": [{"t": "soon", "neck_pitch": 0.0}]}}}
        with self.assertRaises(DuckShowFormatError):
            _load(doc)

    def test_requires_given_as_list(self) -> None:
        doc = {**_BASE, "requires": [], "tracks": {"lead": {}}}
        with self.assertRaises(DuckShowFormatError):
            _load(doc)

    def test_curve_track_given_as_object_not_list(self) -> None:
        doc = {**_BASE, "tracks": {"lead": {"head": {"neck_pitch": 0.0}}}}
        with self.assertRaises(DuckShowFormatError):
            _load(doc)

    def test_event_entry_is_a_number(self) -> None:
        doc = {**_BASE, "tracks": {"lead": {"events": [3]}}}
        with self.assertRaises(DuckShowFormatError):
            _load(doc)

    def test_policy_entry_is_a_string(self) -> None:
        doc = {**_BASE, "requires": {"policies": ["x"]}, "tracks": {"lead": {}}}
        with self.assertRaises(DuckShowFormatError):
            _load(doc)

    def test_cast_entry_is_a_number(self) -> None:
        doc = {**_BASE, "cast": [1], "tracks": {}}
        with self.assertRaises(DuckShowFormatError):
            _load(doc)

    def test_meta_given_as_a_list(self) -> None:
        doc = {"format": "duckshow/1", "meta": [], "cast": [], "tracks": {}}
        with self.assertRaises(DuckShowFormatError):
            _load(doc)

    def test_tracks_role_entry_is_not_an_object(self) -> None:
        doc = {**_BASE, "tracks": {"lead": [1, 2, 3]}}
        with self.assertRaises(DuckShowFormatError):
            _load(doc)


class NonFiniteNumbersRejectedTest(unittest.TestCase):
    """json.loads accepts the non-standard NaN/Infinity/-Infinity tokens by
    default; a .duckshow document must not.
    """

    def test_nan_keyframe_value_rejected(self) -> None:
        text = (
            '{"format":"duckshow/1","meta":{"duration":10.0},'
            '"cast":[{"role":"lead"}],'
            '"tracks":{"lead":{"locomotion":[{"t":0.0,"vx":NaN}]}}}'
        )
        with self.assertRaises(DuckShowFormatError):
            loads_show(text)

    def test_nan_t_rejected(self) -> None:
        text = (
            '{"format":"duckshow/1","meta":{"duration":10.0},'
            '"cast":[{"role":"lead"}],'
            '"tracks":{"lead":{"head":[{"t":NaN,"neck_pitch":0.0}]}}}'
        )
        with self.assertRaises(DuckShowFormatError):
            loads_show(text)

    def test_infinity_rejected(self) -> None:
        text = (
            '{"format":"duckshow/1","meta":{"duration":10.0},'
            '"cast":[{"role":"lead"}],'
            '"tracks":{"lead":{"locomotion":[{"t":0.0,"vy":Infinity}]}}}'
        )
        with self.assertRaises(DuckShowFormatError):
            loads_show(text)

    def test_negative_infinity_rejected(self) -> None:
        text = (
            '{"format":"duckshow/1","meta":{"duration":10.0},'
            '"cast":[{"role":"lead"}],'
            '"tracks":{"lead":{"locomotion":[{"t":0.0,"vy":-Infinity}]}}}'
        )
        with self.assertRaises(DuckShowFormatError):
            loads_show(text)


if __name__ == "__main__":
    unittest.main()
