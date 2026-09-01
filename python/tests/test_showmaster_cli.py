"""Coverage for python/tools/showmaster.py behavior that test_showmaster.py
doesn't exercise (F68): main()'s exit codes, panic() addressing the whole
roster while load()/play()/seek() address only cast ducks, the per-duck
`role` field showmaster attaches to a load command, and the state loop's
5 Hz broadcast with the armed -> playing auto-advance.

Standalone from python/duck_agent, same as test_showmaster.py: these all
drive SwarmMaster/main() against a fake agent UDP socket created in-test.
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from tools import showmaster  # noqa: E402

DEMO_SHOW_PATH = Path(__file__).resolve().parent.parent.parent / "shows" / "demo" / "demo.duckshow.json"


def _free_udp_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(2.0)
    return s


def _recv_cmd(sock: socket.socket, timeout: float = 1.0):
    """Receive the next "cmd" datagram, skipping "state" broadcasts."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, None
        sock.settimeout(remaining)
        try:
            data, addr = sock.recvfrom(65536)
        except socket.timeout:
            return None, None
        msg = json.loads(data.decode("utf-8"))
        if msg.get("type") == "cmd":
            return msg, addr


class MainExitCodeTest(unittest.TestCase):
    """showmaster.main()'s exit codes: 0 on all-ACK, 1 on any NACK/timeout.

    scripts/e2e_demo.sh runs `showmaster.py ... run` under `set -e`, so a
    regression that returned 0 on a partial NACK would let a half-loaded
    flock 'play' with no CI signal until the (much coarser) e2e verifier.
    """

    def setUp(self) -> None:
        self.agent_sock = _free_udp_socket()
        self.agent_addr = self.agent_sock.getsockname()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.addCleanup(self._stop_responder)
        self.addCleanup(self.agent_sock.close)

    def _stop_responder(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _serve(self, decide) -> None:
        """`decide(cmd_dict) -> "ack" | "nack" | "silent"` for every cmd
        datagram the fake agent receives (any type: load/play/etc.)."""

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.agent_sock.settimeout(0.2)
                    data, addr = self.agent_sock.recvfrom(65536)
                except socket.timeout:
                    continue
                except OSError:
                    return
                try:
                    msg = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if msg.get("type") != "cmd":
                    continue
                action = decide(msg)
                if action == "silent":
                    continue
                ok = action == "ack"
                resp = {
                    "v": 1,
                    "type": "ack",
                    "duck": "duck-01",
                    "cmd_id": msg["cmd_id"],
                    "ok": ok,
                    "error": None if ok else "nope",
                }
                self.agent_sock.sendto(json.dumps(resp).encode("utf-8"), addr)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def _argv(self, *cmd_args: str) -> list[str]:
        return [
            "--duck", f"duck-01=127.0.0.1:{self.agent_addr[1]}",
            "--role", "lead=duck-01",
            "--port", "0",
            "--cmd-retries", "2",
            "--cmd-timeout-ms", "50",
            *cmd_args,
        ]

    def _run_main(self, *cmd_args: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = showmaster.main(self._argv(*cmd_args))
        return rc, buf.getvalue()

    def test_exit_0_on_all_ack(self) -> None:
        self._serve(lambda cmd: "ack")
        rc, out = self._run_main("load", str(DEMO_SHOW_PATH))
        self.assertEqual(rc, 0)
        self.assertIn("[load] duck-01: ACK", out)

    def test_exit_1_on_nack(self) -> None:
        self._serve(lambda cmd: "nack")
        rc, out = self._run_main("load", str(DEMO_SHOW_PATH))
        self.assertEqual(rc, 1)
        self.assertIn("[load] duck-01: TIMEOUT/NACK", out)

    def test_exit_1_on_timeout(self) -> None:
        self._serve(lambda cmd: "silent")
        rc, out = self._run_main("load", str(DEMO_SHOW_PATH))
        self.assertEqual(rc, 1)
        self.assertIn("[load] duck-01: TIMEOUT/NACK", out)

    def test_panic_exit_code_follows_acks_too(self) -> None:
        self._serve(lambda cmd: "ack")
        rc, out = self._run_main("panic")
        self.assertEqual(rc, 0)
        self.assertIn("[panic] duck-01: ACK", out)


class PanicVsLoadRosterTest(unittest.TestCase):
    """panic() must reach every roster duck, not just the current cast
    (docs/swarmlink-protocol.md #3: "Never NACKed", CLAUDE.md invariant
    "panic always works from any state") -- load/play/seek only address
    target_ducks(). A regression from panic()'s `sorted(self.roster.keys())`
    to `self.target_ducks()` would pass every test in test_showmaster.py's
    RosterAndRoleTest (which only checks target_ducks() itself, never
    panic()'s own targeting).
    """

    def setUp(self) -> None:
        self.sock1 = _free_udp_socket()
        self.sock2 = _free_udp_socket()
        self.addr1 = self.sock1.getsockname()
        self.addr2 = self.sock2.getsockname()
        # duck-02 has no role: not part of the cast.
        self.master = showmaster.SwarmMaster(
            roster={"duck-01": self.addr1, "duck-02": self.addr2},
            roles={"lead": "duck-01"},
            port=0,
            cmd_retries=1,
            cmd_timeout_ms=80,
        )
        self.master.start()
        self.addCleanup(self.master.close)
        self.addCleanup(self.sock1.close)
        self.addCleanup(self.sock2.close)

    def test_load_only_addresses_cast_duck_and_carries_role_field(self) -> None:
        t = threading.Thread(target=self.master.load, args=(DEMO_SHOW_PATH,), daemon=True)
        t.start()
        try:
            cmd1, addr1 = _recv_cmd(self.sock1, timeout=1.0)
            self.assertIsNotNone(cmd1, "the cast duck must receive load")
            self.assertEqual(cmd1["role"], "lead", "load must carry the per-duck role field")
            ack = {"v": 1, "type": "ack", "duck": "duck-01", "cmd_id": cmd1["cmd_id"], "ok": True, "error": None}
            self.sock1.sendto(json.dumps(ack).encode("utf-8"), addr1)

            cmd2, _ = _recv_cmd(self.sock2, timeout=0.4)
            self.assertIsNone(cmd2, "a duck with no assigned role must not receive load")
        finally:
            t.join(timeout=3)

    def test_panic_addresses_the_full_roster_including_uncast_ducks(self) -> None:
        t = threading.Thread(target=self.master.panic, daemon=True)
        t.start()
        try:
            cmd1, addr1 = _recv_cmd(self.sock1, timeout=1.0)
            cmd2, addr2 = _recv_cmd(self.sock2, timeout=1.0)
            self.assertIsNotNone(cmd1, "the cast duck must receive panic")
            self.assertIsNotNone(cmd2, "the un-cast roster duck must ALSO receive panic")
            self.assertEqual(cmd1["cmd"], "panic")
            self.assertEqual(cmd2["cmd"], "panic")
            for sock, addr, cmd, duck_id in (
                (self.sock1, addr1, cmd1, "duck-01"),
                (self.sock2, addr2, cmd2, "duck-02"),
            ):
                ack = {"v": 1, "type": "ack", "duck": duck_id, "cmd_id": cmd["cmd_id"], "ok": True, "error": None}
                sock.sendto(json.dumps(ack).encode("utf-8"), addr)
        finally:
            t.join(timeout=3)


class StateLoopTest(unittest.TestCase):
    """The 5 Hz transport-state broadcast (docs/swarmlink-protocol.md #2)
    and the armed -> playing auto-advance at the scheduled epoch.
    """

    def test_broadcasts_at_5hz_and_advances_armed_to_playing(self) -> None:
        sock = _free_udp_socket()
        addr = sock.getsockname()
        master = showmaster.SwarmMaster(
            roster={"duck-01": addr}, roles={"lead": "duck-01"}, port=0, cmd_retries=1, cmd_timeout_ms=80
        )
        master.start()
        stop = threading.Event()
        # Record state broadcasts from the very first datagram: on a slow
        # runner the whole ARMED phase can elapse while play() is still
        # waiting on ACKs, so collecting only after play() returns would
        # miss it (observed on a 2-vCPU CI runner).
        states: list[dict] = []
        states_lock = threading.Lock()

        def auto_ack() -> None:
            while not stop.is_set():
                try:
                    sock.settimeout(0.2)
                    data, a = sock.recvfrom(65536)
                except socket.timeout:
                    continue
                except OSError:
                    return
                msg = json.loads(data.decode("utf-8"))
                if msg.get("type") == "cmd":
                    ack = {"v": 1, "type": "ack", "duck": "duck-01", "cmd_id": msg["cmd_id"], "ok": True, "error": None}
                    sock.sendto(json.dumps(ack).encode("utf-8"), a)
                elif msg.get("type") == "state":
                    with states_lock:
                        states.append(msg)

        responder = threading.Thread(target=auto_ack, daemon=True)
        responder.start()
        try:
            result = master.play(DEMO_SHOW_PATH, lead_ms=600, from_show_time=0.0)
            self.assertEqual(result, {"duck-01": True})

            # Wait until we have seen the ARMED phase and a few PLAYING
            # broadcasts (or give up after a generous deadline and let the
            # assertions below explain what was actually observed).
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                with states_lock:
                    transports_so_far = [s["transport"] for s in states]
                if "armed" in transports_so_far and transports_so_far.count("playing") >= 3:
                    break
                time.sleep(0.05)
            stop.set()
            responder.join(timeout=2)
            with states_lock:
                states = list(states)
            self.assertGreaterEqual(len(states), 4, "expected ~5 Hz state broadcasts")
            seqs = [s["seq"] for s in states]
            self.assertEqual(seqs, sorted(seqs), "seq must be non-decreasing")
            self.assertEqual(len(seqs), len(set(seqs)), "seq must not repeat")
            self.assertTrue(all(isinstance(s["master_time"], int) for s in states))
            transports = [s["transport"] for s in states]
            self.assertIn("armed", transports, "must observe the ARMED phase before the epoch")
            self.assertIn("playing", transports, "must auto-advance to PLAYING at the epoch")
            first_playing = transports.index("playing")
            first_armed = transports.index("armed")
            # Broadcasts start with the master's resting "stopped" state; that
            # is only allowed *before* ARMED. From ARMED on, the sequence must
            # be armed... then playing..., never stopped and never playing
            # before armed.
            self.assertTrue(
                all(t == "stopped" for t in transports[:first_armed]),
                f"only 'stopped' may precede the ARMED phase: {transports}",
            )
            self.assertTrue(
                all(t == "armed" for t in transports[first_armed:first_playing]),
                f"transport must not report playing before armed: {transports}",
            )
            self.assertNotIn("stopped", transports[first_armed:], f"no 'stopped' after ARMED: {transports}")
            show_times = [s["show_time"] for s in states]
            self.assertTrue(
                all(b >= a - 1e-6 for a, b in zip(show_times, show_times[1:])),
                f"show_time must be non-decreasing: {show_times}",
            )
        finally:
            stop.set()
            responder.join(timeout=2)
            master.close()
            sock.close()


if __name__ == "__main__":
    unittest.main()
