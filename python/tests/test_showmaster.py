"""Tests for python/tools/showmaster.py.

These unit-test the cmd retry/ack loop and time_resp correctness directly
against a fake agent UDP socket created in-test. Deliberately does NOT
depend on python/duck_agent (may not exist yet).
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from tools import showmaster  # noqa: E402


def _free_udp_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(2.0)
    return s


class TimeReqTest(unittest.TestCase):
    """time_req -> time_resp exchange correctness (protocol section 1)."""

    def setUp(self) -> None:
        self.agent_sock = _free_udp_socket()
        agent_addr = self.agent_sock.getsockname()
        self.master = showmaster.SwarmMaster(
            roster={"duck-01": agent_addr},
            roles={"lead": "duck-01"},
            port=0,
        )
        self.master.start()

    def tearDown(self) -> None:
        self.master.close()
        self.agent_sock.close()

    def test_time_resp_echoes_t0_and_has_plausible_t1_t2(self) -> None:
        master_addr = ("127.0.0.1", self.master.bound_port)
        t0 = 123456789
        before_ns = time.monotonic_ns()
        req = {"v": 1, "type": "time_req", "duck": "duck-01", "t0": t0}
        self.agent_sock.sendto(json.dumps(req).encode("utf-8"), master_addr)

        # The master also broadcasts its 5 Hz "state" stream unconditionally
        # (incl. while "stopped" -- see docs/swarmlink-protocol.md section 2
        # and F28), to the same roster address this test's socket listens
        # on; skip those to find the time_resp.
        self.agent_sock.settimeout(2.0)
        resp = None
        for _ in range(50):
            data, _ = self.agent_sock.recvfrom(65536)
            msg = json.loads(data.decode("utf-8"))
            if msg.get("type") == "time_resp":
                resp = msg
                break
        after_ns = time.monotonic_ns()
        self.assertIsNotNone(resp, "no time_resp received (only state broadcasts?)")

        self.assertEqual(resp["type"], "time_resp")
        self.assertEqual(resp["t0"], t0)
        self.assertIsInstance(resp["t1"], int)
        self.assertIsInstance(resp["t2"], int)
        # t1/t2 are master's own monotonic clock -- they won't be
        # comparable to the agent's before/after window in absolute
        # terms across processes running the *same* clock domain here,
        # but since this test runs in-process, master and agent share
        # the same monotonic clock, so we can assert ordering directly.
        self.assertLessEqual(resp["t1"], resp["t2"])
        self.assertGreaterEqual(resp["t1"], before_ns)
        self.assertLessEqual(resp["t2"], after_ns)


class CommandRetryTest(unittest.TestCase):
    """Command send/retry/ack loop (protocol section 3)."""

    def setUp(self) -> None:
        self.agent_sock = _free_udp_socket()
        self.agent_addr = self.agent_sock.getsockname()
        self.master = showmaster.SwarmMaster(
            roster={"duck-01": self.agent_addr},
            roles={"lead": "duck-01"},
            port=0,
            cmd_retries=5,
            cmd_timeout_ms=100,
        )
        self.master.start()

    def tearDown(self) -> None:
        self.master.close()
        self.agent_sock.close()

    def _recv_cmd(self, timeout: float = 1.0) -> tuple[dict, tuple]:
        """Receive the next "cmd" datagram, skipping the 5 Hz "state"
        broadcast that the master now sends unconditionally to every
        roster address (docs/swarmlink-protocol.md section 2; F28).
        """
        self.agent_sock.settimeout(timeout)
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            self.agent_sock.settimeout(remaining if remaining > 0 else timeout)
            data, addr = self.agent_sock.recvfrom(65536)
            msg = json.loads(data.decode("utf-8"))
            if msg.get("type") == "cmd":
                return msg, addr

    def test_acks_immediately_on_first_attempt(self) -> None:
        import threading

        result_holder: dict = {}

        def run_send():
            result_holder["results"] = self.master.send_command(
                ["duck-01"], "load", {"show": "demo", "sha256": "deadbeef"}
            )

        t = threading.Thread(target=run_send)
        t.start()

        cmd, addr = self._recv_cmd()
        self.assertEqual(cmd["type"], "cmd")
        self.assertEqual(cmd["cmd"], "load")
        self.assertEqual(cmd["show"], "demo")
        self.assertIn("cmd_id", cmd)

        ack = {"v": 1, "type": "ack", "duck": "duck-01", "cmd_id": cmd["cmd_id"], "ok": True, "error": None}
        self.agent_sock.sendto(json.dumps(ack).encode("utf-8"), addr)

        t.join(timeout=3)
        self.assertEqual(result_holder["results"], {"duck-01": True})

    def test_retries_until_acked_on_third_attempt(self) -> None:
        import threading

        result_holder: dict = {}
        start = time.monotonic()

        def run_send():
            result_holder["results"] = self.master.send_command(
                ["duck-01"], "stop", {}
            )

        t = threading.Thread(target=run_send)
        t.start()

        # Drop the first two sends, ack on the third.
        cmd, addr = self._recv_cmd()
        cmd_id = cmd["cmd_id"]
        cmd2, addr2 = self._recv_cmd()
        self.assertEqual(cmd2["cmd_id"], cmd_id)  # same cmd_id re-sent (idempotent retry)
        cmd3, addr3 = self._recv_cmd()
        self.assertEqual(cmd3["cmd_id"], cmd_id)

        ack = {"v": 1, "type": "ack", "duck": "duck-01", "cmd_id": cmd_id, "ok": True, "error": None}
        self.agent_sock.sendto(json.dumps(ack).encode("utf-8"), addr3)

        t.join(timeout=3)
        elapsed = time.monotonic() - start
        self.assertEqual(result_holder["results"], {"duck-01": True})
        # Two 100ms waits elapsed before the third send got acked.
        self.assertGreaterEqual(elapsed, 0.18)

    def test_gives_up_after_max_retries_and_reports_failure(self) -> None:
        import threading

        result_holder: dict = {}

        def run_send():
            result_holder["results"] = self.master.send_command(
                ["duck-01"], "panic", {}
            )

        t = threading.Thread(target=run_send)
        t.start()

        # Never ack; just drain the retries so the socket doesn't back up.
        for _ in range(5):
            try:
                self._recv_cmd(timeout=1.0)
            except socket.timeout:
                pass

        t.join(timeout=3)
        self.assertFalse(t.is_alive())
        self.assertEqual(result_holder["results"], {"duck-01": False})

    def test_nack_is_reported_as_failure(self) -> None:
        import threading

        result_holder: dict = {}

        def run_send():
            result_holder["results"] = self.master.send_command(
                ["duck-01"], "load", {"show": "demo", "sha256": "bad"}
            )

        t = threading.Thread(target=run_send)
        t.start()

        cmd, addr = self._recv_cmd()
        nack = {
            "v": 1,
            "type": "ack",
            "duck": "duck-01",
            "cmd_id": cmd["cmd_id"],
            "ok": False,
            "error": "hash mismatch",
        }
        self.agent_sock.sendto(json.dumps(nack).encode("utf-8"), addr)

        t.join(timeout=3)
        self.assertEqual(result_holder["results"], {"duck-01": False})


class RosterAndRoleTest(unittest.TestCase):
    def test_parse_duck_arg(self) -> None:
        duck_id, addr = showmaster.parse_duck_arg("duck-01=127.0.0.1:47801")
        self.assertEqual(duck_id, "duck-01")
        self.assertEqual(addr, ("127.0.0.1", 47801))

    def test_parse_role_arg(self) -> None:
        role, duck_id = showmaster.parse_role_arg("lead=duck-01")
        self.assertEqual(role, "lead")
        self.assertEqual(duck_id, "duck-01")

    def test_target_ducks_uses_roles_when_present(self) -> None:
        master = showmaster.SwarmMaster(
            roster={"duck-01": ("127.0.0.1", 1), "duck-02": ("127.0.0.1", 2)},
            roles={"lead": "duck-01"},
            port=0,
        )
        try:
            self.assertEqual(master.target_ducks(), ["duck-01"])
        finally:
            master.sock.close()

    def test_target_ducks_falls_back_to_full_roster_without_roles(self) -> None:
        master = showmaster.SwarmMaster(
            roster={"duck-01": ("127.0.0.1", 1), "duck-02": ("127.0.0.1", 2)},
            roles={},
            port=0,
        )
        try:
            self.assertEqual(master.target_ducks(), ["duck-01", "duck-02"])
        finally:
            master.sock.close()


class LostDuckWatchdogTest(unittest.TestCase):
    """F33: the master must mark a duck lost after
    TELEMETRY_LOST_THRESHOLD_S without telemetry (docs/swarmlink-protocol.md
    section 4: "Master marks a duck lost after 5 s without telemetry"), and
    clear it again once telemetry resumes.
    """

    def setUp(self) -> None:
        self._orig_threshold = showmaster.TELEMETRY_LOST_THRESHOLD_S
        showmaster.TELEMETRY_LOST_THRESHOLD_S = 0.3  # keep the test fast
        self.master = showmaster.SwarmMaster(roster={"duck-01": ("127.0.0.1", 1)}, port=0)
        self.master.start()

    def tearDown(self) -> None:
        self.master.close()
        showmaster.TELEMETRY_LOST_THRESHOLD_S = self._orig_threshold

    def test_duck_marked_lost_after_threshold_then_recovered(self) -> None:
        self.assertEqual(self.master.lost_ducks(), [])
        self.master._handle_telemetry({"duck": "duck-01", "state": "playing", "show_time": 1.0})
        self.assertEqual(self.master.lost_ducks(), [])

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and "duck-01" not in self.master.lost_ducks():
            time.sleep(0.05)
        self.assertEqual(self.master.lost_ducks(), ["duck-01"])

        self.master._handle_telemetry({"duck": "duck-01", "state": "playing", "show_time": 2.0})
        self.assertEqual(self.master.lost_ducks(), [])

    def test_duck_never_heard_from_is_not_reported_lost(self) -> None:
        # Mirrors SwarmMaster.swift's sweepLostDucks: only ducks we have
        # actually received telemetry from at least once are tracked.
        time.sleep(0.6)
        self.assertEqual(self.master.lost_ducks(), [])


class TelemetryConsoleOutputTest(unittest.TestCase):
    """F23: console output is diagnostic only and must never block the recv
    thread -- _handle_telemetry (and the lost/recovered watchdog) route
    output through a queue drained by a dedicated print thread.
    """

    def test_handle_telemetry_returns_promptly_when_console_consumer_stalled(self) -> None:
        master = showmaster.SwarmMaster(roster={"duck-01": ("127.0.0.1", 1)}, port=0)
        master.start()
        try:
            release = threading.Event()

            def blocking_print(*args, **kwargs):
                # Simulates a stalled/full stdout pipe: the print thread
                # parks here instead of returning.
                release.wait(timeout=5)

            with unittest.mock.patch("builtins.print", side_effect=blocking_print):
                master._emit("prime the stalled consumer")
                time.sleep(0.1)  # let the print thread pick it up and park

                t0 = time.monotonic()
                for i in range(50):
                    master._handle_telemetry(
                        {"duck": "duck-01", "state": "playing", "show_time": float(i)}
                    )
                elapsed = time.monotonic() - t0
                self.assertLess(
                    elapsed,
                    1.0,
                    "recv-thread telemetry handling blocked on a stalled console consumer (F23)",
                )
            release.set()
        finally:
            master.close()


class ShowFileHelpersTest(unittest.TestCase):
    def test_show_id_for_strips_duckshow_json_suffix(self) -> None:
        self.assertEqual(showmaster.show_id_for(Path("demo.duckshow.json")), "demo")
        self.assertEqual(
            showmaster.show_id_for(Path("/some/dir/demo.duckshow.json")), "demo"
        )

    def test_sha256_of_file_matches_hashlib(self) -> None:
        import hashlib
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'{"hello": "world"}')
            path = Path(f.name)
        try:
            expected = hashlib.sha256(b'{"hello": "world"}').hexdigest()
            self.assertEqual(showmaster.sha256_of_file(path), expected)
        finally:
            path.unlink()

    def test_sha256_of_demo_show_matches_pinned_literal(self) -> None:
        # Same literal is asserted from the Swift side in
        # DuckShowTests.swift's testShaHelperMatchesPinnedPythonLiteral --
        # a load's sha256 field must mean the same 64 hex chars on both
        # the Mac (SwarmLink) and the duck (this file) for the hash check
        # in docs/swarmlink-protocol.md #3 to mean anything (F67).
        demo = Path(__file__).resolve().parent.parent.parent / "shows" / "demo" / "demo.duckshow.json"
        self.assertEqual(
            showmaster.sha256_of_file(demo),
            "617b07e6dd6596f4bce5cc772072040c9365c1f579decd44cda3244ef7ac496f",
        )


if __name__ == "__main__":
    unittest.main()
