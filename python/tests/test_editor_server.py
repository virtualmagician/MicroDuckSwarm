"""Tests for scripts/editor_server.py's HTTP behaviour.

Standalone from the rest of the repo, same pattern as test_osc_send.py:
editor_server.py only knows how to serve the working tree and run a bake, not
duckswarm semantics.

The cache-header tests here exist because of a real, expensive-to-diagnose
failure. The editor is a set of separately-fetched ES modules with no bundler
and no content hashes in their URLs. editor_server.py inherited
SimpleHTTPRequestHandler's behaviour of sending Last-Modified but no
Cache-Control, which lets a browser apply *heuristic* freshness (RFC 9111
4.2.2) to each module independently. A user ended up running a freshly-fetched
duckshow-editor.html against a cached duckshow-core.js from before the commit
that added SKILL_DURATIONS_S; the missing export threw inside boot()'s first
setShow(), boot() aborted before it could honour ?show=, and the editor sat
there showing the two-role starter show with no error on screen. It read as
"the whole editor is broken".

The fix is to send no-store on every response. These tests pin that, because
the failure mode is silent, is invisible in any single-file review, and only
reproduces on a browser that happens to hold a stale entry.
"""

from __future__ import annotations

import http.client
import http.server
import json
import sys
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import editor_server  # noqa: E402


