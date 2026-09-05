"""Operator pause/resume (docs/swarmlink-protocol.md "Pause and resume").

The transport half of the timeline control track: authored hold points will
trigger this same mechanism locally rather than a second one, so the rules
these tests pin are the ones a hold will inherit.

Three of them exist because the control-track design review found each as a
way to split the cast on stage:

  * both commands are PARKED, not validated on arrival, because both reference
    masters break their retry loop on any ACK -- a NACK included -- so one
    early NACK would strand one duck for the rest of the show;
  * resume is idempotent by STATE, not just by cmd_id, because two GO presses
    are two cmd_ids and the second would otherwise re-anchor this duck's clock
    away from the rest of the cast;
  * a pause COMMANDS zero rather than going silent, because silence leaves the
    duck coasting until robotd's 500 ms deadman catches it.
"""

from __future__ import annotations

import time
import unittest

from tests.test_agent import DEMO_SHA256, FakeMaster, FakeRobotd, SHOWS_DIR  # noqa: E402
from duck_agent.agent import DuckAgent  # noqa: E402


class PauseResume(unittest.TestCase):
    def setUp(self) -> None:
        self.robotd = FakeRobotd()
        self.master = FakeMaster()
        self.agent = DuckAgent(
            duck_id="duck-pause",
            robotd_target=self.robotd.target,
            shows_dir=SHOWS_DIR,
            listen_port=0,
        )
        self.agent.start()
        self.agent_addr = ("127.0.0.1", self.agent.bound_port)
        self.addCleanup(self.agent.stop)
        self.addCleanup(self.robotd.stop)
        self.addCleanup(self.master.stop)

    def _play(self) -> None:
        cmd_id = self.master.send_cmd(
            self.agent_addr, "load", show="demo", sha256=DEMO_SHA256, role="lead"
        )
        self.assertTrue(self.master.wait_for_ack(cmd_id)[0]["ok"])
        at = time.monotonic_ns() + 150_000_000
        cmd_id = self.master.send_cmd(
            self.agent_addr, "play", show="demo", at_master_time=at, from_show_time=0.0
        )
        self.assertTrue(self.master.wait_for_ack(cmd_id)[0]["ok"])
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self.agent.state == "playing":
                return
            time.sleep(0.02)
        self.fail("agent never reached playing")

    def _send(self, cmd: str, **fields) -> dict:
        cmd_id = self.master.send_cmd(self.agent_addr, cmd, **fields)
        acks = self.master.wait_for_ack(cmd_id)
        self.assertTrue(acks, f"no ack for {cmd}")
        return acks[0]

    def _pause_now(self) -> None:
        ack = self._send("pause", at_master_time=time.monotonic_ns())
        self.assertTrue(ack["ok"], ack)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self.agent.state == "paused":
                return
            time.sleep(0.02)
        self.fail("agent never reached paused")

    def _moves_after(self, wall: float) -> list[dict]:
        return [m for t, m in self.robotd.all_messages()
                if t >= wall and m.get("method") == "robot.move"]

    def test_pause_freezes_the_show_clock(self) -> None:
        self._play()
        time.sleep(0.3)
        self._pause_now()
        first = self.agent.build_telemetry()["show_time"]
        time.sleep(0.5)
        second = self.agent.build_telemetry()["show_time"]
        self.assertAlmostEqual(first, second, places=4, msg="show clock advanced while paused")
        self.assertGreater(first, 0.0, "test is vacuous if the clock never ran")

    def test_pause_commands_zero_rather_than_going_silent(self) -> None:
        self._play()
        self._pause_now()
        mark = time.monotonic()
        time.sleep(0.4)
        during = self._moves_after(mark)
        self.assertGreaterEqual(
            len(during), 5,
            "a paused duck emitted (almost) no robot.move -- it is coasting on the deadman",
        )
        for m in during:
            p = m["params"]
            self.assertAlmostEqual(p.get("vx", 0.0), 0.0, places=6, msg=f"vx not zero: {p}")
            self.assertAlmostEqual(p.get("vy", 0.0), 0.0, places=6, msg=f"vy not zero: {p}")
            self.assertAlmostEqual(p.get("vyaw", 0.0), 0.0, places=6, msg=f"vyaw not zero: {p}")

    def test_resume_continues_from_where_it_stopped(self) -> None:
        self._play()
        time.sleep(0.3)
        self._pause_now()
        frozen = self.agent.build_telemetry()["show_time"]
        time.sleep(0.5)  # wall time passes; show time must not

        ack = self._send("resume", at_master_time=time.monotonic_ns())
        self.assertTrue(ack["ok"], ack)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self.agent.state != "playing":
            time.sleep(0.02)
        self.assertEqual(self.agent.state, "playing")

        time.sleep(0.2)
        after = self.agent.build_telemetry()["show_time"]
        # Continues from the frozen point: strictly ahead of it, but nowhere
        # near the 0.5 s of wall time that elapsed while held.
        self.assertGreater(after, frozen)
        self.assertLess(after, frozen + 0.45, f"resume jumped: frozen={frozen} after={after}")

    def test_second_resume_while_playing_is_a_no_op(self) -> None:
        """Two GO presses are two cmd_ids. Without a state check the second
        would re-anchor this duck's epoch away from the rest of the cast."""
        self._play()
        time.sleep(0.3)
        self._pause_now()
        self._send("resume", at_master_time=time.monotonic_ns())
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self.agent.state != "playing":
            time.sleep(0.02)
        time.sleep(0.25)
        before = self.agent.build_telemetry()["show_time"]

        ack = self._send("resume", at_master_time=time.monotonic_ns())
        self.assertTrue(ack["ok"], "a redundant resume must be ACKed, not NACKed")
        time.sleep(0.2)
        after = self.agent.build_telemetry()["show_time"]
        self.assertGreater(after, before, "the clock must keep running")
        self.assertLess(abs((after - before) - 0.2), 0.12,
                        f"a second resume re-anchored the clock: {before} -> {after}")

    def test_resume_arriving_before_the_pause_lands_is_not_nacked(self) -> None:
        """Parked, not arrival-validated. A NACK here would be fatal: both
        reference masters abandon their retries on any ACK, NACK included."""
        self._play()
        ack = self._send("resume", at_master_time=time.monotonic_ns())
        self.assertTrue(ack["ok"], f"resume while playing must be a no-op ACK, got {ack}")
        self.assertEqual(self.agent.state, "playing")

    def test_stop_clears_the_pause(self) -> None:
        self._play()
        self._pause_now()
        ack = self._send("stop")
        self.assertTrue(ack["ok"], ack)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self.agent.state != "loaded":
            time.sleep(0.02)
        self.assertEqual(self.agent.state, "loaded", "stop must leave a paused duck LOADED, not stuck")


    def test_panic_from_paused_reaches_idle(self) -> None:
        """CLAUDE.md rule 5: panic always works from any state. A hold is a
        state, so it has to work from inside one."""
        self._play()
        self._pause_now()
        ack = self._send("panic")
        self.assertTrue(ack["ok"], "panic must never be NACKed")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self.agent.state != "idle":
            time.sleep(0.02)
        self.assertEqual(self.agent.state, "idle")

    def test_seek_while_paused_moves_the_frozen_point_and_stays_paused(self) -> None:
        """Scrubbing during a hold must not start this duck performing alone --
        the control-track review's "seek out of a hold strands the duck", in
        the opposite direction."""
        self._play()
        time.sleep(0.3)
        self._pause_now()
        self._send("seek", show_time=4.0, at_master_time=time.monotonic_ns())
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if abs(self.agent.build_telemetry()["show_time"] - 4.0) < 1e-6:
                break
            time.sleep(0.02)
        self.assertAlmostEqual(self.agent.build_telemetry()["show_time"], 4.0, places=4)
        self.assertEqual(self.agent.state, "paused", "a seek must not silently un-pause the duck")
        time.sleep(0.3)
        self.assertAlmostEqual(self.agent.build_telemetry()["show_time"], 4.0, places=4,
                               msg="clock resumed running after a seek while paused")


if __name__ == "__main__":
    unittest.main()
