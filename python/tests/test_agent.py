"""Integration tests for python/duck_agent/agent.py.

Drives a real DuckAgent against two minimal, test-private fakes:
  * FakeRobotd -- an NDJSON JSON-RPC TCP server standing in for robotd,
    recording every received line with its arrival time. (The real,
    protocol-faithful mock duck lives in python/mock_duck and is a
    separate component -- this fake is deliberately minimal and only
    exists to keep these tests self-contained.)
  * FakeMaster -- a UDP socket standing in for the SwarmLink master,
    used to send cmd datagrams and observe time_req/ack/telemetry.

Timing assertions use generous polling windows (not fixed sleeps tied to
the 50 Hz tick rate) so they stay robust on a loaded CI machine.
"""

from __future__ import annotations

import hashlib
import json
import socket
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duck_agent.agent import DuckAgent  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHOWS_DIR = REPO_ROOT / "shows"
DEMO_SHOW_PATH = SHOWS_DIR / "demo" / "demo.duckshow.json"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


DEMO_SHA256 = _sha256_file(DEMO_SHOW_PATH)


class FakeRobotd:
    """Minimal NDJSON JSON-RPC TCP server. Private to this test module --
    see module docstring."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.target = f"127.0.0.1:{self.port}"

        self._lock = threading.Lock()
        self._received: list[tuple[float, dict]] = []  # (monotonic recv time, msg)
        self._stop = threading.Event()
        self._conns: list[socket.socket] = []

        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._conns.append(conn)
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        f = conn.makefile("rb")
        try:
            while not self._stop.is_set():
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line.decode("utf-8"))
                with self._lock:
                    self._received.append((time.monotonic(), msg))
                if "id" in msg:
                    result: object = {}
                    method = msg.get("method")
                    if method == "hello":
                        result = {"api_version": 16, "daemon_version": "fake-1", "revision": "test"}
                    elif method == "robot.mode":
                        result = "idle"
                    elif method == "robot.safeToRestart":
                        result = True
                    resp = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
                    try:
                        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                    except OSError:
                        break
        except (OSError, json.JSONDecodeError):
            pass
        finally:
            try:
                f.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass

    def all_messages(self) -> list[tuple[float, dict]]:
        with self._lock:
            return list(self._received)

    def by_method(self, method: str) -> list[dict]:
        with self._lock:
            return [m for _, m in self._received if m.get("method") == method]

    def wait_for_method(self, method: str, count: int = 1, timeout: float = 2.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matches = self.by_method(method)
            if len(matches) >= count:
                return matches
            time.sleep(0.02)
        return self.by_method(method)


class FakeMaster:
    """Minimal SwarmLink-master-shaped UDP peer. Answers time_req
    immediately (using the same process's monotonic clock, so the
    computed offset is ~0) and lets the test send arbitrary cmd
    datagrams to the agent.
    """

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]

        self._lock = threading.Lock()
        self._received: list[tuple[float, dict, tuple]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                msg = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            with self._lock:
                self._received.append((time.monotonic(), msg, addr))
            if msg.get("type") == "time_req":
                now = time.monotonic_ns()
                resp = {"v": 1, "type": "time_resp", "t0": msg.get("t0"), "t1": now, "t2": now}
                try:
                    self.sock.sendto(json.dumps(resp).encode("utf-8"), addr)
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def send_cmd(self, agent_addr: tuple[str, int], cmd: str, cmd_id: str = None, **fields) -> str:
        cmd_id = cmd_id or str(uuid.uuid4())
        msg = {"v": 1, "type": "cmd", "cmd_id": cmd_id, "cmd": cmd, **fields}
        self.sock.sendto(json.dumps(msg).encode("utf-8"), agent_addr)
        return cmd_id

    def send_raw(self, agent_addr: tuple[str, int], msg: dict) -> None:
        self.sock.sendto(json.dumps(msg).encode("utf-8"), agent_addr)

    def messages(self) -> list[tuple[float, dict, tuple]]:
        with self._lock:
            return list(self._received)

    def acks_for(self, cmd_id: str) -> list[dict]:
        with self._lock:
            return [m for _, m, _ in self._received if m.get("type") == "ack" and m.get("cmd_id") == cmd_id]

    def wait_for_ack(self, cmd_id: str, timeout: float = 2.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            acks = self.acks_for(cmd_id)
            if acks:
                return acks
            time.sleep(0.02)
        return []

    def wait_for(self, predicate, timeout: float = 2.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                matches = [m for _, m, _ in self._received if predicate(m)]
            if matches:
                return matches
            time.sleep(0.02)
        return []


class DuckAgentIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.robotd = FakeRobotd()
        self.master = FakeMaster()
        self.agent = DuckAgent(
            duck_id="duck-test",
            robotd_target=self.robotd.target,
            shows_dir=SHOWS_DIR,
            listen_port=0,
        )
        self.agent.start()
        self.agent_addr = ("127.0.0.1", self.agent.bound_port)
        self.addCleanup(self.agent.stop)
        self.addCleanup(self.robotd.stop)
        self.addCleanup(self.master.stop)

    def _wait_for_state(self, state: str, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.agent.state == state:
                return True
            time.sleep(0.02)
        return False

    def _wait_for_connected(self, timeout: float = 3.0) -> bool:
        # `robotd.connected` flips only after the agent's reader thread has
        # received and parsed the hello *reply* -- three thread handoffs
        # after FakeRobotd has merely recorded the hello line it was sent.
        # Polling this (rather than asserting immediately once the hello
        # shows up in FakeRobotd._received) avoids a real, reproducible
        # TOCTOU race on a loaded/descheduled CI runner (F65).
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.agent.robotd.connected:
                return True
            time.sleep(0.01)
        return self.agent.robotd.connected

    def _load_demo_show(self, role: str = "lead") -> tuple[str, dict]:
        cmd_id = self.master.send_cmd(self.agent_addr, "load", show="demo", sha256=DEMO_SHA256, role=role)
        acks = self.master.wait_for_ack(cmd_id)
        self.assertTrue(acks, "no ack received for load")
        return cmd_id, acks[0]

    # -- handshake -------------------------------------------------------

    def test_hello_sent_on_connect(self) -> None:
        hellos = self.robotd.wait_for_method("hello")
        self.assertEqual(len(hellos), 1)
        self.assertEqual(hellos[0]["params"], {"api_version": 16})
        self.assertTrue(self._wait_for_connected(), "agent.robotd.connected never became true")
        self.assertEqual(self.agent.robotd.hello_reply["api_version"], 16)

    # -- load --------------------------------------------------------------

    def test_load_acks_ok_and_transitions_to_loaded(self) -> None:
        cmd_id, ack = self._load_demo_show()
        self.assertEqual(ack["type"], "ack")
        self.assertEqual(ack["duck"], "duck-test")
        self.assertEqual(ack["cmd_id"], cmd_id)
        self.assertTrue(ack["ok"], ack)
        self.assertIsNone(ack["error"])
        self.assertEqual(self.agent.state, "loaded")
        self.assertEqual(self.agent.show_id, "demo")
        self.assertEqual(self.agent.role, "lead")

    def test_load_nacks_on_sha256_mismatch(self) -> None:
        cmd_id = self.master.send_cmd(self.agent_addr, "load", show="demo", sha256="0" * 64, role="lead")
        acks = self.master.wait_for_ack(cmd_id)
        self.assertTrue(acks)
        self.assertFalse(acks[0]["ok"])
        self.assertIn("sha256", acks[0]["error"])
        self.assertEqual(self.agent.state, "idle")

    def test_load_nacks_on_role_not_in_cast(self) -> None:
        cmd_id = self.master.send_cmd(self.agent_addr, "load", show="demo", sha256=DEMO_SHA256, role="nonexistent")
        acks = self.master.wait_for_ack(cmd_id)
        self.assertTrue(acks)
        self.assertFalse(acks[0]["ok"])
        self.assertIn("not in cast", acks[0]["error"])

    def test_load_nacks_on_unknown_show_id(self) -> None:
        cmd_id = self.master.send_cmd(self.agent_addr, "load", show="does-not-exist", sha256=DEMO_SHA256, role="lead")
        acks = self.master.wait_for_ack(cmd_id)
        self.assertTrue(acks)
        self.assertFalse(acks[0]["ok"])
        self.assertIn("not found", acks[0]["error"])

    # -- command dedup ---------------------------------------------------

    def test_cmd_dedup_reacks_without_reexecuting(self) -> None:
        cmd_id, _ = self._load_demo_show()
        self.assertEqual(self.agent.state, "loaded")

        panic_id = self.master.send_cmd(self.agent_addr, "panic")
        panic_acks = self.master.wait_for_ack(panic_id)
        self.assertTrue(panic_acks and panic_acks[0]["ok"])
        self.assertTrue(self._wait_for_state("idle"))

        acks_before = len(self.master.acks_for(cmd_id))

        # Retransmit the identical load cmd_id, exactly as the real master
        # does up to 5x while waiting for an ack (swarmlink-protocol.md #3).
        self.master.send_raw(
            self.agent_addr,
            {"v": 1, "type": "cmd", "cmd_id": cmd_id, "cmd": "load", "show": "demo", "sha256": DEMO_SHA256, "role": "lead"},
        )

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(self.master.acks_for(cmd_id)) <= acks_before:
            time.sleep(0.02)
        self.assertGreater(len(self.master.acks_for(cmd_id)), acks_before, "expected a re-ACK for the duplicate cmd_id")

        # The critical assertion: dedup means the load was NOT re-executed,
        # so panic's transition to "idle" must still hold.
        self.assertEqual(self.agent.state, "idle")

    # -- play / intents ----------------------------------------------------

    def test_play_arms_then_plays_and_emits_intents(self) -> None:
        self._load_demo_show()
        at_master_time = time.monotonic_ns() + 300_000_000  # 300ms lead
        cmd_id = self.master.send_cmd(self.agent_addr, "play", show="demo", at_master_time=at_master_time, from_show_time=0.0)
        acks = self.master.wait_for_ack(cmd_id)
        self.assertTrue(acks and acks[0]["ok"], acks)

        # Should briefly be armed, then transition to playing once the
        # scheduled start time arrives.
        self.assertTrue(self._wait_for_state("playing", timeout=3.0))

        # Give the 50 Hz tick loop a handful of ticks to emit intents.
        move_msgs = self.robotd.wait_for_method("robot.move", count=2, timeout=1.0)
        self.assertGreaterEqual(len(move_msgs), 2, "expected robot.move notifications while playing")
        head_msgs = self.robotd.wait_for_method("robot.head", count=2, timeout=1.0)
        self.assertGreaterEqual(len(head_msgs), 2, "expected robot.head notifications while playing")

        for m in move_msgs:
            self.assertEqual(m.get("jsonrpc"), "2.0")
            self.assertNotIn("id", m)  # continuous intents are notifications
            self.assertIn("vx", m["params"])

    def test_play_fires_discrete_events(self) -> None:
        self._load_demo_show()
        # Start close to the first scripted sound event (t=4.0 "chirp")
        # so the test doesn't have to wait through the whole show.
        at_master_time = time.monotonic_ns() + 50_000_000
        cmd_id = self.master.send_cmd(
            self.agent_addr, "play", show="demo", at_master_time=at_master_time, from_show_time=3.8
        )
        acks = self.master.wait_for_ack(cmd_id)
        self.assertTrue(acks and acks[0]["ok"], acks)
        self.assertTrue(self._wait_for_state("playing", timeout=3.0))

        sound_reqs = self.robotd.wait_for_method("robot.sound", count=1, timeout=2.0)
        self.assertGreaterEqual(len(sound_reqs), 1)
        self.assertEqual(sound_reqs[0]["params"].get("tag"), "chirp")
        self.assertIn("id", sound_reqs[0])  # discrete events are requests, not notifications

    # -- panic -----------------------------------------------------------

    def test_panic_is_never_nacked_and_stops_emission(self) -> None:
        panic_id = self.master.send_cmd(self.agent_addr, "panic")
        acks = self.master.wait_for_ack(panic_id)
        self.assertTrue(acks and acks[0]["ok"], "panic must never be NACKed")
        self.assertTrue(self._wait_for_state("idle"))

    def test_panic_during_playback_stops_intent_emission(self) -> None:
        self._load_demo_show()
        at_master_time = time.monotonic_ns() + 50_000_000
        cmd_id = self.master.send_cmd(self.agent_addr, "play", show="demo", at_master_time=at_master_time, from_show_time=0.0)
        self.master.wait_for_ack(cmd_id)
        self.assertTrue(self._wait_for_state("playing", timeout=3.0))
        self.robotd.wait_for_method("robot.move", count=1, timeout=1.0)

        panic_id = self.master.send_cmd(self.agent_addr, "panic")
        acks = self.master.wait_for_ack(panic_id)
        self.assertTrue(acks and acks[0]["ok"])
        self.assertTrue(self._wait_for_state("idle"))

        # Panic itself sends one final zero-velocity robot.move (ordered after
        # the FSM reset, under the playback lock), and the fake robotd's
        # reader thread may still be parsing in-flight lines when the ACK
        # arrives. So: wait for the move stream to go quiet, then assert it
        # STAYS quiet and that the last move was the zero one. (The demo
        # show's own early moves are zero as well -- locomotion holds its
        # first keyframe -- so the value alone cannot identify panic's move.)
        def _move_count() -> int:
            return len(self.robotd.by_method("robot.move"))

        deadline = time.monotonic() + 3.0
        last = _move_count()
        quiet_since = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.02)
            now = _move_count()
            if now != last:
                last, quiet_since = now, time.monotonic()
            elif time.monotonic() - quiet_since >= 0.3:  # ~15 tick periods of silence
                break
        moves_at_panic = _move_count()
        time.sleep(0.3)  # several more tick periods
        moves_after = _move_count()
        self.assertEqual(moves_after, moves_at_panic, "no further robot.move notifications after panic")
        last_move = self.robotd.by_method("robot.move")[-1].get("params", {})
        self.assertTrue(
            all(abs(last_move.get(k, 1.0)) < 1e-9 for k in ("vx", "vy", "vyaw")),
            f"panic must end with a zero-velocity robot.move, got {last_move}",
        )

        stop_reqs = self.robotd.by_method("robot.stop")
        self.assertTrue(stop_reqs, "expected a robot.stop request from panic")


if __name__ == "__main__":
    unittest.main()
