"""Coverage for python/duck_agent/agent.py paths that test_agent.py and
test_agent_safety.py don't exercise: telemetry's "degraded" state and
FAULT-recovery-via-load, seek's NACK/mode-reapply behavior, the plain
(no held sound, no failure) stop/end-of-show path, panic's neutral
head/pose notify and its effect on an ARMED schedule and on FAULT, and
load-time validation-error / policy-match NACKs.

Reuses the FakeRobotd/FakeMaster fakes from test_agent.py and the
_AgentTestBase/_write_show/ScriptedRobotd/SilentMaster helpers from
test_agent_safety.py rather than re-inventing them.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_agent import DEMO_SHA256, SHOWS_DIR, FakeMaster  # noqa: E402
from tests.test_agent_safety import ScriptedRobotd, SilentMaster, _AgentTestBase, _write_show  # noqa: E402

NS = 1_000_000_000


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Telemetry: "degraded" while PLAYING with sync lost, and FAULT recovery
# ---------------------------------------------------------------------------


class TelemetryStateTest(_AgentTestBase):
    def test_degraded_reported_while_playing_with_sync_lost(self) -> None:
        # A SilentMaster never answers time_req, but the agent still needs
        # one sample to *start* playing (test_play_without_time_sync_is_nacked
        # in test_agent_safety.py covers that NACK) -- so record one sample
        # directly, then let the clock go stale from there: a short
        # degraded_no_sample_s makes that "stale" arrive quickly.
        self._start(master=SilentMaster())
        self.agent.clock._degraded_no_sample_s = 0.2
        self.assertTrue(self._load()["ok"])
        now = time.monotonic_ns()
        self.agent.clock.record_exchange(t0=now, t1=now, t2=now, t3=now)
        ack = self._play(lead_s=0.05)
        self.assertTrue(ack["ok"], ack)
        self.assertTrue(self._wait_for_state("playing"))
        # The FSM stays "playing" throughout -- degraded is a telemetry
        # overlay, never an FSM state (agent.py's own module docstring).
        self.assertTrue(self._wait(lambda: self.agent.build_telemetry()["state"] == "degraded", timeout=2.0))
        self.assertEqual(self.agent.state, "playing")
        # And the duck keeps performing its local copy while degraded --
        # it does not stop emitting just because sync is gone.
        n = len(self.robotd.by_method("robot.head"))
        time.sleep(0.2)
        self.assertGreater(len(self.robotd.by_method("robot.head")), n, "must keep performing while degraded")

    def test_fault_recovers_via_load_while_robotd_still_disconnected(self) -> None:
        self._start(robotd=ScriptedRobotd())
        self._start_walking()
        self.robotd.sever(refuse_new=True)
        self.assertTrue(self._wait_for_state("fault"))
        self.assertTrue(self._wait(lambda: self.agent.build_telemetry()["state"] == "fault"))

        # play must still be refused from fault...
        nack = self._play(lead_s=0.2)
        self.assertFalse(nack["ok"])
        self.assertIn("fault", nack["error"])

        # ...but a fresh load recovers the FSM even though robotd is still
        # unreachable (docs/swarmlink-protocol.md #5: "accept load/panic").
        ack = self._load()
        self.assertTrue(ack["ok"], ack)
        self.assertEqual(self.agent.state, "loaded")
        # Telemetry must not lie about a duck whose robotd link is still
        # down just because the FSM recovered.
        self.assertEqual(self.agent.build_telemetry()["state"], "fault")

    def test_telemetry_shape_and_seq_increments(self) -> None:
        self._start()
        first = self.agent.build_telemetry()
        second = self.agent.build_telemetry()
        for msg in (first, second):
            self.assertEqual(msg["type"], "telemetry")
            self.assertEqual(msg["duck"], "duck-test")
            self.assertIn(msg["state"], ("idle", "loaded", "armed", "playing", "degraded", "fault"))
            self.assertIn("show", msg)
            self.assertIn("show_time", msg)
            self.assertIn("clock_offset_ms", msg)
            self.assertIn("clock_rtt_ms", msg)
            self.assertIn("policies_ok", msg)
            self.assertIn("last_error", msg)
        self.assertEqual(second["seq"], first["seq"] + 1)


# ---------------------------------------------------------------------------
# seek: NACK outside armed/playing, mode reapplied (or correctly not)
# ---------------------------------------------------------------------------


class SeekCoverageTest(_AgentTestBase):
    def test_seek_nacked_when_not_armed_or_playing(self) -> None:
        self._start()
        self.assertTrue(self._load()["ok"])  # LOADED, not armed/playing
        seek_id = self.master.send_cmd(self.agent_addr, "seek", show_time=5.0, at_master_time=time.monotonic_ns())
        acks = self.master.wait_for_ack(seek_id)
        self.assertTrue(acks)
        self.assertFalse(acks[0]["ok"])
        self.assertIn("not armed, playing or paused", acks[0]["error"])
        self.assertEqual(self.agent.state, "loaded")

    def test_seek_reapplies_latest_mode_event_and_skips_when_none_applies(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path, sha = _write_show(
                Path(d), "modetest", duration=30.0, events=[{"t": 2.0, "mode": "roller"}]
            )
            self._start(shows_dir=Path(d))
            self.assertTrue(self._load("modetest", sha)["ok"])
            self._wait_for_sync()
            self.assertTrue(self._play(show="modetest", from_show_time=0.0)["ok"])
            self.assertTrue(self._wait_for_state("playing"))

            seek_id = self.master.send_cmd(
                self.agent_addr, "seek", show_time=5.0, at_master_time=time.monotonic_ns()
            )
            acks = self.master.wait_for_ack(seek_id)
            self.assertTrue(acks and acks[0]["ok"], acks)
            self.assertTrue(
                self._wait(lambda: self.agent.current_mode == "roller"),
                "seeking past t=2.0's mode event must re-apply it",
            )
            set_modes = self.robotd.by_method("robot.setMode")
            self.assertEqual(len(set_modes), 1)
            self.assertEqual(set_modes[0]["params"], {"mode": "roller"})

            # Seek back to show_time=0.0: mode_at(0.0) is None (no mode
            # event has t <= 0.0), so the agent must NOT force a second
            # setMode call -- docs/duckshow-format.md: mode events fire
            # once; a seek only re-applies "the latest mode event <= the
            # seek point", and there isn't one here.
            seek_id2 = self.master.send_cmd(
                self.agent_addr, "seek", show_time=0.0, at_master_time=time.monotonic_ns()
            )
            acks2 = self.master.wait_for_ack(seek_id2)
            self.assertTrue(acks2 and acks2[0]["ok"], acks2)
            time.sleep(0.3)
            self.assertEqual(len(self.robotd.by_method("robot.setMode")), 1, "no spurious re-application of mode")
            self.assertEqual(self.agent.current_mode, "roller")


# ---------------------------------------------------------------------------
# plain stop / end-of-show (no held sound, no robotd failure)
# ---------------------------------------------------------------------------


class PlainStopAndEndOfShowTest(_AgentTestBase):
    def test_stop_zeroes_locomotion_and_returns_to_loaded(self) -> None:
        self._start()
        self._start_walking()
        stop_id = self.master.send_cmd(self.agent_addr, "stop")
        acks = self.master.wait_for_ack(stop_id)
        self.assertTrue(acks and acks[0]["ok"], acks)
        self.assertEqual(self.agent.state, "loaded")
        self.assertEqual(self._last_move(), {"vx": 0.0, "vy": 0.0, "vyaw": 0.0})
        self.assertTrue(self.robotd.by_method("robot.stop"))
        n = len(self.robotd.by_method("robot.move"))
        time.sleep(0.3)
        self.assertEqual(len(self.robotd.by_method("robot.move")), n, "no further intents after a graceful stop")

    def test_end_of_show_sends_stop_and_returns_to_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _, sha = _write_show(
                Path(d), "short", duration=0.4, locomotion=[{"t": 0.0, "vx": 0.1}, {"t": 0.3, "vx": 0.1}]
            )
            self._start(shows_dir=Path(d))
            self.assertTrue(self._load("short", sha)["ok"])
            self._wait_for_sync()
            self.assertTrue(self._play(show="short")["ok"])
            self.assertTrue(self._wait_for_state("loaded", timeout=3.0), self.agent.state)
            self.assertTrue(self.robotd.by_method("robot.stop"))
            self.assertEqual(self._last_move(), {"vx": 0.0, "vy": 0.0, "vyaw": 0.0})


# ---------------------------------------------------------------------------
# panic: neutral head/pose, cancels an ARMED schedule, ACKs from FAULT
# ---------------------------------------------------------------------------


class PanicCoverageTest(_AgentTestBase):
    def test_panic_sends_neutral_head_and_pose(self) -> None:
        self._start()
        self.assertTrue(self._wait(lambda: self.agent.robotd.connected), "robotd never connected")
        panic_id = self.master.send_cmd(self.agent_addr, "panic")
        acks = self.master.wait_for_ack(panic_id)
        self.assertTrue(acks and acks[0]["ok"], "panic must never be NACKed")
        self.assertTrue(self._wait_for_state("idle"))
        heads = self.robotd.wait_for_method("robot.head", count=1, timeout=2.0)
        poses = self.robotd.wait_for_method("robot.pose", count=1, timeout=2.0)
        self.assertTrue(heads and poses, "panic must notify neutral robot.head and robot.pose")
        self.assertEqual(heads[-1]["params"], {"neck_pitch": 0.0, "head_pitch": 0.0, "head_yaw": 0.0, "head_roll": 0.0})
        self.assertEqual(poses[-1]["params"], {"z": 0.0, "roll": 0.0, "pitch": 0.0, "active": False})
        self.assertTrue(self.robotd.by_method("robot.stop"))

    def test_panic_during_armed_cancels_scheduled_start(self) -> None:
        self._start()
        self.assertTrue(self._load()["ok"])
        self._wait_for_sync()
        ack = self._play(lead_s=1.5)
        self.assertTrue(ack["ok"], ack)
        self.assertTrue(self._wait_for_state("armed"))

        panic_id = self.master.send_cmd(self.agent_addr, "panic")
        acks = self.master.wait_for_ack(panic_id)
        self.assertTrue(acks and acks[0]["ok"])
        self.assertTrue(self._wait_for_state("idle"))

        # panic itself sends exactly one zeroing robot.move (belt-and-
        # braces for a duck that *was* moving); the point of this test is
        # that the tick loop's scheduled start from ARMED must not add a
        # second, non-zero one once the original start time arrives.
        moves_after_panic = self.robotd.by_method("robot.move")
        self.assertEqual(len(moves_after_panic), 1)
        self.assertEqual(moves_after_panic[0]["params"], {"vx": 0.0, "vy": 0.0, "vyaw": 0.0})

        # Outlive the original scheduled start; the number must never begin.
        time.sleep(1.9)
        self.assertEqual(self.agent.state, "idle")
        self.assertEqual(len(self.robotd.by_method("robot.move")), 1, "scheduled start fired after panic")

    def test_panic_from_fault_state_acks_and_returns_idle(self) -> None:
        self._start(robotd=ScriptedRobotd())
        self._start_walking()
        self.robotd.sever()
        self.assertTrue(self._wait_for_state("fault"))

        panic_id = self.master.send_cmd(self.agent_addr, "panic")
        acks = self.master.wait_for_ack(panic_id)
        self.assertTrue(acks and acks[0]["ok"], "panic must never be NACKed, even from fault")
        self.assertTrue(self._wait_for_state("idle"))


# ---------------------------------------------------------------------------
# load: validation-error NACK, and the policy-present-with-matching-hash OK path
# ---------------------------------------------------------------------------


class LoadValidationAndPolicyTest(_AgentTestBase):
    def test_load_nacks_on_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _, sha = _write_show(Path(d), "toofast", locomotion=[{"t": 0.0, "vx": 0.5}])
            self._start(shows_dir=Path(d))
            ack = self._load("toofast", sha)
            self.assertFalse(ack["ok"])
            self.assertIn("validation failed", ack["error"])
            self.assertIn("vx=0.5", ack["error"])
            self.assertEqual(self.agent.state, "idle")

    def test_load_ok_when_policy_present_with_matching_hash(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            shows_dir = Path(d)
            policy_bytes = b"pretend-onnx-bytes"
            policy_dir = shows_dir / "policies"
            policy_dir.mkdir(parents=True)
            (policy_dir / "walk.onnx").write_bytes(policy_bytes)
            policy = {
                "name": "walk",
                "file": "policies/walk.onnx",
                "sha256": _sha256_bytes(policy_bytes),
                "slot": "walk",
            }
            _, sha = _write_show(shows_dir, "haspolicy", policies=[policy])
            self._start(shows_dir=shows_dir)
            ack = self._load("haspolicy", sha)
            self.assertTrue(ack["ok"], ack)
            self.assertEqual(self.agent.state, "loaded")
            telemetry = self.agent.build_telemetry()
            self.assertTrue(telemetry["policies_ok"])
            self.assertIsNone(telemetry["last_error"])


if __name__ == "__main__":
    unittest.main()
