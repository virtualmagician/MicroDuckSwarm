#!/usr/bin/env python3
"""scripts/editor_server.py -- local dev server for the duckshow editor.

STDLIB ONLY (CLAUDE.md #1: "Python is stdlib-only ... the tools must run on
any Mac"). A drop-in replacement for `python3 -m http.server`, used by
scripts/edit.sh: it serves the repo root exactly the same way (same files,
same URLs, same directory listings -- nothing about loading a show or an
asset changes), and adds a small JSON API so the editor's "Create Preview"
button (docs/viewer.md "Create Preview (baked physics)") can run
tools/bake itself instead of requiring a terminal command.

    GET  /api/capabilities   -- can this machine bake at all, and which
                                 shows in the repo could be baked?
    POST /api/bake           -- {"show": "/shows/octet/octet.duckshow.json"}
                                 starts a bake, returns a job id immediately.
    GET  /api/bake/<job id>  -- progress, and once finished, the URL of the
                                 written cache plus a summary of its bake log.

Every other request falls straight through to the same static-file handler
`python3 -m http.server` uses.

SECURITY. This process executes a subprocess (tools/bake's own venv
Python, running bake_show.py) in response to an HTTP request. That is a
real capability a network service does not usually have, so it is scoped
deliberately narrowly:

  - Binds 127.0.0.1 ONLY. Never 0.0.0.0. This is not a command-line flag
    on purpose -- there is nothing for a caller of this script to get
    wrong. It must never be reachable from another machine on the network.
  - The only thing an HTTP caller can influence is *which show already in
    this repository* gets baked. POST /api/bake's body is a single
    "show" string; resolve_show_path() below resolves it against the
    repo root and refuses anything that (a) does not end in
    ".duckshow.json", (b) does not resolve to a path inside the repo
    (blocks "..", absolute paths outside the tree, and symlink escapes),
    or (c) does not exist as a file. Nothing else about the request body
    is read.
  - The baker subprocess is always invoked as an explicit argv list --
    never shell=True, never string-interpolated into a shell command --
    so there is no shell metacharacter or quoting hazard to get right.
  - The interpreter (tools/bake/.venv/bin/python3), the script
    (tools/bake/bake_show.py) and the output path are all fixed by this
    server, not the client. No other flag is ever passed through.

See docs/viewer.md "Create Preview (baked physics)" for the feature this
serves and the fidelity/licensing context around tools/bake itself.
"""

from __future__ import annotations

import http.server
import json
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO_ROOT / "tools" / "bake" / ".venv" / "bin" / "python3"
BAKE_SCRIPT = REPO_ROOT / "tools" / "bake" / "bake_show.py"
BAKE_CWD = REPO_ROOT / "tools" / "bake"
ASSETS_DIR = REPO_ROOT / "assets" / "microduck"
SHOWS_DIR = REPO_ROOT / "shows"
# Gitignored output directory for baked pose caches (see .gitignore and
# docs/bake-format.md) -- deliberately NOT under shows/, which is authored
# content, not build output.
BAKES_DIR = REPO_ROOT / "bakes"

HOST = "127.0.0.1"  # see module docstring -- not configurable, on purpose.
DEFAULT_PORT = 8000

# bake_show.py's own per-role progress line (all of its progress output
# goes to stderr via its _eprint() helper, not stdout -- see the quoted
# format string in tools/bake/bake_show.py: `f"  [{role_name}] {pct:3d}%
# ({k}/{n} frames)"`). Matched against every line of the subprocess's
# combined stdout+stderr stream as it arrives.
PROGRESS_RE = re.compile(r"^\s*\[(?P<role>[^\]]+)\]\s+(?P<pct>\d{1,3})%\s+\((?P<k>\d+)/(?P<n>\d+) frames\)")

JOBS: dict[str, "BakeJob"] = {}
JOBS_LOCK = threading.Lock()
LOG_TAIL_CAP = 4000  # lines kept per job; a full octet-sized bake logs a few hundred at most


class ShowPathError(ValueError):
    """A client-supplied show path failed validation (see resolve_show_path)."""


