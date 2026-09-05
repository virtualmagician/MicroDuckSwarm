"""duckset loader + validator, against docs/setlist-format.md.

The split under test is the one duckshow already uses: the loader fails only
on documents that cannot be read at all, and everything semantic comes back as
Issue records with a location. The cases that matter here are the ones where
the two could plausibly be swapped, because getting them the wrong way round
either refuses a setlist a newer editor wrote or silently accepts a set that
cannot run.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckset  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OCTET = "/shows/octet/octet.duckshow.json"
DEMO = "/shows/demo/demo.duckshow.json"


def doc(entries, name="probe", **meta):
    return {"format": "duckset/1", "meta": {"name": name, **meta}, "entries": entries}


def entry(id_, show=OCTET, **kw):
    return {"id": id_, "show": show, **kw}


def severities(issues, severity):
    return [i for i in issues if i.severity == severity]


class Loading(unittest.TestCase):
    def test_round_trips_a_minimal_document(self) -> None:
        s = duckset.parse_setlist(doc([entry("a")]))
        self.assertEqual(s.format, "duckset/1")
        self.assertEqual(s.meta.name, "probe")
        self.assertEqual(s.entries[0].id, "a")
        self.assertEqual(s.entries[0].end, "hold", "hold is the default end behaviour")
        self.assertIsNone(s.entries[0].label)

    def test_an_empty_setlist_is_a_valid_document(self) -> None:
        # The editor creates one before anything is dragged into it.
        s = duckset.parse_setlist(doc([]))
        self.assertEqual(s.entries, [])
        self.assertEqual(severities(duckset.validate(s), "error"), [])

    def test_unknown_fields_are_ignored(self) -> None:
        # CLAUDE.md rule 4, everywhere.
        raw = doc([entry("a", colour="red")])
        raw["future_block"] = {"anything": 1}
        raw["meta"]["unknown"] = True
        s = duckset.parse_setlist(raw)
        self.assertEqual(s.entries[0].id, "a")

    def test_rejects_an_unsupported_major(self) -> None:
        raw = doc([])
        raw["format"] = "duckset/2"
        with self.assertRaises(duckset.DuckSetFormatError) as cm:
            duckset.parse_setlist(raw)
        self.assertIn("duckset/1", str(cm.exception))

    def test_rejects_a_missing_or_malformed_format(self) -> None:
        for raw, why in [({"meta": {"name": "x"}}, "absent"),
                         ({"format": "setlist/1", "meta": {"name": "x"}}, "wrong name"),
                         ({"format": 1, "meta": {"name": "x"}}, "not a string")]:
            with self.subTest(why=why):
                with self.assertRaises(duckset.DuckSetFormatError):
                    duckset.parse_setlist(raw)

    def test_rejects_wrong_json_types(self) -> None:
        for raw, why in [
            ({"format": "duckset/1", "meta": {"name": "x"}, "entries": {}}, "entries not a list"),
            ({"format": "duckset/1", "meta": {"name": "x"}, "entries": ["a"]}, "entry not an object"),
            ({"format": "duckset/1", "meta": {"name": 7}}, "meta.name not a string"),
            ({"format": "duckset/1", "meta": {"name": "x"}, "entries": [{"show": OCTET}]}, "no id"),
            ({"format": "duckset/1", "meta": {"name": "x"}, "entries": [{"id": "a"}]}, "no show"),
        ]:
            with self.subTest(why=why):
                with self.assertRaises(duckset.DuckSetFormatError):
                    duckset.parse_setlist(raw)

    def test_an_unknown_end_behaviour_loads_and_is_a_validator_error(self) -> None:
        # Deliberately NOT a parse failure: a setlist written by a newer editor
        # must still open here, with the problem shown against the block that
        # has it, rather than refusing the whole file.
        s = duckset.parse_setlist(doc([entry("a", end="fade")]))
        self.assertEqual(s.entries[0].end, "fade")
        errors = severities(duckset.validate(s), "error")
        self.assertEqual(len(errors), 1, errors)
        self.assertEqual(errors[0].entry, "a")
        self.assertIn("fade", errors[0].message)

    def test_loads_from_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "set.duckset.json"
            path.write_text(json.dumps(doc([entry("a")])), encoding="utf-8")
            self.assertEqual(duckset.load_setlist(path).entries[0].id, "a")

    def test_unparseable_text_names_the_problem(self) -> None:
        with self.assertRaises(duckset.DuckSetFormatError) as cm:
            duckset.loads_setlist("{not json")
        self.assertIn("JSON", str(cm.exception))


class Validation(unittest.TestCase):
    def test_a_clean_setlist_has_no_errors(self) -> None:
        s = duckset.parse_setlist(doc([entry("a", end="hold"), entry("b", end="loop")]))
        self.assertEqual(severities(duckset.validate(s), "error"), [])

    def test_duplicate_entry_ids_are_an_error(self) -> None:
        # Ids key the editor's drag ordering and any operator cue that names an
        # entry; two blocks answering to one name is ambiguous in both.
        s = duckset.parse_setlist(doc([entry("a"), entry("a", show=DEMO)]))
        errors = severities(duckset.validate(s), "error")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("duplicate", errors[0].message)

    def test_the_same_show_twice_under_different_ids_is_fine(self) -> None:
        # A reprise. This is exactly why the id is not the show.
        s = duckset.parse_setlist(doc([entry("open"), entry("reprise")]))
        self.assertEqual(severities(duckset.validate(s), "error"), [])

    def test_a_show_path_must_be_a_duckshow(self) -> None:
        s = duckset.parse_setlist(doc([entry("a", show="/shows/octet/notes.txt")]))
        errors = severities(duckset.validate(s), "error")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(".duckshow.json", errors[0].message)

    def test_a_show_path_that_escapes_the_repo_is_an_error(self) -> None:
        s = duckset.parse_setlist(doc([entry("a", show="/../secrets.duckshow.json")]))
        errors = severities(duckset.validate(s, REPO_ROOT), "error")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("escapes", errors[0].message)

    def test_an_empty_meta_name_is_an_error(self) -> None:
        s = duckset.parse_setlist(doc([], name="   "))
        self.assertEqual(len(severities(duckset.validate(s), "error")), 1)

    def test_a_trailing_continue_is_a_warning_not_an_error(self) -> None:
        s = duckset.parse_setlist(doc([entry("a"), entry("b", end="continue")]))
        issues = duckset.validate(s)
        self.assertEqual(severities(issues, "error"), [])
        warnings = [i for i in severities(issues, "warning") if "continue" in i.message]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertEqual(warnings[0].entry, "b")

    def test_a_continue_that_is_not_last_says_nothing(self) -> None:
        s = duckset.parse_setlist(doc([entry("a", end="continue"), entry("b")]))
        self.assertEqual([i for i in duckset.validate(s) if "continue" in i.message], [])


class ValidationAgainstDisk(unittest.TestCase):
    """The checks that need the referenced shows. All warnings: a setlist that
    names a show this machine does not have is still a valid document."""

    def test_a_missing_show_is_a_warning_not_an_error(self) -> None:
        s = duckset.parse_setlist(doc([entry("a", show="/shows/nope/nope.duckshow.json")]))
        issues = duckset.validate(s, REPO_ROOT)
        self.assertEqual(severities(issues, "error"), [])
        self.assertTrue(any("not found" in i.message for i in issues), issues)

    def test_without_a_repo_root_nothing_touches_the_filesystem(self) -> None:
        s = duckset.parse_setlist(doc([entry("a", show="/shows/nope/nope.duckshow.json")]))
        self.assertEqual(duckset.validate(s), [])

    def test_a_cast_change_between_entries_is_a_warning(self) -> None:
        # The operator is about to be surprised by which ducks are needed.
        s = duckset.parse_setlist(doc([entry("a", show=OCTET), entry("b", show=DEMO)]))
        issues = duckset.validate(s, REPO_ROOT)
        self.assertEqual(severities(issues, "error"), [])
        self.assertTrue(any("cast changes" in i.message for i in issues), issues)

    def test_the_same_cast_twice_says_nothing(self) -> None:
        s = duckset.parse_setlist(doc([entry("a", show=OCTET), entry("b", show=OCTET)]))
        issues = duckset.validate(s, REPO_ROOT)
        self.assertEqual([i for i in issues if "cast changes" in i.message], [])

    def test_a_show_that_fails_its_own_validator_is_a_warning(self) -> None:
        # shows/fixtures holds deliberately invalid documents for exactly this.
        broken = sorted((REPO_ROOT / "shows" / "fixtures").glob("invalid-*.duckshow.json"))
        self.assertTrue(broken, "no invalid fixture to point a setlist at")
        rel = "/" + broken[0].relative_to(REPO_ROOT).as_posix()
        s = duckset.parse_setlist(doc([entry("a", show=rel)]))
        issues = duckset.validate(s, REPO_ROOT)
        self.assertEqual(severities(issues, "error"), [])
        self.assertTrue(any("does not load" in i.message or "fails validation" in i.message
                            for i in issues), issues)


if __name__ == "__main__":
    unittest.main()