def post_json(path: str, body: dict) -> tuple[int, dict]:
    """One POST against a real editor_server.Handler on an ephemeral port."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), editor_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(body).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
        res = conn.getresponse()
        parsed = json.loads(res.read().decode("utf-8"))
        conn.close()
        return res.status, parsed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class SaveEndpoint(unittest.TestCase):
    """POST /api/save WRITES, so its refusals matter more than its successes.

    The editor needs this because a browser cannot reliably write a file back:
    the File System Access API works in Chrome but its handle does not survive
    a reload, and a show opened via ?show= never has one. Writing through the
    local server is the only route that works in every browser and survives a
    reload, which is what makes multi-file authoring (a setlist) practical.
    """

    def setUp(self) -> None:
        self.show = REPO_ROOT / "shows" / "demo" / "demo.duckshow.json"
        self.original = self.show.read_text(encoding="utf-8")
        self.addCleanup(lambda: self.show.write_text(self.original, encoding="utf-8"))

    def test_writes_the_document_back_over_the_file(self) -> None:
        doc = json.loads(self.original)
        doc.setdefault("editor", {}).setdefault("marks", {})["probe"] = {"x": 1.25, "y": 0, "heading": 0}
        text = json.dumps(doc, indent=2)
        status, body = post_json("/api/save", {"show": "/shows/demo/demo.duckshow.json", "show_text": text})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["saved"], "/shows/demo/demo.duckshow.json")
        written = json.loads(self.show.read_text(encoding="utf-8"))
        self.assertEqual(written["editor"]["marks"]["probe"]["x"], 1.25)

    def test_leaves_no_temp_file_behind(self) -> None:
        post_json("/api/save", {"show": "/shows/demo/demo.duckshow.json", "show_text": self.original})
        self.assertEqual(list(self.show.parent.glob("*.tmp")), [],
                         "a .tmp beside the show means the atomic rename did not happen")

    def test_refuses_to_overwrite_a_validator_fixture(self) -> None:
        # shows/fixtures/ is validator test data, several deliberately invalid.
        # An editor overwriting one would corrupt the cross-language parity suite.
        status, body = post_json("/api/save", {
            "show": "/shows/fixtures/valid-baseline.duckshow.json", "show_text": self.original})
        self.assertEqual(status, 403, body)
        self.assertIn("fixtures", body["error"])

    def test_refuses_a_path_outside_the_repo(self) -> None:
        status, body = post_json("/api/save", {
            "show": "/../etc/passwd.duckshow.json", "show_text": self.original})
        self.assertEqual(status, 400, body)
        self.assertIn("escapes", body["error"])

    def test_refuses_a_non_duckshow_extension(self) -> None:
        status, body = post_json("/api/save", {"show": "/CLAUDE.md", "show_text": self.original})
        self.assertEqual(status, 400, body)

    def test_refuses_a_body_that_is_not_a_duckshow_document(self) -> None:
        for text, why in [("not json", "unparseable"), ('{"a":1}', "no format key"), ('[]', "not an object")]:
            with self.subTest(why=why):
                status, body = post_json("/api/save", {
                    "show": "/shows/demo/demo.duckshow.json", "show_text": text})
                self.assertEqual(status, 400, body)
        self.assertEqual(self.show.read_text(encoding="utf-8"), self.original,
                         "a refused save must not have touched the file")

    def test_refuses_a_non_string_body(self) -> None:
        status, body = post_json("/api/save", {"show": "/shows/demo/demo.duckshow.json", "show_text": 42})
        self.assertEqual(status, 400, body)


class SaveSetlistEndpoint(unittest.TestCase):
    """POST /api/save-setlist is the only route that may CREATE a file, so the
    directory and the filename are the whole security story.

    The parent is the fixed SETLISTS_DIR constant and the request supplies a
    name. What matters is that a name which is not a name is refused rather
    than reduced to its basename: silently writing "x.duckset.json" for a
    request that said "/../../x.duckset.json" is contained, but it hides a
    client bug and leaves a file nobody asked for.
    """

    VALID = json.dumps({"format": "duckset/1", "meta": {"name": "probe"}, "entries": []})

    def setUp(self) -> None:
        self.dir = REPO_ROOT / "shows" / "setlists"
        self.existed = self.dir.is_dir()
        self.before = set(self.dir.iterdir()) if self.existed else set()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if not self.dir.is_dir():
            return
        for path in set(self.dir.iterdir()) - self.before:
            path.unlink()
        if not self.existed and not any(self.dir.iterdir()):
            self.dir.rmdir()

    def test_creates_a_setlist_that_did_not_exist(self) -> None:
        status, body = post_json("/api/save-setlist", {
            "setlist": "probe-new.duckset.json", "setlist_text": self.VALID})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["saved"], "/shows/setlists/probe-new.duckset.json")
        written = json.loads((self.dir / "probe-new.duckset.json").read_text(encoding="utf-8"))
        self.assertEqual(written["meta"]["name"], "probe")

    def test_accepts_the_full_path_it_hands_back(self) -> None:
        # The editor round-trips body["saved"] into the next save.
        status, body = post_json("/api/save-setlist", {
            "setlist": "/shows/setlists/probe-round.duckset.json", "setlist_text": self.VALID})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["saved"], "/shows/setlists/probe-round.duckset.json")

    def test_refuses_a_traversal_instead_of_taking_the_basename(self) -> None:
        status, body = post_json("/api/save-setlist", {
            "setlist": "/../../evil.duckset.json", "setlist_text": self.VALID})
        self.assertEqual(status, 400, body)
        self.assertNotIn("evil.duckset.json", [p.name for p in self.dir.iterdir()] if self.dir.is_dir() else [])

    def test_refuses_any_path_separator(self) -> None:
        for name in ["sub/dir/x.duckset.json", "/shows/other/x.duckset.json", "a\\b.duckset.json"]:
            with self.subTest(name=name):
                status, body = post_json("/api/save-setlist", {"setlist": name, "setlist_text": self.VALID})
                self.assertEqual(status, 400, body)

    def test_refuses_a_leading_dot_and_an_overlong_name(self) -> None:
        for name in [".hidden.duckset.json", "-lead.duckset.json", "x" * 65 + ".duckset.json"]:
            with self.subTest(name=name):
                status, body = post_json("/api/save-setlist", {"setlist": name, "setlist_text": self.VALID})
                self.assertEqual(status, 400, body)

    def test_refuses_a_non_duckset_extension(self) -> None:
        status, body = post_json("/api/save-setlist", {
            "setlist": "probe.duckshow.json", "setlist_text": self.VALID})
        self.assertEqual(status, 400, body)

    def test_refuses_a_body_that_is_not_a_duckset_document(self) -> None:
        for text, why in [("not json", "unparseable"), ("[]", "not an object"),
                          ('{"format":"duckshow/1"}', "wrong format")]:
            with self.subTest(why=why):
                status, body = post_json("/api/save-setlist", {
                    "setlist": "probe-bad.duckset.json", "setlist_text": text})
                self.assertEqual(status, 400, body)
        self.assertFalse((self.dir / "probe-bad.duckset.json").exists(),
                         "a refused save must not have created the file")

    def test_leaves_no_temp_file_behind(self) -> None:
        post_json("/api/save-setlist", {"setlist": "probe-tmp.duckset.json", "setlist_text": self.VALID})
        self.assertEqual(list(self.dir.glob("*.tmp")), [])


class ShowIndexEndpoint(unittest.TestCase):
    """GET /api/shows is what gives a setlist block its title, its width and
    the cast it expects, for shows the setlist page never opens."""

    def test_reports_name_duration_and_cast_for_every_show(self) -> None:
        req = ServedRequest("/api/shows")
        with req as res:
            self.assertEqual(res.status, 200)
        shows = json.loads(req.body)["shows"]
        by_path = {s["path"]: s for s in shows}
        demo = by_path.get("/shows/demo/demo.duckshow.json")
        self.assertIsNotNone(demo, sorted(by_path))
        self.assertEqual(demo["duration"], 20.0)
        self.assertEqual(demo["roles"], ["lead", "wing"])
        self.assertTrue(demo["name"])

    def test_excludes_the_validator_fixtures(self) -> None:
        # Several are deliberately invalid; offering them as setlist entries
        # would put a show that cannot load in front of the author.
        req = ServedRequest("/api/shows")
        with req:
            pass
        shows = json.loads(req.body)["shows"]
        self.assertEqual([s for s in shows if "/fixtures/" in s["path"]], [])


class ServedRequest:
    """One request against a real editor_server.Handler on an ephemeral port."""

    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        # Port 0 -> the OS picks a free one, so tests never collide with a
        # developer's own `./scripts/edit.sh` session on 8000.
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), editor_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        conn.request("GET", self.path)
        self.response = conn.getresponse()
        self.body = self.response.read()
        conn.close()
        return self.response

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        return False


class TestNoCaching(unittest.TestCase):
    """Every response must forbid storing, whatever produced it."""

    def assert_no_store(self, path: str) -> None:
        with ServedRequest(path) as res:
            self.assertEqual(res.status, 200, f"{path} should be served")
            values = res.headers.get_all("Cache-Control") or []
            self.assertEqual(
                len(values), 1,
                f"{path} must send exactly one Cache-Control header, got {values!r}",
            )
            self.assertIn(
                "no-store", values[0].lower(),
                f"{path} must be uncacheable, got Cache-Control: {values[0]!r}",
            )

    def test_es_module_is_uncacheable(self):
        # The specific asset whose staleness caused the original failure.
        self.assert_no_store("/editor/duckshow-core.js")

    def test_editor_html_is_uncacheable(self):
        self.assert_no_store("/editor/duckshow-editor.html")

    def test_show_json_is_uncacheable(self):
        # A show edited on disk must never be served from a browser cache --
        # the editor would silently open a stale copy of the user's own work.
        self.assert_no_store("/shows/octet/octet.duckshow.json")

    def test_json_api_sends_exactly_one_cache_control(self):
        # _json() sets its own no-store; end_headers() must not add a second.
        self.assert_no_store("/api/capabilities")

    def test_every_editor_module_is_uncacheable(self):
        # Guards the whole module set rather than one file, since the skew that
        # broke the editor only needs ONE of them to go stale.
        for js in sorted((REPO_ROOT / "editor").glob("*.js")):
            with self.subTest(module=js.name):
                self.assert_no_store(f"/editor/{js.name}")

    def test_404_also_sends_cache_control(self):
        # A cached 404 for an asset added later is its own version-skew trap.
        with ServedRequest("/editor/does-not-exist.js") as res:
            self.assertEqual(res.status, 404)
            values = res.headers.get_all("Cache-Control") or []
            self.assertEqual(len(values), 1, f"expected one Cache-Control, got {values!r}")
            self.assertIn("no-store", values[0].lower())


class TestStillServesTheWorkingTree(unittest.TestCase):
    """The no-cache override must not disturb ordinary static serving."""

    def test_serves_real_file_contents(self):
        req = ServedRequest("/editor/duckshow-core.js")
        with req:
            pass
        on_disk = (REPO_ROOT / "editor" / "duckshow-core.js").read_bytes()
        self.assertEqual(req.body, on_disk, "served bytes must match the working tree")

    def test_serves_the_export_the_editor_depends_on(self):
        # Directly pins the contract whose violation caused the original bug:
        # duckshow-editor.html reads core.SKILL_DURATIONS_S at render time.
        req = ServedRequest("/editor/duckshow-core.js")
        with req:
            pass
        self.assertIn(b"export const SKILL_DURATIONS_S", req.body)


if __name__ == "__main__":
    unittest.main()
