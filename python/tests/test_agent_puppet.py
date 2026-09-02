"""Puppet channel tests -- docs/swarmlink-protocol.md #6, docs/authoring.md #1.

Drives a real DuckAgent through the FakeRobotd/FakeMaster fakes from
test_agent.py (and _AgentTestBase/ScriptedRobotd/_write_show from
test_agent_safety.py) with a small in-test puppet sender, plus pure tests
of duck_agent.puppet (parsing/clamping/channel bookkeeping) and of the
tools/puppet.py streamer against an injected clock.

Timing assertions poll with deadlines (never a fixed sleep followed by an
assertion) so they stay robust on a loaded CI machine; "nothing happens"
checks poll a predicate for a bounded window and assert it never fired.
"""

from __future__ import annotations

import io
import json
import math
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duck_agent.puppet import (  # noqa: E402
    PUPPET_FRESH_NS,
    SEQ_RESET_NS,
    PuppetChannel,
    PuppetPacket,
    PuppetPacketError,
    nudge_move,
    parse_puppet_packet,
)
from duckshow.limits import DEFAULT_LIMITS  # noqa: E402
from tests.test_agent import FakeMaster  # noqa: E402
from tests.test_agent_safety import ScriptedRobotd, _AgentTestBase, _write_show  # noqa: E402
from tools import puppet as puppet_tool  # noqa: E402

NS = 1_000_000_000
LIM = DEFAULT_LIMITS


def _approx(params: dict, **expected: float) -> bool:
    return all(abs(float(params.get(k, math.nan)) - v) < 1e-6 for k, v in expected.items())


def _is_zero_move(params: dict) -> bool:
    return _approx(params, vx=0.0, vy=0.0, vyaw=0.0)


class PuppetSender:
    """Streams one puppet payload at 50 Hz from the FakeMaster's socket with
    an increasing seq; `set()` swaps the payload live, `send_once()` sends
    a single packet with an explicit seq. Test-private."""

    def __init__(self, master: FakeMaster, agent_addr: tuple[str, int], seq0: int = 1, period_s: float = 0.02) -> None:
        self.master = master
        self.agent_addr = agent_addr
        self.seq = seq0
        self.period_s = period_s
        self._lock = threading.Lock()
        self._payload: Optional[dict[str, Any]] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.packets_sent = 0

    def start(self, **payload: Any) -> "PuppetSender":
        self.set(**payload)
        self._thread.start()
        return self

    def set(self, **payload: Any) -> None:
        with self._lock:
            self._payload = dict(payload)

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def send_once(self, seq: Optional[int] = None, **payload: Any) -> int:
        with self._lock:
            if seq is None:
                seq = self.seq
                self.seq += 1
            msg = {"v": 1, "type": "puppet", "seq": seq, "master_time": time.monotonic_ns(), **payload}
        self.master.send_raw(self.agent_addr, msg)
        return seq

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                payload = self._payload
                seq = self.seq
                self.seq += 1
            if payload is not None:
                msg = {"v": 1, "type": "puppet", "seq": seq, "master_time": time.monotonic_ns(), **payload}
                try:
                    self.master.send_raw(self.agent_addr, msg)
                except OSError:
                    return
                self.packets_sent += 1
            self._stop.wait(self.period_s)


class _PuppetAgentTest(_AgentTestBase):
    def _sender(self, seq0: int = 1) -> PuppetSender:
        sender = PuppetSender(self.master, self.agent_addr, seq0=seq0)
        self.addCleanup(sender.stop)
        return sender

    def _moves(self) -> list[tuple[float, dict]]:
        return [(rx, m["params"]) for rx, m in self.robotd.all_messages() if m.get("method") == "robot.move"]

    def _has_move(self, **expected: float) -> bool:
        return any(_approx(p, **expected) for _, p in self._moves())

    def _last_params(self, method: str) -> Optional[dict]:
        msgs = self.robotd.by_method(method)
        return msgs[-1]["params"] if msgs else None

    def _wait_quiet(self, method: str, quiet_s: float = 0.3, timeout: float = 3.0) -> int:
        """Wait until no new `method` message has arrived for `quiet_s`;
        returns the count at that point (same pattern as test_agent.py's
        panic test)."""
        deadline = time.monotonic() + timeout
        last = len(self.robotd.by_method(method))
        quiet_since = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.02)
            now = len(self.robotd.by_method(method))
            if now != last:
                last, quiet_since = now, time.monotonic()
            elif time.monotonic() - quiet_since >= quiet_s:
                break
        return last

    def _assert_stays_quiet(self, method: str, count: int, window_s: float = 0.3) -> None:
        self.assertFalse(
            self._wait(lambda: len(self.robotd.by_method(method)) != count, timeout=window_s),
            f"unexpected further {method} messages",
        )


# ---------------------------------------------------------------------------
# Puppet mode (IDLE/LOADED)
# ---------------------------------------------------------------------------


