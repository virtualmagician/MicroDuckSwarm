"""CLAUDE.md rule 6, enforced by the gate rather than by memory.

"This repo is public. No venue details, client names, or credentials in code,
comments, or fixtures."

A fleet file is a list of duck addresses on a venue network, so it is exactly
the thing rule 6 exists to keep out. docs/fleet.md puts it outside the checkout
and .gitignore is the backstop for the copy someone drops in here anyway. This
file is what makes both of those true tomorrow: a rule nothing checks is a rule
that lasts until the evening someone is in a hurry.

Deliberately checks `git ls-files` rather than the working tree, because what
matters is what is committed and pushed, not what is sitting on a laptop.
"""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Filenames that would hold real duck addresses. fleet.example.json is the one
# committed exception and is checked separately below.
_FLEET_FILE_RE = re.compile(r"(^|/)(fleet|roster)\.json$|\.(fleet|roster)\.json$")

_IGNORE_ENTRIES = ("fleet.json", "*.fleet.json", "roster.json", "*.roster.json", "/fleet/")


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


class NoFleetDataIsCommitted(unittest.TestCase):
    def test_no_fleet_or_roster_file_is_tracked(self) -> None:
        offenders = [p for p in tracked_files() if _FLEET_FILE_RE.search(p)]
        self.assertEqual(offenders, [], f"venue addresses must not be committed (rule 6): {offenders}")

    def test_nothing_tracked_is_a_path_the_ignore_rules_exclude(self) -> None:
        """The regex above re-expresses the ignore rules and can drift from
        them. This asks git instead, so a rule the regex does not model (the
        whole `/fleet/` directory, for one) still counts.

        --no-index is required: check-ignore normally reports nothing for a
        tracked path, since tracking wins over ignoring, which is exactly the
        state this is looking for.
        """
        tracked = tracked_files()
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--stdin"],
            cwd=REPO_ROOT, input="\n".join(tracked), capture_output=True, text=True,
        )
        offenders = [line for line in result.stdout.splitlines() if line]
        # fleet.example.json is committed on purpose and .gitignore negates it,
        # so check-ignore does not list it; anything else here is a real leak.
        self.assertEqual(offenders, [],
                         f"tracked despite matching an ignore rule (rule 6): {offenders}")

    def test_gitignore_carries_the_backstop_entries(self) -> None:
        text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        lines = {line.strip() for line in text.splitlines()}
        missing = [entry for entry in _IGNORE_ENTRIES if entry not in lines]
        self.assertEqual(missing, [], f".gitignore lost its fleet-file backstop: {missing}")

    def test_git_actually_ignores_those_paths(self) -> None:
        # The entries being present is not the same as them working: a later
        # negation or a broader pattern could undo them.
        for candidate in ["fleet.json", "venue.fleet.json", "roster.json",
                          "show.roster.json", "fleet/anything.json", "deploy/fleet.json"]:
            with self.subTest(path=candidate):
                result = subprocess.run(
                    ["git", "check-ignore", "-q", candidate], cwd=REPO_ROOT, capture_output=True,
                )
                self.assertEqual(result.returncode, 0, f"{candidate} would be committable")


class TheExampleFleetFileIsSafe(unittest.TestCase):
    """fleet.example.json is committed on purpose, so it is the one file that
    could smuggle a real address past the check above."""

    def setUp(self) -> None:
        self.path = REPO_ROOT / "fleet.example.json"
        self.assertTrue(self.path.is_file(), "fleet.example.json is the documented example")
        self.entries = json.loads(self.path.read_text(encoding="utf-8"))

    def test_it_is_tracked_despite_the_ignore_rules(self) -> None:
        self.assertIn("fleet.example.json", tracked_files())

    def test_it_is_a_bare_array_so_swarmlink_reads_it_as_a_roster(self) -> None:
        # docs/fleet.md: the fleet file IS the roster, extended with optional
        # fields only. A wrapper object would break [RosterEntry].load.
        self.assertIsInstance(self.entries, list)
        self.assertTrue(self.entries)

    def test_every_host_is_a_documentation_address(self) -> None:
        # RFC 5737 TEST-NET-1. Anything routable here is either a real venue or
        # someone else's machine, and both are rule 6 problems.
        for entry in self.entries:
            with self.subTest(duck=entry.get("id")):
                host = entry["host"]
                address = ipaddress.ip_address(host)
                self.assertIn(address, ipaddress.ip_network("192.0.2.0/24"),
                              f"{host} is not an RFC 5737 documentation address")

    def test_it_carries_no_hostname_that_could_name_a_venue(self) -> None:
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn(".local", text)
        self.assertNotIn("@", text)

    def test_ids_and_roles_are_invented_and_well_formed(self) -> None:
        seen = set()
        for entry in self.entries:
            duck_id = entry["id"]
            self.assertRegex(duck_id, r"^[A-Za-z0-9_-]+$",
                             "must match provision_duck.sh's --duck-id charset")
            self.assertNotIn(duck_id, seen, "duplicate id in the example")
            seen.add(duck_id)


if __name__ == "__main__":
    unittest.main()
