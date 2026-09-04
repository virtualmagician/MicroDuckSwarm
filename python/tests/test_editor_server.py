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
import sys
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import editor_server  # noqa: E402


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
