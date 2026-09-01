"""Tests for python/tools/showmaster.py.

These unit-test the cmd retry/ack loop and time_resp correctness directly
against a fake agent UDP socket created in-test. Deliberately does NOT
depend on python/duck_agent (may not exist yet).
"""

from __future__ import annotations

import json
import socket
import sys
import time
import unittest
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

        data, _ = self.agent_sock.recvfrom(65536)
        resp = json.loads(data.decode("utf-8"))
        after_ns = time.monotonic_ns()

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
        self.agent_sock.settimeout(timeout)
        data, addr = self.agent_sock.recvfrom(65536)
        return json.loads(data.decode("utf-8")), addr

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


if __name__ == "__main__":
    unittest.main()