def resolve_show_path(raw: object) -> Path:
    """Validate a client-supplied show path and resolve it to an absolute
    Path inside the repo. Raises ShowPathError (message safe to send back
    to the client) for anything that isn't exactly a real, in-repo
    .duckshow.json file. This is the only place client input is allowed
    to influence a filesystem path or a subprocess argument -- see the
    module docstring's SECURITY section.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ShowPathError("show path is required")
    cleaned = raw.strip().lstrip("/")
    if not cleaned:
        raise ShowPathError("show path is required")
    if not cleaned.endswith(".duckshow.json"):
        raise ShowPathError("show path must end in .duckshow.json")
    root = REPO_ROOT.resolve()
    candidate = (REPO_ROOT / cleaned).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ShowPathError("show path escapes the repository root") from None
    if not candidate.is_file():
        raise ShowPathError(f"show file not found: /{cleaned}")
    return candidate


def _known_shows() -> list[str]:
    """.duckshow.json files under shows/, as repo-root-relative "/shows/..."
    strings -- what /api/capabilities offers the editor as bakeable shows.
    Excludes shows/fixtures/: those are python/duckshow validator test
    fixtures (several deliberately invalid), not authored shows anyone
    would want a preview of.
    """
    if not SHOWS_DIR.is_dir():
        return []
    out = []
    for path in sorted(SHOWS_DIR.rglob("*.duckshow.json")):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[:2] == ("shows", "fixtures"):
            continue
        out.append("/" + rel.as_posix())
    return out


def get_capabilities() -> dict:
    venv_ok = VENV_PYTHON.is_file()
    script_ok = BAKE_SCRIPT.is_file()
    assets_ok = (ASSETS_DIR / "mjcf").is_dir() and (ASSETS_DIR / "policies").is_dir()
    reasons = []
    if not venv_ok:
        reasons.append("tools/bake/.venv not set up (see tools/bake/README.md)")
    if not script_ok:
        reasons.append("tools/bake/bake_show.py not found")
    if not assets_ok:
        reasons.append("assets/microduck/ not populated (see docs/bake-parts.md §2)")
    return {
        "available": venv_ok and script_ok and assets_ok,
        "reason": "; ".join(reasons) if reasons else None,
        "venv_python": venv_ok,
        "bake_script": script_ok,
        "assets": assets_ok,
        "shows": _known_shows(),
    }


def _count_roles(show_abs: Path) -> Optional[int]:
    """Best-effort cast size, read straight off the show JSON's own `cast`
    array -- used only to render "role 3 of 8" in progress; never used for
    validation (bake_show.py does that properly via python/duckshow, and
    refuses to bake a show that fails it). None if anything looks off; a
    missing role_total just means progress text omits the "(x/y)" part.
    """
    try:
        doc = json.loads(show_abs.read_text(encoding="utf-8"))
        cast = doc.get("cast")
        if isinstance(cast, list):
            return len(cast)
    except Exception:
        pass
    return None


def _summarize_cache(cache: dict) -> dict:
    """The bake-log summary docs/viewer.md asks to surface: role/frame
    counts plus anything logged unsimulated or fallen -- read straight
    back off the written duckbake/1 document (docs/bake-format.md), not
    re-derived from the subprocess's progress text.
    """
    roles = cache.get("roles") if isinstance(cache.get("roles"), list) else []
    poses = cache.get("poses") if isinstance(cache.get("poses"), dict) else {}
    frame_total = 0
    for role in roles:
        p = poses.get(role) or {}
        x = p.get("x")
        if isinstance(x, list):
            frame_total += len(x)
    show_meta = cache.get("show") if isinstance(cache.get("show"), dict) else {}
    log = cache.get("log") if isinstance(cache.get("log"), list) else []
    return {
        "roles": len(roles),
        "duration": show_meta.get("duration"),
        "frame_count_total": frame_total,
        "unsimulated_roles": cache.get("unsimulated_roles") or [],
        "fallen_roles": cache.get("fallen_roles") or [],
        "log": log[:200],  # generous cap; a real bake logs at most a few dozen entries
    }


class BakeJob:
    """One POST /api/bake request's lifecycle: running -> done | error.
    All mutable fields are read/written under `lock`; to_json() takes a
    consistent snapshot for GET /api/bake/<id> rather than reading fields
    one at a time while the reader thread might be updating them.
    """

    def __init__(self, job_id: str, show_rel: str, show_abs: Path, output_path: Path, role_total: Optional[int]):
        self.id = job_id
        self.show = show_rel
        self.show_abs = show_abs
        self.output_path = output_path
        self.role_total = role_total
        self.lock = threading.Lock()
        self.status = "running"  # "running" | "done" | "error"
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.returncode: Optional[int] = None
        self.error: Optional[str] = None
        self.summary: Optional[dict] = None
        self.progress: Optional[dict] = None
        self.log_lines: list[str] = []
        self._roles_seen: list[str] = []

    def append_log(self, line: str) -> None:
        with self.lock:
            self.log_lines.append(line)
            if len(self.log_lines) > LOG_TAIL_CAP:
                del self.log_lines[: len(self.log_lines) - LOG_TAIL_CAP]
            m = PROGRESS_RE.match(line)
            if m:
                role = m.group("role")
                if role not in self._roles_seen:
                    self._roles_seen.append(role)
                self.progress = {
                    "role": role,
                    "role_index": self._roles_seen.index(role) + 1,
                    "role_total": self.role_total,
                    "pct": int(m.group("pct")),
                }

    def finish(self, returncode: int) -> None:
        with self.lock:
            self.returncode = returncode
            self.finished_at = time.time()
            if returncode != 0:
                self.status = "error"
                self.error = f"bake_show.py exited {returncode}"
                return
        # Read the cache outside the lock (file I/O) -- only the field
        # assignment below needs it.
        try:
            cache = json.loads(self.output_path.read_text(encoding="utf-8"))
            summary = _summarize_cache(cache)
        except Exception as exc:  # noqa: BLE001 -- report, don't crash the job thread
            with self.lock:
                self.status = "error"
                self.error = f"bake finished but the cache could not be read: {exc}"
            return
        with self.lock:
            self.summary = summary
            self.status = "done"

    def fail_to_start(self, message: str) -> None:
        with self.lock:
            self.status = "error"
            self.error = message
            self.finished_at = time.time()

    def to_json(self) -> dict:
        with self.lock:
            output_url = None
            if self.status == "done":
                output_url = "/" + self.output_path.relative_to(REPO_ROOT).as_posix()
            return {
                "id": self.id,
                "show": self.show,
                "status": self.status,
                "started_at": self.started_at,
                "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 2),
                "progress": self.progress,
                "log_tail": list(self.log_lines[-60:]),
                "returncode": self.returncode,
                "output_url": output_url,
                "summary": self.summary,
                "error": self.error,
            }


def _run_job(job: BakeJob) -> None:
    """Runs in its own thread, one per job: spawns the baker, streams its
    combined output into job.append_log() line by line (so progress is
    real, not a fake spinner -- docs/viewer.md), then records the outcome.
    Never touches shared server state other than this one job.
    """
    argv = [str(VENV_PYTHON), str(BAKE_SCRIPT), str(job.show_abs), str(job.output_path)]
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv list, no shell=True; see module docstring
            argv,
            cwd=str(BAKE_CWD),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # bake_show.py logs progress/errors to stderr; merge so nothing is missed
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        job.fail_to_start(f"failed to start the baker: {exc}")
        return

    assert proc.stdout is not None
    for line in proc.stdout:
        job.append_log(line.rstrip("\n"))
    returncode = proc.wait()
    job.finish(returncode)


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the repo root exactly like `python3 -m http.server` (same
    base class, same `directory=`), plus the small JSON API described in
    the module docstring. Every path other than /api/* falls through to
    SimpleHTTPRequestHandler's normal static-file behaviour untouched.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO_ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:  # keep default stderr access logging
        super().log_message(fmt, *args)

    # -- JSON helpers --------------------------------------------------
    def _json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away (e.g. tab closed mid-poll) -- nothing to do

    def _json_error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    # -- routing ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 -- http.server's naming convention
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/capabilities":
            return self._json(200, get_capabilities())
        if path.startswith("/api/bake/"):
            job_id = path[len("/api/bake/"):]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                return self._json_error(404, "no such bake job")
            return self._json(200, job.to_json())
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/bake":
            return self._handle_post_bake()
        return self._json_error(404, "not found")

    def _handle_post_bake(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > 1_000_000:
            return self._json_error(400, "missing or oversized request body")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json_error(400, "request body must be JSON")
        if not isinstance(body, dict):
            return self._json_error(400, "request body must be a JSON object")

        caps = get_capabilities()
        if not caps["available"]:
            return self._json_error(409, caps["reason"] or "baking is not available on this machine")

        try:
            show_abs = resolve_show_path(body.get("show"))
        except ShowPathError as exc:
            return self._json_error(400, str(exc))

        show_rel = "/" + show_abs.relative_to(REPO_ROOT).as_posix()
        role_total = _count_roles(show_abs)

        BAKES_DIR.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex[:12]
        output_path = BAKES_DIR / f"{job_id}.duckbake.json"

        job = BakeJob(job_id, show_rel, show_abs, output_path, role_total)
        with JOBS_LOCK:
            JOBS[job_id] = job
        threading.Thread(target=_run_job, args=(job,), daemon=True).start()

        self._json(202, job.to_json())


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) > 1:
        print("usage: editor_server.py [PORT]", file=sys.stderr)
        return 2
    try:
        port = int(argv[0]) if argv else DEFAULT_PORT
    except ValueError:
        print(f"editor_server.py: not a valid port: {argv[0]!r}", file=sys.stderr)
        return 2

    server = http.server.ThreadingHTTPServer((HOST, port), Handler)
    caps = get_capabilities()
    print(
        f"editor_server.py: serving {REPO_ROOT} at http://{HOST}:{port}/ "
        f"(bake API: {'available' if caps['available'] else 'unavailable -- ' + (caps['reason'] or '?')})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