class PuppetModeTest(_PuppetAgentTest):
    def test_puppet_in_loaded_drives_move_at_tick_rate_then_zeroes_on_deadman(self) -> None:
        self._start()
        self.assertTrue(self._load()["ok"])
        sender = self._sender().start(move={"vx": 0.1, "vy": 0.02, "vyaw": 0.3})

        def puppet_moves() -> list[tuple[float, dict]]:
            return [(rx, p) for rx, p in self._moves() if _approx(p, vx=0.1, vy=0.02, vyaw=0.3)]

        # Forwarded on the 50 Hz tick, not once per packet: many moves, closely spaced.
        self.assertTrue(self._wait(lambda: len(puppet_moves()) >= 10, timeout=1.5), "puppet move not forwarded at tick rate")
        times = [rx for rx, _ in puppet_moves()]
        gaps = sorted(b - a for a, b in zip(times, times[1:]))
        self.assertLess(gaps[len(gaps) // 2], 0.1, f"median gap between forwarded moves too large: {gaps}")
        self.assertEqual(self.agent.state, "loaded")

        sender.stop()
        t_stop = time.monotonic()
        # Deadman: the last value keeps being forwarded for up to 250 ms, then exactly
        # one zero move lands, then silence.
        self.assertTrue(
            self._wait(lambda: any(rx > t_stop and _is_zero_move(p) for rx, p in self._moves()), timeout=1.5),
            "locomotion was not zeroed after the stream went stale",
        )
        count = self._wait_quiet("robot.move")
        moves = self._moves()
        self.assertTrue(_is_zero_move(moves[-1][1]), moves[-1])
        last_puppet_rx = puppet_moves()[-1][0]
        after = [p for rx, p in moves if rx > last_puppet_rx]
        self.assertEqual(len(after), 1, f"expected exactly one zero move after staleness, got {after}")
        zero_rx = next(rx for rx, p in moves if rx > last_puppet_rx)
        self.assertGreaterEqual(zero_rx - t_stop, 0.15, "zeroed before the 250 ms deadman could have elapsed")
        self._assert_stays_quiet("robot.move", count)
        # The stream never sent robot.stop: staleness zeroes locomotion, nothing more.
        self.assertEqual(self.robotd.by_method("robot.stop"), [])

    def test_head_pose_mouth_forwarded_in_puppet_mode(self) -> None:
        self._start()
        self.assertTrue(self._wait(lambda: self.agent.robotd.connected))
        sender = self._sender().start(
            head={"neck_pitch": 0.1, "head_pitch": -0.2, "head_yaw": 0.3, "head_roll": 0.0},
            pose={"z": -0.02, "roll": 0.0, "pitch": 0.1, "active": True},
            mouth={"open": 0.6},
        )
        self.assertTrue(self._wait(lambda: _approx(self._last_params("robot.head") or {}, head_pitch=-0.2, head_yaw=0.3)))
        self.assertTrue(self._wait(lambda: _approx(self._last_params("robot.pose") or {}, z=-0.02, pitch=0.1)))
        self.assertTrue(self._last_params("robot.pose")["active"])
        self.assertTrue(self._wait(lambda: _approx(self._last_params("robot.mouth") or {}, open=0.6)))
        # IDLE is a puppet-mode state too; no locomotion was asserted so no robot.move at all.
        self.assertEqual(self.agent.state, "idle")
        self.assertEqual(self._moves(), [])
        sender.stop()
        # Head/pose/mouth stop being forwarded when stale (robotd holds them; no zeroing).
        count = self._wait_quiet("robot.head")
        self._assert_stays_quiet("robot.head", count)
        self.assertEqual(self._moves(), [])

    def test_stale_seq_is_dropped(self) -> None:
        self._start()
        self.assertTrue(self._load()["ok"])
        sender = self._sender()
        sender.send_once(seq=100, move={"vx": 0.1, "vy": 0.0, "vyaw": 0.0})
        self.assertTrue(self._wait(lambda: self._has_move(vx=0.1)))
        sender.send_once(seq=50, move={"vx": 0.2, "vy": 0.0, "vyaw": 0.0})  # reordered / stale
        sender.send_once(seq=100, move={"vx": 0.2, "vy": 0.0, "vyaw": 0.0})  # duplicate
        self.assertFalse(self._wait(lambda: self._has_move(vx=0.2), timeout=0.3), "stale seq was applied")
        sender.send_once(seq=101, move={"vx": 0.15, "vy": 0.0, "vyaw": 0.0})
        self.assertTrue(self._wait(lambda: self._has_move(vx=0.15)))
        self.assertEqual(self.agent._puppet.last_seq, 101)

    def test_out_of_range_values_are_clamped(self) -> None:
        self._start()
        self.assertTrue(self._load()["ok"])
        self._sender().send_once(
            seq=1,
            move={"vx": 5.0, "vy": -5.0, "vyaw": 100.0},
            head={"neck_pitch": 9.0, "head_pitch": -9.0, "head_yaw": 0.0, "head_roll": 0.0},
            pose={"z": 1.0, "roll": -9.0, "pitch": 9.0, "active": True},
            mouth={"open": 3.0},
        )
        self.assertTrue(self._wait(lambda: self._has_move(vx=LIM.max_abs_vx, vy=-LIM.max_abs_vy, vyaw=LIM.max_abs_vyaw)))
        self.assertTrue(
            self._wait(lambda: _approx(self._last_params("robot.head") or {}, neck_pitch=LIM.max_abs_head_angle, head_pitch=-LIM.max_abs_head_angle))
        )
        self.assertTrue(
            self._wait(lambda: _approx(self._last_params("robot.pose") or {}, z=LIM.max_abs_pose_z, roll=-LIM.max_abs_pose_roll, pitch=LIM.max_abs_pose_pitch))
        )
        self.assertTrue(self._wait(lambda: _approx(self._last_params("robot.mouth") or {}, open=LIM.max_mouth_open)))

    def test_malformed_packets_are_dropped(self) -> None:
        self._start()
        self.assertTrue(self._load()["ok"])
        sender = self._sender()
        bad = [
            {"v": 1, "type": "puppet", "move": {"vx": 0.1}},  # no seq
            {"v": 1, "type": "puppet", "seq": "7", "move": {"vx": 0.1}},
            {"v": 1, "type": "puppet", "seq": True, "move": {"vx": 0.1}},
            {"v": 1, "type": "puppet", "seq": 7, "move": [0.1, 0.0, 0.0]},
            {"v": 1, "type": "puppet", "seq": 8, "move": {"vx": "fast"}},
            {"v": 1, "type": "puppet", "seq": 9, "move": {"vx": float("nan")}},
            {"v": 1, "type": "puppet", "seq": 10, "move": {"vx": 0.1}, "do": "moonwalk"},
            {"v": 1, "type": "puppet", "seq": 11, "move": {"vx": 0.1}, "sound": "quack"},
            {"v": 1, "type": "puppet", "seq": 12, "pose": {"z": 0.0, "active": "yes"}},
            {"v": 1, "type": "puppet", "seq": 13, "head": 0.5},
            {"v": 1, "type": "puppet", "seq": 14, "do": 42},
        ]
        for msg in bad:
            self.master.send_raw(self.agent_addr, msg)
        self.master.sock.sendto(b'{"type": "puppet", "seq": 15, ', self.agent_addr)  # not JSON
        self.master.sock.sendto(b'[{"type": "puppet", "seq": 16}]', self.agent_addr)  # not an object

        def anything_forwarded() -> bool:
            return any(self.robotd.by_method(m) for m in ("robot.move", "robot.head", "robot.pose", "robot.mouth", "robot.do", "robot.sound"))

        self.assertFalse(self._wait(anything_forwarded, timeout=0.4), "a malformed packet was applied")
        self.assertFalse(self.agent.build_telemetry()["puppet"])
        self.assertIsNone(self.agent._puppet.last_seq)
        # ...and the channel still works afterwards.
        sender.send_once(seq=3, move={"vx": 0.1, "vy": 0.0, "vyaw": 0.0})
        self.assertTrue(self._wait(lambda: self._has_move(vx=0.1)))
        self.assertEqual(self.agent.state, "loaded")

    def test_oversized_integer_field_is_dropped_and_recv_loop_survives(self) -> None:
        """A JSON integer literal beyond float range must be rejected as a
        malformed packet, not crash the datagram reader: `float()` raises
        OverflowError on such a value, which is not a ValueError/
        PuppetPacketError, so an unguarded conversion would kill the
        `-recv` thread and leave the duck deaf to every later cmd,
        panic included (finding A1)."""
        self._start()
        self.assertTrue(self._load()["ok"])
        huge = int("9" * 400)  # far beyond float's ~1.8e308 range
        self.master.sock.sendto(
            json.dumps({"v": 1, "type": "puppet", "seq": 1, "move": {"vx": huge}}).encode("utf-8"),
            self.agent_addr,
        )
        # The packet must be dropped, not applied.
        self.assertFalse(self._wait(lambda: self._moves() != [], timeout=0.3), "an out-of-range int was applied")
        recv_thread = next(t for t in self.agent._threads if t.name.endswith("-recv"))
        self.assertTrue(recv_thread.is_alive(), "the recv thread died on a malformed puppet packet")
        # ...and the agent still reads the socket: panic still ACKs.
        panic_id = self.master.send_cmd(self.agent_addr, "panic")
        acks = self.master.wait_for_ack(panic_id, timeout=1.5)
        self.assertTrue(acks and acks[0]["ok"], "panic must still ACK after an oversized-int puppet packet")

    def test_do_and_sound_fire_once_per_seq(self) -> None:
        self._start()
        self.assertTrue(self._load()["ok"])
        sender = self._sender()
        sender.send_once(seq=10, move={"vx": 0.0, "vy": 0.0, "vyaw": 0.0}, do="kick_left", sound="chirp")
        # The master-side retransmit/reorder cases: same seq again, twice.
        sender.send_once(seq=10, do="kick_left", sound="chirp")
        sender.send_once(seq=10, do="kick_left", sound="chirp")
        sender.send_once(seq=11, move={"vx": 0.0, "vy": 0.0, "vyaw": 0.0})  # carries no action
        self.assertTrue(self._wait(lambda: self.robotd.by_method("robot.do") and self.robotd.by_method("robot.sound")))
        self.assertFalse(
            self._wait(lambda: len(self.robotd.by_method("robot.do")) > 1 or len(self.robotd.by_method("robot.sound")) > 1, timeout=0.4),
            "an action fired more than once for one seq",
        )
        do = self.robotd.by_method("robot.do")[0]
        sound = self.robotd.by_method("robot.sound")[0]
        self.assertEqual(do["params"], {"skill": "kick_left"})
        self.assertEqual(sound["params"], {"tag": "chirp"})
        self.assertIn("id", do)  # discrete: a request, same path as timeline events
        self.assertIn("id", sound)
        # A robotd refusal (BUSY) of an operator input is logged, never a fault.
        self.assertEqual(self.agent.state, "loaded")

    def test_stop_cmd_stops_puppet_driven_motion_and_wins_over_the_stream(self) -> None:
        self._start()
        self.assertTrue(self._load()["ok"])
        sender = self._sender().start(move={"vx": 0.1, "vy": 0.0, "vyaw": 0.0})
        self.assertTrue(self._wait(lambda: self._has_move(vx=0.1)))
        stop_id = self.master.send_cmd(self.agent_addr, "stop")
        acks = self.master.wait_for_ack(stop_id)
        self.assertTrue(acks and acks[0]["ok"], acks)
        t_ack = time.monotonic()
        self.assertEqual(self.agent.state, "loaded")
        self.assertTrue(self._wait(lambda: self.robotd.by_method("robot.stop")), "stop must send robot.stop for puppet-driven motion")
        count = self._wait_quiet("robot.move")
        moves = self._moves()
        self.assertTrue(_is_zero_move(moves[-1][1]), moves[-1])
        # The sender is still streaming: stop wins until the stream goes quiet.
        self.assertGreater(sender.packets_sent, 0)
        self._assert_stays_quiet("robot.move", count)
        self.assertFalse(any(rx > t_ack + 0.1 and not _is_zero_move(p) for rx, p in self._moves()))

    def test_agent_shutdown_while_puppet_driving_zeroes_and_stops(self) -> None:
        """`agent.stop()` (SIGINT/SIGTERM) must not leave robotd holding a
        puppet-driven velocity: nothing would be left running the deadman
        or accepting panic once the process exits (finding A4)."""
        self._start()
        self.assertTrue(self._load()["ok"])
        sender = self._sender().start(move={"vx": 0.25, "vy": 0.0, "vyaw": 0.0})
        self.assertTrue(self._wait(lambda: self._has_move(vx=0.25)), "puppet move never forwarded")

        self.agent.stop()  # exactly what __main__'s SIGINT/SIGTERM handler calls
        sender.stop()

        self.assertTrue(_is_zero_move(self._moves()[-1][1]), "shutdown left robotd holding a nonzero velocity")
        self.assertTrue(self.robotd.by_method("robot.stop"), "shutdown did not send robot.stop for puppet-driven motion")


# ---------------------------------------------------------------------------
# Nudge layer (PLAYING)
# ---------------------------------------------------------------------------


class NudgeLayerTest(_PuppetAgentTest):
    def test_nudge_adds_to_timeline_locomotion_and_clamps_at_limit(self) -> None:
        self._start()
        self._start_walking()  # demo lead from t=3.6: timeline vx=0.1 until t=5.5
        sender = self._sender().start(move={"vx": 0.1, "vy": 0.05, "vyaw": -0.2})
        self.assertTrue(self._wait(lambda: self._has_move(vx=0.2, vy=0.05, vyaw=-0.2), timeout=0.6), "vector sum not applied")
        sender.set(move={"vx": 0.2, "vy": 0.0, "vyaw": 0.0})  # 0.1 + 0.2 > max_abs_vx
        self.assertTrue(self._wait(lambda: self._has_move(vx=LIM.max_abs_vx, vy=0.0, vyaw=0.0), timeout=0.6), "sum not clamped")
        self.assertLess(self.agent._current_show_time(), 5.5, "test overran the walk segment; timing assumptions broken")
        self.assertEqual(self.agent.state, "playing")
        self.assertTrue(self.agent.build_telemetry()["puppet"])
        sender.stop()
        # Stale: the timeline resumes ownership (vx=0.1 again), still inside the walk segment.
        self.assertTrue(self._wait(lambda: _approx(self._moves()[-1][1], vx=0.1, vy=0.0, vyaw=0.0), timeout=1.0), "timeline did not resume")
        self.assertLess(self.agent._current_show_time(), 5.5)
        self.assertEqual(self.agent.state, "playing")
        self.assertTrue(self._wait(lambda: not self.agent.build_telemetry()["puppet"], timeout=1.0))

    def test_nudge_without_locomotion_track_starts_from_standing_and_zeroes_when_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shows = Path(tmp)
            _path, sha = _write_show(shows, "still", duration=10.0)  # no locomotion track at all
            self._start(shows_dir=shows)
            self.assertTrue(self._load(show="still", sha256=sha)["ok"])
            self._wait_for_sync()
            self.assertTrue(self._play(show="still")["ok"])
            self.assertTrue(self._wait_for_state("playing"))
            self.assertEqual(self._moves(), [])  # the timeline emits no locomotion
            sender = self._sender().start(move={"vx": 0.1, "vy": 0.0, "vyaw": 0.0})
            self.assertTrue(self._wait(lambda: len([1 for _, p in self._moves() if _approx(p, vx=0.1)]) >= 3))
            sender.stop()
            self.assertTrue(self._wait(lambda: _is_zero_move(self._moves()[-1][1]), timeout=1.5), "puppet velocity would be held forever")
            count = self._wait_quiet("robot.move")
            self._assert_stays_quiet("robot.move", count)
            self.assertEqual(self.agent.state, "playing")

    def test_head_pose_mouth_override_during_play_then_timeline_resumes(self) -> None:
        self._start()
        self.assertTrue(self._load()["ok"])
        self._wait_for_sync()
        self.assertTrue(self._play(from_show_time=0.0)["ok"])  # demo lead: head track 0-8 s, |head_yaw| <= 0.6
        self.assertTrue(self._wait_for_state("playing"))
        self.assertTrue(self._wait(lambda: self.robotd.by_method("robot.head")))
        sender = self._sender().start(
            head={"neck_pitch": 0.0, "head_pitch": 0.0, "head_yaw": 0.9, "head_roll": 0.1},
            pose={"z": -0.03, "roll": 0.0, "pitch": 0.0, "active": True},
            mouth={"open": 0.7},
        )
        self.assertTrue(self._wait(lambda: _approx(self._last_params("robot.head") or {}, head_yaw=0.9, head_roll=0.1)), "head not overridden")
        self.assertTrue(self._wait(lambda: _approx(self._last_params("robot.pose") or {}, z=-0.03)), "pose not overridden")
        self.assertTrue(self._wait(lambda: _approx(self._last_params("robot.mouth") or {}, open=0.7)), "mouth not overridden")
        self.assertEqual(self.agent.state, "playing")
        sender.stop()
        t_stop = time.monotonic()
        # Timeline head resumes (its envelope never reaches 0.9); pose/mouth simply stop
        # (the demo lead has no pose track and no mouth keyframes this early).
        self.assertTrue(
            self._wait(lambda: abs((self._last_params("robot.head") or {"head_yaw": 9.0})["head_yaw"]) < 0.7, timeout=1.5),
            "timeline head did not resume",
        )
        self.assertGreaterEqual(time.monotonic() - t_stop, 0.15, "override released before the deadman")
        pose_count = self._wait_quiet("robot.pose")
        self._assert_stays_quiet("robot.pose", pose_count)
        self.assertFalse(any(m["params"]["head_yaw"] > 0.7 for m in self.robotd.by_method("robot.head")[-3:]))
        self.assertEqual(self.agent.state, "playing")

    def test_do_fires_once_while_playing(self) -> None:
        self._start()
        self.assertTrue(self._load()["ok"])
        self._wait_for_sync()
        self.assertTrue(self._play(from_show_time=0.0)["ok"])  # first timeline event is at t=4.0
        self.assertTrue(self._wait_for_state("playing"))
        sender = self._sender()
        sender.send_once(seq=1, do="kick_right")
        sender.send_once(seq=1, do="kick_right")
        self.assertTrue(self._wait(lambda: self.robotd.by_method("robot.do")))
        self.assertFalse(self._wait(lambda: len(self.robotd.by_method("robot.do")) > 1, timeout=0.4))
        self.assertEqual(self.robotd.by_method("robot.do")[0]["params"], {"skill": "kick_right"})
        self.assertEqual(self.agent.state, "playing")

    def test_play_from_puppet_driven_loaded_zeroes_locomotion_while_armed(self) -> None:
        self._start()
        self.assertTrue(self._load()["ok"])
        self._wait_for_sync()
        sender = self._sender().start(move={"vx": 0.1, "vy": 0.0, "vyaw": 0.0})
        self.assertTrue(self._wait(lambda: self._has_move(vx=0.1)))
        self.assertTrue(self._play(from_show_time=3.6, lead_s=0.5)["ok"])  # ARMED for ~0.5 s, then walk segment
        self.assertTrue(self._wait_for_state("playing", timeout=3.0))
        self.assertTrue(self._wait(lambda: self._has_move(vx=0.2), timeout=1.0), "nudge not applied once playing")
        sender.stop()
        # Order of the move stream (timestamps from different reader threads are not
        # comparable at tick granularity, the sequence is): bare puppet 0.1s while
        # LOADED, then exactly one zero while ARMED, then the nudged 0.2s while PLAYING.
        moves = [p for _, p in self._moves()]
        first_nudged = next(i for i, p in enumerate(moves) if _approx(p, vx=0.2))
        before = moves[:first_nudged]
        last_bare = max(i for i, p in enumerate(before) if _approx(p, vx=0.1))
        self.assertEqual(
            [_is_zero_move(p) for p in before[last_bare + 1 :]],
            [True],
            f"expected exactly one zero move between LOADED puppeting and PLAYING, got {before[last_bare + 1:]}",
        )
        self.assertFalse(any(_approx(p, vx=0.1) for p in moves[first_nudged:]), "bare puppet velocity leaked into PLAYING")


# ---------------------------------------------------------------------------
# Precedence: panic, fault, telemetry
# ---------------------------------------------------------------------------


class PuppetPrecedenceTest(_PuppetAgentTest):
    def test_panic_during_puppet_zeroes_and_stops_and_stream_stays_muted_until_quiet(self) -> None:
        self._start()
        self.assertTrue(self._load()["ok"])
        sender = self._sender().start(move={"vx": 0.1, "vy": 0.0, "vyaw": 0.0})
        self.assertTrue(self._wait(lambda: len([1 for _, p in self._moves() if _approx(p, vx=0.1)]) >= 3))

        panic_id = self.master.send_cmd(self.agent_addr, "panic")
        acks = self.master.wait_for_ack(panic_id)
        self.assertTrue(acks and acks[0]["ok"], "panic must never be NACKed")
        t_ack = time.monotonic()
        self.assertTrue(self._wait_for_state("idle"))
        self.assertTrue(self._wait(lambda: self.robotd.by_method("robot.stop")), "panic must send robot.stop")

        # The sender keeps streaming through the panic: it must not re-drive the duck.
        count = self._wait_quiet("robot.move")
        moves = self._moves()
        self.assertTrue(_is_zero_move(moves[-1][1]), f"panic must end with a zero move, got {moves[-1]}")
        self.assertFalse(any(rx > t_ack + 0.1 and not _is_zero_move(p) for rx, p in moves), "puppet re-drove the duck after panic")
        self._assert_stays_quiet("robot.move", count)
        self.assertGreater(sender.packets_sent, 0)
        self.assertFalse(self.agent.build_telemetry()["puppet"], "a muted stream must not report puppet=true")

        # Once the stream has been quiet for a deadman period, a new stream is honoured again (IDLE is puppet-eligible).
        sender.stop()
        n_before = len(self._moves())
        self.assertFalse(self._wait(lambda: len(self._moves()) != n_before, timeout=0.35))
        fresh = self._sender(seq0=100_000).start(move={"vx": 0.1, "vy": 0.0, "vyaw": 0.0})
        self.assertTrue(self._wait(lambda: any(_approx(p, vx=0.1) for _, p in self._moves()[n_before:]), timeout=1.5), "channel did not unmute after quiet")
        self.assertTrue(self.agent.build_telemetry()["puppet"])
        fresh.stop()

    def test_panic_during_slow_puppet_action_drops_a_still_queued_one(self) -> None:
        """Two puppet actions land in the same tick's drain (a `do` and a
        `sound` from one packet). The `do`'s robot.do reply is slow; while
        it is in flight, panic fires. The still-queued `sound` must never
        reach robotd afterwards -- a state check alone cannot tell this
        apart from a legitimate post-panic puppet action, because panic's
        landing state (idle) is puppet-eligible; only the channel epoch
        (bumped by `mute()`) can (finding A2)."""
        self._start(robotd=ScriptedRobotd(delay={"robot.do": 0.5}))
        self.assertTrue(self._load()["ok"])
        self._sender().send_once(seq=1, do="kick_left", sound="chirp")
        self.assertTrue(self._wait(lambda: self.robotd.by_method("robot.do")), "robot.do never sent")

        # The do request is now blocked on its delayed reply; panic while in flight.
        panic_id = self.master.send_cmd(self.agent_addr, "panic")
        acks = self.master.wait_for_ack(panic_id)
        self.assertTrue(acks and acks[0]["ok"], "panic must never be NACKed")
        self.assertTrue(self._wait_for_state("idle"))
        self.assertTrue(self._wait(lambda: self.robotd.by_method("robot.stop")), "panic must send robot.stop")

        # Give the delayed robot.do reply time to land and the queued sound its chance to fire.
        self.assertFalse(
            self._wait(lambda: self.robotd.by_method("robot.sound"), timeout=1.0),
            "a puppet action queued before panic fired after panic's robot.stop",
        )

    def test_fault_ignores_puppet(self) -> None:
        self._start(robotd=ScriptedRobotd(fail={"robot.sound": (1, "busy")}))
        self.assertTrue(self._load()["ok"])
        self._wait_for_sync()
        self.assertTrue(self._play(from_show_time=3.9)["ok"])  # chirp at t=4.0 fails -> FAULT
        self.assertTrue(self._wait_for_state("fault", timeout=3.0))
        t_fault = time.monotonic()
        sender = self._sender().start(move={"vx": 0.1, "vy": 0.0, "vyaw": 0.0}, head={"head_yaw": 0.5})
        self.assertTrue(self._wait(lambda: self.agent.build_telemetry()["puppet"]))  # packets are accepted...
        self.assertFalse(
            self._wait(lambda: any(rx > t_fault + 0.05 and not _is_zero_move(p) for rx, p in self._moves()), timeout=0.4),
            "FAULT forwarded a puppet move",
        )
        self.assertFalse(any(_approx(m["params"], head_yaw=0.5) for m in self.robotd.by_method("robot.head")))
        self.assertEqual(self.agent.state, "fault")
        sender.stop()

    def test_telemetry_puppet_flag_toggles(self) -> None:
        self._start()
        self.assertTrue(self._load()["ok"])  # learns the master address -> telemetry flows
        self.assertFalse(self.agent.build_telemetry()["puppet"])
        self._sender().send_once(seq=1, move={"vx": 0.0, "vy": 0.0, "vyaw": 0.0})
        self.assertTrue(self._wait(lambda: self.agent.build_telemetry()["puppet"], timeout=1.0))
        t_true = time.monotonic()
        self.assertTrue(self._wait(lambda: not self.agent.build_telemetry()["puppet"], timeout=1.0))
        self.assertLess(time.monotonic() - t_true, 0.6)
        # And it is on the wire as a JSON boolean.
        wire = self.master.wait_for(lambda m: m.get("type") == "telemetry" and "puppet" in m, timeout=3.0)
        self.assertTrue(wire)
        self.assertIsInstance(wire[0]["puppet"], bool)


# ---------------------------------------------------------------------------
# duck_agent.puppet in isolation
# ---------------------------------------------------------------------------


class PuppetParsingTest(unittest.TestCase):
    def test_partial_channels_default_and_clamp(self) -> None:
        p = parse_puppet_packet({"seq": 5, "move": {"vx": 1.0}, "mouth": {"open": -1}, "pose": {"z": -0.1}})
        self.assertEqual(p.seq, 5)
        self.assertEqual(p.move, {"vx": LIM.max_abs_vx, "vy": 0.0, "vyaw": 0.0})
        self.assertEqual(p.mouth, {"open": 0.0})
        self.assertEqual(p.pose, {"z": -LIM.max_abs_pose_z, "roll": 0.0, "pitch": 0.0, "active": False})
        self.assertIsNone(p.head)
        self.assertIsNone(p.do)
        empty = parse_puppet_packet({"seq": 6})
        self.assertFalse(empty.carries_anything())

    def test_malformed_raises(self) -> None:
        for msg in (
            {},
            {"seq": 1.5},
            {"seq": True},
            {"seq": 1, "move": 3},
            {"seq": 1, "move": {"vx": "0.1"}},
            {"seq": 1, "move": {"vx": True}},
            {"seq": 1, "head": {"head_yaw": float("inf")}},
            {"seq": 1, "pose": {"active": 1}},
            {"seq": 1, "do": "moonwalk"},
            {"seq": 1, "sound": "quack"},
            {"seq": 1, "do": ["kick_left"]},
            ["seq", 1],
            {"seq": 1, "move": {"vx": int("9" * 400)}},  # beyond float range: OverflowError, not ValueError
        ):
            with self.assertRaises(PuppetPacketError, msg=repr(msg)):
                parse_puppet_packet(msg)  # type: ignore[arg-type]

    def test_nudge_is_vector_sum_clamped(self) -> None:
        self.assertEqual(
            nudge_move({"vx": 0.1, "vy": -0.15, "vyaw": 1.0}, {"vx": -0.05, "vy": -0.1, "vyaw": 1.0}),
            {"vx": 0.05, "vy": -LIM.max_abs_vy, "vyaw": LIM.max_abs_vyaw},
        )


class PuppetChannelTest(unittest.TestCase):
    def test_per_channel_freshness_and_seq_rules(self) -> None:
        ch = PuppetChannel()
        t0 = 1_000 * NS
        self.assertTrue(ch.offer(PuppetPacket(seq=1, move={"vx": 0.1, "vy": 0.0, "vyaw": 0.0}, head={"head_yaw": 0.5}), t0))
        self.assertTrue(ch.offer(PuppetPacket(seq=2, move={"vx": 0.2, "vy": 0.0, "vyaw": 0.0}), t0 + 100_000_000))
        self.assertFalse(ch.offer(PuppetPacket(seq=2, move={"vx": 0.9, "vy": 0.0, "vyaw": 0.0}), t0 + 110_000_000))
        self.assertFalse(ch.offer(PuppetPacket(seq=1), t0 + 120_000_000))
        v = ch.values(t0 + 200_000_000)
        self.assertEqual(v.move["vx"], 0.2)
        self.assertEqual(v.head, {"head_yaw": 0.5})  # asserted by seq 1, still within its 250 ms
        v = ch.values(t0 + 300_000_000)
        self.assertEqual(v.move["vx"], 0.2)  # seq 2 still fresh
        self.assertIsNone(v.head)  # seq 1's head expired on its own clock
        self.assertTrue(ch.is_fresh(t0 + 300_000_000))
        v = ch.values(t0 + 100_000_000 + PUPPET_FRESH_NS)
        self.assertIsNone(v.move)
        self.assertFalse(ch.is_fresh(t0 + 100_000_000 + PUPPET_FRESH_NS))
        # Seq tracking resets after a long silence (measured from the last packet
        # *offered*, accepted or not), so a restarted sender is accepted.
        self.assertFalse(ch.offer(PuppetPacket(seq=1), t0 + 120_000_000 + SEQ_RESET_NS - 1))
        t_restart = t0 + 120_000_000 + SEQ_RESET_NS - 1 + SEQ_RESET_NS
        self.assertTrue(ch.offer(PuppetPacket(seq=1, mouth={"open": 1.0}), t_restart))
        self.assertEqual(ch.last_seq, 1)

    def test_actions_once_and_mute_latch(self) -> None:
        ch = PuppetChannel()
        t0 = 5 * NS
        ch.offer(PuppetPacket(seq=1, do="kick_left", sound="chirp"), t0)
        ch.offer(PuppetPacket(seq=2, do="kick_right"), t0 + 20_000_000)
        self.assertEqual(ch.take_actions(), [("do", "kick_left"), ("sound", "chirp"), ("do", "kick_right")])
        self.assertEqual(ch.take_actions(), [])
        ch.offer(PuppetPacket(seq=3, move={"vx": 0.1, "vy": 0.0, "vyaw": 0.0}, do="roulade"), t0 + 40_000_000)
        ch.mute()
        self.assertEqual(ch.take_actions(), [])  # queued action dropped by panic/stop
        self.assertIsNone(ch.values(t0 + 50_000_000).move)
        self.assertFalse(ch.is_fresh(t0 + 50_000_000))
        # Streaming on through the mute: dropped while the gaps stay under a deadman period.
        t = t0 + 60_000_000
        for seq in range(4, 20):
            self.assertFalse(ch.offer(PuppetPacket(seq=seq, move={"vx": 0.1, "vy": 0.0, "vyaw": 0.0}), t))
            t += 20_000_000
        # A quiet gap of one deadman period unmutes.
        t += PUPPET_FRESH_NS
        self.assertTrue(ch.offer(PuppetPacket(seq=20, move={"vx": 0.1, "vy": 0.0, "vyaw": 0.0}), t))
        self.assertEqual(ch.values(t).move["vx"], 0.1)
        self.assertTrue(ch.is_fresh(t))


# ---------------------------------------------------------------------------
# tools/puppet.py
# ---------------------------------------------------------------------------


class _FakeTime:
    """Deterministic clock for PuppetStreamer: sleep() just advances it."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


class PuppetToolStreamerTest(unittest.TestCase):
    def _run(self, frames: list[dict], **kw: Any) -> tuple[list[dict], _FakeTime]:
        ft = _FakeTime()
        sent: list[dict] = []
        streamer = puppet_tool.PuppetStreamer(
            puppet_tool.validate_frames(frames), sent.append, clock=ft.clock, sleep=ft.sleep, seq0=1000, master_time=lambda: 7, **kw
        )
        n = streamer.run()
        self.assertEqual(n, len(sent))
        return sent, ft

    def test_frames_are_held_until_next_t_and_actions_fire_once(self) -> None:
        frames = [
            {"t": 0.0, "move": {"vx": 0.1}, "do": "kick_left"},
            {"t": 0.05, "head": {"head_yaw": 0.5}, "sound": "chirp"},
            {"t": 0.09, "move": {"vx": 0.0}},
        ]
        sent, ft = self._run(frames, hz=50)
        # ticks at 0, .02, .04 (frame 0), .06, .08 (frame 1), .10 (frame 2 = last, once)
        self.assertEqual(len(sent), 6)
        self.assertEqual([p["seq"] for p in sent], list(range(1000, 1006)))
        for p in sent:
            self.assertEqual((p["v"], p["type"], p["master_time"]), (1, "puppet", 7))
        self.assertEqual([p.get("move") for p in sent[:3]], [{"vx": 0.1}] * 3)
        self.assertEqual([("do" in p, "sound" in p) for p in sent], [(True, False), (False, False), (False, False), (False, True), (False, False), (False, False)])
        self.assertEqual(sent[0]["do"], "kick_left")
        self.assertEqual(sent[3]["sound"], "chirp")
        self.assertNotIn("move", sent[3])  # a frame is a complete assertion: frame 1 carries no move
        self.assertEqual(sent[3]["head"], {"head_yaw": 0.5})
        self.assertEqual(sent[5]["move"], {"vx": 0.0})
        self.assertNotIn("head", sent[5])
        # Exits TAIL_S after the last packet (deadman + slack), without sending more.
        self.assertAlmostEqual(ft.sleeps[-1], puppet_tool.TAIL_S)
        self.assertAlmostEqual(ft.now - 100.0, 0.10 + puppet_tool.TAIL_S, places=6)

    def test_hold_seconds_repeats_last_frame(self) -> None:
        sent, _ = self._run([{"t": 0.0, "move": {"vx": 0.1}}], hz=50, hold_seconds=0.09)
        self.assertEqual(len(sent), 6)  # 0, .02, .04, .06, .08, .10
        self.assertTrue(all(p["move"] == {"vx": 0.1} for p in sent))
        sent_once, _ = self._run([{"t": 0.0, "move": {"vx": 0.1}}], hz=50)
        self.assertEqual(len(sent_once), 1)

    def test_nothing_sent_before_first_frame_and_actions_queue_at_low_hz(self) -> None:
        sent, _ = self._run([{"t": 0.05, "mouth": {"open": 1.0}}], hz=50)
        self.assertEqual(len(sent), 1)
        # At 10 Hz frames 1 and 2 become due in the same tick: one `do` and one `sound`
        # per packet, so their actions go out in consecutive packets, in script order,
        # and the stream does not end until every queued action has been sent.
        frames = [
            {"t": 0.0, "do": "kick_left", "sound": "chirp"},
            {"t": 0.01, "do": "kick_right", "sound": "coo"},
            {"t": 0.02, "do": "roulade"},
        ]
        sent, _ = self._run(frames, hz=10)
        self.assertEqual([p.get("do") for p in sent], ["kick_left", "kick_right", "roulade"])
        self.assertEqual([p.get("sound") for p in sent], ["chirp", "coo", None])

    def test_validate_frames_rejects_bad_scripts(self) -> None:
        for bad in (
            [],
            {"t": 0},
            [{"t": -1}],
            [{"t": "0"}],
            [{"t": 1.0}, {"t": 0.5}],
            [{"t": 0, "move": [0.1]}],
            [{"t": 0, "do": 3}],
            [{}],
        ):
            with self.assertRaises(puppet_tool.ScriptError, msg=repr(bad)):
                puppet_tool.validate_frames(bad)
        self.assertEqual(len(puppet_tool.validate_frames([{"t": 0}, {"t": 0, "unknown": 1}])), 2)

    def test_streamer_rejects_bad_rates(self) -> None:
        with self.assertRaises(ValueError):
            puppet_tool.PuppetStreamer([{"t": 0}], lambda p: None, hz=0)
        with self.assertRaises(ValueError):
            puppet_tool.PuppetStreamer([{"t": 0}], lambda p: None, hz=50, hold_seconds=-1)


class PuppetToolCliTest(_PuppetAgentTest):
    def test_cli_streams_script_to_a_live_agent(self) -> None:
        self._start()
        self.assertTrue(self._load()["ok"])
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "frames.json"
            script.write_text(
                json.dumps(
                    [
                        {"t": 0.0, "move": {"vx": 0.1, "vy": 0.0, "vyaw": 0.0}},
                        {"t": 0.2, "move": {"vx": 0.1, "vy": 0.0, "vyaw": 0.0}, "do": "kick_left", "sound": "chirp"},
                    ]
                )
            )
            stderr = io.StringIO()
            real_stderr, sys.stderr = sys.stderr, stderr
            try:
                rc = puppet_tool.main(
                    ["--agent", f"127.0.0.1:{self.agent.bound_port}", "--script", str(script), "--hz", "50", "--hold-seconds", "0.2"]
                )
            finally:
                sys.stderr = real_stderr
            self.assertEqual(rc, 0, stderr.getvalue())
        self.assertGreaterEqual(len([1 for _, p in self._moves() if _approx(p, vx=0.1)]), 5)
        self.assertTrue(self._wait(lambda: self.robotd.by_method("robot.do")))
        self.assertEqual(self.robotd.by_method("robot.do")[0]["params"], {"skill": "kick_left"})
        self.assertEqual([m["params"]["tag"] for m in self.robotd.by_method("robot.sound")], ["chirp"])
        # The tool went quiet 0.3 s before returning: the deadman has zeroed the duck by now (or is about to).
        self.assertTrue(self._wait(lambda: _is_zero_move(self._moves()[-1][1]), timeout=1.0))
        self.assertEqual(self.agent.state, "loaded")

    def test_cli_rejects_bad_script_and_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "bad.json"
            script.write_text("[{\"t\": 1.0}, {\"t\": 0.0}]")
            stderr = io.StringIO()
            real_stderr, sys.stderr = sys.stderr, stderr
            try:
                self.assertEqual(puppet_tool.main(["--agent", "127.0.0.1:1", "--script", str(script)]), 2)
                self.assertIn("sorted", stderr.getvalue())
                script.write_text("[{\"t\": 0.0}]")
                self.assertEqual(puppet_tool.main(["--agent", "127.0.0.1:1", "--script", str(script), "--hz", "0"]), 2)
                with self.assertRaises(SystemExit):
                    puppet_tool.main(["--agent", "nohostport", "--script", str(script)])
            finally:
                sys.stderr = real_stderr


if __name__ == "__main__":
    unittest.main()
