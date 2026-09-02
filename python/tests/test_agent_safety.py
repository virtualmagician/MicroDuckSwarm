"""Show-night safety tests for python/duck_agent: every exit from PLAYING
zeroes locomotion and sends robot.stop, faults are reported (not
silently downgraded), the show clock is anchored at the *scheduled*
instant, seek/stale-play/no-sync edge cases, held-sound release, panic
latency with a hung robotd, and robotd_client reconnect robustness.

Builds on the FakeRobotd/FakeMaster fakes from test_agent.py (adding
failure injection and a master that never answers time_req). Timing
assertions use generous polling windows so they stay robust on a loaded
CI machine.
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duck_agent.agent import DuckAgent  # noqa: E402
from duck_agent.robotd_client import RobotdClient, RobotdDisconnected  # noqa: E402
from tests.test_agent import DEMO_SHA256, SHOWS_DIR, FakeMaster, FakeRobotd  # noqa: E402

NS = 1_000_000_000


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _swallow_oserror(fn, *args) -> None:
    try:
        fn(*args)
    except OSError:
        pass


def _rst_close(conn: socket.socket) -> None:
    """Close with SO_LINGER {1, 0}: an abortive RST rather than a FIN."""
    try:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass


class ScriptedRobotd(FakeRobotd):
    """FakeRobotd with failure injection: `fail` maps a method to a
    JSON-RPC error (code, message); `hang` names methods that never get
    a reply; `delay` maps a method to seconds its (successful) reply is
    held back without blocking the connection's reader (a slow actuator,
    not a stalled daemon); `hello_api_version` fakes a version mismatch;
    `sever()` hard-drops every live connection (and, with refuse_new,
    every later one too, so the agent stays disconnected).
    """

    def __init__(
        self,
        fail: Optional[dict[str, tuple[int, str]]] = None,
        hang: Optional[set[str]] = None,
        delay: Optional[dict[str, float]] = None,
        hello_api_version: int = 16,
    ) -> None:
        self.fail = dict(fail or {})
        self.hang = set(hang or ())
        self.delay = dict(delay or {})
        self.hello_api_version = hello_api_version
        self.refuse_new = False
        super().__init__()

    def sever(self, refuse_new: bool = False) -> None:
        self.refuse_new = refuse_new
        for c in list(self._conns):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _handle_conn(self, conn: socket.socket) -> None:
        if self.refuse_new:
            _rst_close(conn)
            return
        f = conn.makefile("rb")
        send_lock = threading.Lock()  # delayed replies come from timer threads

        def send(resp: dict) -> None:
            with send_lock:
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))

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
                if "id" not in msg:
                    continue
                method = msg.get("method")
                if method in self.hang:
                    continue
                if method in self.fail:
                    code, message = self.fail[method]
                    resp = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": code, "message": message}}
                else:
                    result: object = {}
                    if method == "hello":
                        result = {"api_version": self.hello_api_version, "daemon_version": "fake-1", "revision": "test"}
                    elif method == "robot.mode":
                        result = "idle"
                    resp = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
                if method in self.delay:
                    timer = threading.Timer(self.delay[method], lambda r=resp: _swallow_oserror(send, r))
                    timer.daemon = True
                    timer.start()
                    continue
                try:
                    send(resp)
                except OSError:
                    break
        except (OSError, json.JSONDecodeError):
            pass
        finally:
            try:
                f.close()
            except OSError:
                pass


class SilentMaster(FakeMaster):
    """A master that never answers time_req (the agent gets no clock samples)."""

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


def _write_show(
    directory: Path,
    show_id: str,
    *,
    duration: Optional[float] = 10.0,
    events: Optional[list] = None,
    locomotion: Optional[list] = None,
    policies: Optional[list] = None,
) -> tuple[Path, str]:
    meta: dict = {"name": show_id}
    if duration is not None:
        meta["duration"] = duration
    tracks: dict = {}
    if events:
        tracks["events"] = events
    if locomotion:
        tracks["locomotion"] = locomotion
    doc = {
        "format": "duckshow/1",
        "meta": meta,
        "requires": {"policies": policies or []},
        "cast": [{"role": "lead"}],
        "tracks": {"lead": tracks},
    }
    path = directory / f"{show_id}.duckshow.json"
    path.write_text(json.dumps(doc, indent=1))
    return path, _sha256_file(path)


class _AgentTestBase(unittest.TestCase):
    """Builds one agent per test; subclasses/tests pick the fakes."""

    def _start(
        self,
        robotd: Optional[FakeRobotd] = None,
        master: Optional[FakeMaster] = None,
        shows_dir: Path = SHOWS_DIR,
    ) -> None:
        self.robotd = robotd or ScriptedRobotd()
        self.master = master or FakeMaster()
        self.agent = DuckAgent(duck_id="duck-test", robotd_target=self.robotd.target, shows_dir=shows_dir, listen_port=0)
        self.agent.start()
        self.agent_addr = ("127.0.0.1", self.agent.bound_port)
        self.addCleanup(self.agent.stop)
        self.addCleanup(self.robotd.stop)
        self.addCleanup(self.master.stop)

    def _wait(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def _wait_for_state(self, state: str, timeout: float = 3.0) -> bool:
        return self._wait(lambda: self.agent.state == state, timeout)

    def _wait_for_sync(self) -> None:
        self.assertTrue(self._wait(lambda: self.agent.clock.sample_count() >= 1), "no time sample")

    def _load(self, show: str = "demo", sha256: str = DEMO_SHA256, role: str = "lead") -> dict:
        cmd_id = self.master.send_cmd(self.agent_addr, "load", show=show, sha256=sha256, role=role)
        acks = self.master.wait_for_ack(cmd_id)
        self.assertTrue(acks, "no ack received for load")
        return acks[0]

    def _play(self, from_show_time: float = 0.0, lead_s: float = 0.05, show: str = "demo", at_master_time: Optional[int] = None) -> dict:
        at = at_master_time if at_master_time is not None else time.monotonic_ns() + int(lead_s * NS)
        cmd_id = self.master.send_cmd(self.agent_addr, "play", show=show, at_master_time=at, from_show_time=from_show_time)
        acks = self.master.wait_for_ack(cmd_id)
        self.assertTrue(acks, "no ack received for play")
        return acks[0]

    def _last_move(self) -> Optional[dict]:
        moves = self.robotd.by_method("robot.move")
        return moves[-1]["params"] if moves else None

    def _start_walking(self) -> None:
        """Demo show, lead role, from t=3.6: locomotion vx=0.1 from 3.5 to 5.5."""
        self._load()
        self._wait_for_sync()
        ack = self._play(from_show_time=3.6)
        self.assertTrue(ack["ok"], ack)
        self.assertTrue(self._wait_for_state("playing"))
        self.assertTrue(self._wait(lambda: (self._last_move() or {}).get("vx", 0.0) > 0.05), "duck never started walking")


# ---------------------------------------------------------------------------
# Fault entry always stops the robot
# ---------------------------------------------------------------------------


class FaultStopsRobotTest(_AgentTestBase):
    def test_event_failure_enters_fault_and_stops_robot(self) -> None:
        # robotd answers the t=4.0 chirp with app error 1 BUSY.
        self._start(robotd=ScriptedRobotd(fail={"robot.sound": (1, "BUSY")}))
        self._start_walking()  # from 3.6, so the chirp at 4.0 fires while walking
        self.assertTrue(self._wait_for_state("fault"), self.agent.state)
        self.assertIn("event sound failed", self.agent.last_error)
        self.assertTrue(self._wait(lambda: len(self.robotd.by_method("robot.stop")) >= 1), "fault must send robot.stop")
        self.assertEqual(self._last_move(), {"vx": 0.0, "vy": 0.0, "vyaw": 0.0}, "locomotion must be zeroed on fault")
        # and nothing is emitted afterwards
        n = len(self.robotd.by_method("robot.move"))
        time.sleep(0.2)
        self.assertEqual(len(self.robotd.by_method("robot.move")), n)

    def test_end_of_show_stop_failure_enters_fault(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _, sha = _write_show(Path(d), "short", duration=0.4)
            self._start(robotd=ScriptedRobotd(fail={"robot.stop": (1, "BUSY")}), shows_dir=Path(d))
            self.assertTrue(self._load("short", sha)["ok"])
            self._wait_for_sync()
            self.assertTrue(self._play(show="short")["ok"])
            self.assertTrue(self._wait_for_state("fault", timeout=4.0), self.agent.state)
            self.assertIn("end-of-show stop failed", self.agent.last_error)
            self.assertFalse(self._play(show="short")["ok"], "play must be NACKed from fault")

    def test_disconnect_while_playing_faults_and_stop_is_sent_on_reconnect(self) -> None:
        self._start()
        self._start_walking()
        self.assertEqual(len(self.robotd.by_method("robot.stop")), 0)
        self.robotd.sever()
        self.assertTrue(self._wait_for_state("fault"), self.agent.state)
        self.assertEqual(self.agent.last_error, "robotd disconnected")
        # The agent reconnects (backoff 0.2 s); the owed stop goes out then.
        self.assertTrue(self._wait(lambda: len(self.robotd.by_method("robot.stop")) >= 1, timeout=4.0))
        self.assertEqual(self._last_move(), {"vx": 0.0, "vy": 0.0, "vyaw": 0.0})

    def test_play_while_robotd_disconnected_is_nacked(self) -> None:
        self._start()
        self._load()
        self._wait_for_sync()
        self.robotd.sever(refuse_new=True)
        self.assertTrue(self._wait(lambda: not self.agent.robotd.connected))
        self.assertEqual(self.agent.state, "loaded")  # link loss while LOADED is not a fault...
        ack = self._play(lead_s=0.5)
        self.assertFalse(ack["ok"])  # ...but nothing may be scheduled into a dead socket
        self.assertIn("robotd", ack["error"])
        self.assertEqual(self.agent.state, "loaded")


# ---------------------------------------------------------------------------
# Every exit from PLAYING zeroes locomotion + robot.stop; held sounds released
# ---------------------------------------------------------------------------


class ExitPlayingTest(_AgentTestBase):
    def test_load_while_playing_stops_robot_first(self) -> None:
        self._start()
        self._start_walking()
        stops_before = len(self.robotd.by_method("robot.stop"))
        ack = self._load()
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(self.agent.state, "loaded")
        self.assertEqual(len(self.robotd.by_method("robot.stop")), stops_before + 1)
        self.assertEqual(self._last_move(), {"vx": 0.0, "vy": 0.0, "vyaw": 0.0})

    def test_play_while_playing_stops_then_rearms(self) -> None:
        self._start()
        self._start_walking()
        ack = self._play(from_show_time=0.0, lead_s=1.0)
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(self.agent.state, "armed")
        self.assertEqual(len(self.robotd.by_method("robot.stop")), 1)
        self.assertEqual(self._last_move(), {"vx": 0.0, "vy": 0.0, "vyaw": 0.0})
        n = len(self.robotd.by_method("robot.move"))
        time.sleep(0.3)
        self.assertEqual(len(self.robotd.by_method("robot.move")), n, "no intents while ARMED")
        self.assertTrue(self._wait_for_state("playing"))

    def test_held_sound_released_at_end_of_show(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _, sha = _write_show(Path(d), "hold", duration=0.6, events=[{"t": 0.1, "sound": "alarm", "hold": 5.0}])
            self._start(shows_dir=Path(d))
            self.assertTrue(self._load("hold", sha)["ok"])
            self._wait_for_sync()
            self.assertTrue(self._play(show="hold")["ok"])
            sounds = self.robotd.wait_for_method("robot.sound", count=2, timeout=4.0)
            self.assertEqual([s["params"] for s in sounds], [{"tag": "alarm", "hold": True}, {"tag": "alarm", "hold": False}])
            self.assertTrue(self._wait_for_state("loaded"))
            self.assertTrue(self.robotd.by_method("robot.stop"))

    def test_held_sound_released_on_stop(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _, sha = _write_show(Path(d), "hold", duration=20.0, events=[{"t": 0.1, "sound": "alarm", "hold": 5.0}])
            self._start(shows_dir=Path(d))
            self.assertTrue(self._load("hold", sha)["ok"])
            self._wait_for_sync()
            self.assertTrue(self._play(show="hold")["ok"])
            self.robotd.wait_for_method("robot.sound", count=1, timeout=3.0)
            stop_id = self.master.send_cmd(self.agent_addr, "stop")
            acks = self.master.wait_for_ack(stop_id)
            self.assertTrue(acks and acks[0]["ok"], acks)
            sounds = self.robotd.wait_for_method("robot.sound", count=2, timeout=2.0)
            self.assertEqual(sounds[-1]["params"], {"tag": "alarm", "hold": False})
            self.assertEqual(self.agent.state, "loaded")


# ---------------------------------------------------------------------------
# Scheduling: epoch anchored at the scheduled instant, live offset, no-sync
# ---------------------------------------------------------------------------


class SchedulingTest(_AgentTestBase):
    def test_late_play_within_grace_runs_at_master_show_time(self) -> None:
        self._start()
        self._load()
        self._wait_for_sync()
        at_master_time = time.monotonic_ns() - 200_000_000  # arrived 200 ms after the start instant
        ack = self._play(at_master_time=at_master_time)
        self.assertTrue(ack["ok"], ack)
        self.assertTrue(self._wait_for_state("playing"))
        time.sleep(0.3)
        now_ns = time.monotonic_ns()
        expected = (now_ns - at_master_time) / 1e9  # master/agent share one clock here (offset ~0)
        self.assertAlmostEqual(self.agent._current_show_time(now_ns), expected, delta=0.06)

    def test_on_time_play_is_not_quantized_to_the_tick(self) -> None:
        self._start()
        self._load()
        self._wait_for_sync()
        at_master_time = time.monotonic_ns() + 300_000_000
        self.assertTrue(self._play(at_master_time=at_master_time)["ok"])
        self.assertTrue(self._wait_for_state("playing"))
        time.sleep(0.2)
        now_ns = time.monotonic_ns()
        self.assertAlmostEqual(self.agent._current_show_time(now_ns), (now_ns - at_master_time) / 1e9, delta=0.03)

    def test_event_exactly_at_start_fires(self) -> None:
        self._start()
        self._load()
        self._wait_for_sync()
        self.assertTrue(self._play(from_show_time=4.0)["ok"])  # demo: {"t": 4.0, "sound": "chirp"}
        sounds = self.robotd.wait_for_method("robot.sound", count=1, timeout=2.0)
        self.assertTrue(sounds, "event at t == from_show_time must fire on the first tick")
        self.assertEqual(sounds[0]["params"]["tag"], "chirp")

    def test_play_without_time_sync_is_nacked(self) -> None:
        self._start(master=SilentMaster())
        self.assertTrue(self._load()["ok"])
        self.assertEqual(self.agent.clock.sample_count(), 0)
        ack = self._play(lead_s=1.0)
        self.assertFalse(ack["ok"])
        self.assertIn("no time sync", ack["error"])
        self.assertEqual(self.agent.state, "loaded")

    def test_armed_start_tracks_refined_clock_offset(self) -> None:
        self._start(master=SilentMaster())
        self.assertTrue(self._load()["ok"])
        # Sample 1 (noisy, rtt 2 ms): master is 1.0 s ahead of us.
        L = time.monotonic_ns()
        self.agent.clock.record_exchange(t0=L, t1=L + NS + 1_000_000, t2=L + NS + 1_000_000, t3=L + 2_000_000)
        t_play = time.monotonic_ns()
        at_master_time = t_play + NS + int(1.5 * NS)  # local start = now + 1.5 s under sample 1
        self.assertTrue(self._play(at_master_time=at_master_time)["ok"])
        self.assertEqual(self.agent.state, "armed")
        # Sample 2 (tighter, rtt 1 ms -> new min-RTT best): master is really 2.0 s ahead,
        # so the same at_master_time is only 0.5 s away.
        L2 = time.monotonic_ns()
        self.agent.clock.record_exchange(t0=L2, t1=L2 + 2 * NS + 500_000, t2=L2 + 2 * NS + 500_000, t3=L2 + 1_000_000)
        time.sleep(0.2)
        self.assertEqual(self.agent.state, "armed")
        self.assertTrue(self._wait_for_state("playing", timeout=0.8), "start must follow the refined offset")
        self.assertLess((time.monotonic_ns() - t_play) / 1e9, 1.3)

    def test_stale_play_while_armed_is_nacked_and_schedule_kept(self) -> None:
        self._start()
        self._load()
        self._wait_for_sync()
        self.assertTrue(self._play(lead_s=1.2)["ok"])
        self.assertEqual(self.agent.state, "armed")
        scheduled = self.agent._scheduled_at_master_ns
        ack = self._play(at_master_time=time.monotonic_ns() - 3 * NS)
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["error"], "missed_start")
        self.assertEqual(self.agent.state, "armed")
        self.assertEqual(self.agent._scheduled_at_master_ns, scheduled)
        self.assertTrue(self._wait_for_state("playing", timeout=3.0), "the original schedule must still fire")

    def test_stale_play_while_playing_leaves_performance_untouched(self) -> None:
        self._start()
        self._start_walking()
        ack = self._play(at_master_time=time.monotonic_ns() - 3 * NS)
        self.assertFalse(ack["ok"])
        self.assertEqual(self.agent.state, "playing")
        self.assertIsNone(self.agent.last_error)

    def test_telemetry_show_time_while_armed_is_scheduled_from_show_time(self) -> None:
        self._start()
        self._load()
        self._wait_for_sync()
        self.assertTrue(self._play(from_show_time=7.5, lead_s=2.0)["ok"])
        self.assertEqual(self.agent.state, "armed")
        self.assertEqual(self.agent.build_telemetry()["show_time"], 7.5)


# ---------------------------------------------------------------------------
# seek while PLAYING is a clock jump, not an ARMED detour
# ---------------------------------------------------------------------------


class SeekTest(_AgentTestBase):
    def test_seek_while_playing_keeps_emitting_until_the_jump(self) -> None:
        self._start()
        self._start_walking()
        target_master = time.monotonic_ns() + int(0.6 * NS)
        seek_id = self.master.send_cmd(self.agent_addr, "seek", show_time=10.0, at_master_time=target_master)
        acks = self.master.wait_for_ack(seek_id)
        self.assertTrue(acks and acks[0]["ok"], acks)
        self.assertEqual(self.agent.state, "playing", "seek while PLAYING must not park the duck in ARMED")
        n = len(self.robotd.by_method("robot.move"))
        time.sleep(0.3)
        self.assertGreater(len(self.robotd.by_method("robot.move")), n + 5, "intents keep flowing during the lead")
        self.assertLess(self.agent._current_show_time(), 6.0)  # still on the old timeline
        time.sleep(0.5)
        now_ns = time.monotonic_ns()
        self.assertAlmostEqual(self.agent._current_show_time(now_ns), 10.0 + (now_ns - target_master) / 1e9, delta=0.08)
        self.assertEqual(self.agent.state, "playing")

    def test_armed_state_datagram_does_not_feed_drift_correction(self) -> None:
        self._start()
        self._load()
        self._wait_for_sync()
        self.assertTrue(self._play()["ok"])
        self.assertTrue(self._wait_for_state("playing"))
        time.sleep(0.3)
        now = time.monotonic_ns()
        self.master.send_raw(
            self.agent_addr,
            {"v": 1, "type": "state", "seq": 5, "show": "demo", "transport": "armed", "show_time": 0.0, "master_time": now},
        )
        time.sleep(0.15)
        self.assertEqual(self.agent._show_time_correction_target_s, 0.0)
        now = time.monotonic_ns()
        ahead = self.agent._current_show_time(now) + 1.0
        self.master.send_raw(
            self.agent_addr,
            {"v": 1, "type": "state", "seq": 6, "show": "demo", "transport": "playing", "show_time": ahead, "master_time": now},
        )
        self.assertTrue(self._wait(lambda: abs(self.agent._show_time_correction_target_s - 1.0) < 0.1))


# ---------------------------------------------------------------------------
# load validation + telemetry fields
# ---------------------------------------------------------------------------


class LoadTest(_AgentTestBase):
    def test_load_without_sha256_is_nacked(self) -> None:
        self._start()
        for sha in (None, ""):
            fields = {"show": "demo", "role": "lead"}
            if sha is not None:
                fields["sha256"] = sha
            cmd_id = self.master.send_cmd(self.agent_addr, "load", **fields)
            acks = self.master.wait_for_ack(cmd_id)
            self.assertTrue(acks)
            self.assertFalse(acks[0]["ok"])
            self.assertIn("sha256", acks[0]["error"])
        self.assertEqual(self.agent.state, "idle")

    def test_load_nacks_show_without_duration(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _, sha = _write_show(
                Path(d), "nodur", duration=None, locomotion=[{"t": 0.0, "vx": 0.2}, {"t": 1.0, "vx": 0.2}]
            )
            self._start(shows_dir=Path(d))
            ack = self._load("nodur", sha)
            self.assertFalse(ack["ok"])
            self.assertIn("meta.duration", ack["error"])
            self.assertEqual(self.agent.state, "idle")

    def test_policy_failure_is_reported_in_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            policy = {"name": "walk", "mode": "walk", "file": "policies/walk.onnx", "sha256": "0" * 64, "slot": "walk"}
            _, bad_sha = _write_show(Path(d), "needs-policy", policies=[policy])
            _, good_sha = _write_show(Path(d), "plain")
            self._start(shows_dir=Path(d))
            ack = self._load("needs-policy", bad_sha)
            self.assertFalse(ack["ok"])
            self.assertIn("policy", ack["error"])
            telemetry = self.agent.build_telemetry()
            self.assertFalse(telemetry["policies_ok"])
            self.assertEqual(telemetry["last_error"], ack["error"])
            self.assertTrue(self._load("plain", good_sha)["ok"])
            telemetry = self.agent.build_telemetry()
            self.assertTrue(telemetry["policies_ok"])
            self.assertIsNone(telemetry["last_error"])


# ---------------------------------------------------------------------------
# panic latency + telemetry cadence
# ---------------------------------------------------------------------------


class PanicAndCadenceTest(_AgentTestBase):
    def test_panic_acks_promptly_while_robotd_hangs_on_an_event(self) -> None:
        self._start(robotd=ScriptedRobotd(hang={"robot.sound"}))
        self._start_walking()  # the t=4.0 chirp request now hangs for its full 1 s timeout
        self.assertTrue(self.robotd.wait_for_method("robot.sound", count=1, timeout=2.0))
        t0 = time.monotonic()
        panic_id = self.master.send_cmd(self.agent_addr, "panic")
        acks = self.master.wait_for_ack(panic_id, timeout=3.0)
        latency = time.monotonic() - t0
        self.assertTrue(acks and acks[0]["ok"], "panic must never be NACKed")
        self.assertLess(latency, 0.5, f"panic ACK took {latency * 1000:.0f} ms while an event RPC was in flight")
        self.assertEqual(self.agent.state, "idle")
        self.assertTrue(self._wait(lambda: len(self.robotd.by_method("robot.stop")) >= 1), "panic must send robot.stop")
        self.assertEqual(self._last_move(), {"vx": 0.0, "vy": 0.0, "vyaw": 0.0}, "panic zeroes locomotion")
        n = len(self.robotd.by_method("robot.move"))
        time.sleep(1.2)  # outlive the hung request's timeout: nothing may resume, no fault from idle
        self.assertEqual(len(self.robotd.by_method("robot.move")), n)
        self.assertEqual(self.agent.state, "idle")

    def test_telemetry_switches_to_5hz_promptly_when_playing(self) -> None:
        self._start()
        self._load()
        self._wait_for_sync()
        self.assertTrue(self._play(lead_s=0.3)["ok"])
        self.assertTrue(self._wait_for_state("playing"))
        t_playing = time.monotonic()
        time.sleep(1.0)
        playing_samples = [
            rx for rx, m, _ in self.master.messages()
            if m.get("type") == "telemetry" and m.get("state") == "playing" and t_playing <= rx <= t_playing + 1.0
        ]
        self.assertGreaterEqual(len(playing_samples), 3, "expected ~5 Hz telemetry within the first second of PLAYING")
        self.assertLess(playing_samples[0] - t_playing, 0.35, "first PLAYING telemetry must not wait out the 1 s idle period")


# ---------------------------------------------------------------------------
# robotd_client robustness
# ---------------------------------------------------------------------------


class _DropThenServe:
    """Accepts and RST-closes the first `drops` connections, then serves hello."""

    def __init__(self, drops: int, unix_path: Optional[str] = None) -> None:
        self.drops = drops
        self.accepted = 0
        if unix_path:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(unix_path)
            self.target = unix_path
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.bind(("127.0.0.1", 0))
            self.target = f"127.0.0.1:{self.sock.getsockname()[1]}"
        self.sock.listen(4)
        self.sock.settimeout(0.5)
        self._stop = threading.Event()
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepted += 1
            if self.accepted <= self.drops:
                _rst_close(conn)
                continue
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        f = conn.makefile("rb")
        try:
            while not self._stop.is_set():
                line = f.readline()
                if not line:
                    return
                msg = json.loads(line)
                if "id" in msg:
                    resp = {"jsonrpc": "2.0", "id": msg["id"], "result": {"api_version": 16, "daemon_version": "x", "revision": "y"}}
                    conn.sendall((json.dumps(resp) + "\n").encode())
        except (OSError, json.JSONDecodeError):
            return

    def stop(self) -> None:
        self._stop.set()
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass
        self.sock.close()


class RobotdClientRobustnessTest(unittest.TestCase):
    def _assert_reconnects_through_drops(self, server: _DropThenServe) -> None:
        client = RobotdClient(server.target, backoff_initial=0.05, backoff_max=0.2)
        client.start()
        self.addCleanup(client.stop)
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and not client.connected:
            time.sleep(0.02)
        self.assertTrue(client._connector_thread.is_alive(), "the reconnect thread died")
        self.assertTrue(client.connected, f"never connected; accepted={server.accepted}")
        self.assertGreater(server.accepted, server.drops)

    def test_connector_survives_accept_and_close_over_tcp(self) -> None:
        server = _DropThenServe(drops=3)
        self.addCleanup(server.stop)
        self._assert_reconnects_through_drops(server)

    def test_connector_survives_accept_and_close_over_unix_socket(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            server = _DropThenServe(drops=3, unix_path=str(Path(d) / "robotd.sock"))
            self.addCleanup(server.stop)
            self._assert_reconnects_through_drops(server)

    def test_close_socket_wakes_only_still_pending_waiters(self) -> None:
        client = RobotdClient("127.0.0.1:1")  # never started
        done, waiting = threading.Event(), threading.Event()
        client._pending = {7: done, 8: waiting}
        client._pending.pop(7)  # a request whose finally-block cleanup already ran
        client._close_socket()
        self.assertTrue(waiting.is_set())
        self.assertFalse(done.is_set())
        self.assertNotIn(7, client._replies, "must not resurrect a reply nobody will ever pop")
        self.assertEqual(client._replies[8]["error"]["message"], "disconnected")
        self.assertEqual(client._pending, {})

    def test_notify_bounds_its_block_when_robotd_stops_reading(self) -> None:
        """A robotd that accepts, answers hello, and then stops draining
        the socket (SIGSTOP/deadlock/starvation) but keeps the fd open
        must not be able to block notify() forever: on the agent, notify()
        runs on the 50 Hz tick thread while _playback_lock is held, so an
        unbounded block there would also block panic (finding A3).
        `_write_line` must give up after SEND_TIMEOUT_S and drop the link.
        """
        with tempfile.TemporaryDirectory() as d:
            sock_path = str(Path(d) / "robotd.sock")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock_path)
            srv.listen(1)
            self.addCleanup(srv.close)
            accepted = threading.Event()

            def serve_hello_then_stall() -> None:
                try:
                    conn, _addr = srv.accept()
                except OSError:
                    return
                accepted.set()
                f = conn.makefile("rb")
                line = f.readline()
                msg = json.loads(line)
                resp = {"jsonrpc": "2.0", "id": msg["id"], "result": {"api_version": 16, "daemon_version": "x", "revision": "y"}}
                conn.sendall((json.dumps(resp) + "\n").encode())
                time.sleep(5)  # never read again; the fd stays open (no EOF)

            threading.Thread(target=serve_hello_then_stall, daemon=True).start()

            client = RobotdClient(sock_path)
            client.start()
            self.addCleanup(client.stop)
            self.assertTrue(accepted.wait(timeout=2.0), "server never accepted")
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not client.connected:
                time.sleep(0.02)
            self.assertTrue(client.connected)

            # Flood notify() (as the tick thread would) until the send
            # buffer is full and the bounded wait gives up.
            t0 = time.monotonic()
            raised = False
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    client.notify("robot.move", {"vx": 0.1, "vy": 0.0, "vyaw": 0.0})
                except RobotdDisconnected:
                    raised = True
                    break
            dt = time.monotonic() - t0
            self.assertTrue(raised, "notify() never gave up on a robotd that stopped reading")
            self.assertLess(dt, 5.0, f"notify() took {dt:.2f}s to give up; not bounded")
            # _write_line() closes the socket synchronously with the raise, but
            # `connected` only flips once the reader thread notices the close
            # and the connector loop catches up -- poll for it.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and client.connected:
                time.sleep(0.02)
            self.assertFalse(client.connected, "the link must be dropped once the send times out")

    def test_api_version_mismatch_is_logged_as_warning(self) -> None:
        robotd = ScriptedRobotd(hello_api_version=15)
        self.addCleanup(robotd.stop)
        client = RobotdClient(robotd.target)
        with self.assertLogs("duck_agent.robotd_client", level="WARNING") as logs:
            client.start()
            self.addCleanup(client.stop)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not client.connected:
                time.sleep(0.02)
            self.assertTrue(client.connected, "mismatch is reported, not refused")
        self.assertTrue(any("api_version 15" in line for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
