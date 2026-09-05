"""Relax: the state in which a duck is safe to pick up, and the neutral that
stop always owed but never paid.

Two defects motivate this file.

The first is that nothing in the agent ever closed the bill. `robot.mouth` was
emitted only from the two tick paths, so a show that ended (or was stopped)
between the `mouthOpen` keyframe and its close left the bill open on stage
indefinitely -- not stop, not end-of-show, and not even panic put it back.
`_notify_neutral` had always sent head and pose; the mouth was simply missing
from it.

The second is that there was no safe state at all. docs/robotd-api.md is
explicit that `robot.stop` leaves the duck standing -- "it does not go limp or
collapse" -- so an operator repositioning the cast between chapters was lifting
a powered, actively balancing robot frozen in its last commanded pose. `relax`
is that state, and these tests pin its edges: it is refused mid-show, it is
idempotent, it silences the puppet stream, and the play path re-torques before
it arms so a relaxed duck never tries to perform limp.

The physical effect of `robot.relax` is inferred rather than documented (see
docs/swarmlink-protocol.md); what is asserted here is the *agent's* behaviour
around it, which is testable regardless of what the real daemon turns out to
do.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_agent import _sha256_file  # noqa: E402
from tests.test_agent_puppet import PuppetSender  # noqa: E402
from tests.test_agent_safety import _AgentTestBase  # noqa: E402


def _params(msgs: list[dict]) -> list[dict]:
    return [m.get("params", {}) for m in msgs]


class _RelaxTest(_AgentTestBase):
    def setUp(self) -> None:
        self._start()
        # relax and its inverse are robotd round trips, so both are NACKed
        # until the agent has actually dialled the daemon.
        self.assertTrue(self._wait(lambda: self.agent.robotd.connected), "robotd never connected")

    def _relax(self, on: Optional[bool] = None, timeout: float = 2.0) -> dict:
        fields: dict[str, Any] = {} if on is None else {"on": on}
        cmd_id = self.master.send_cmd(self.agent_addr, "relax", **fields)
        acks = self.master.wait_for_ack(cmd_id, timeout=timeout)
        self.assertTrue(acks, "no ack received for relax")
        return acks[0]

    def _telemetry(self, timeout: float = 3.0) -> dict:
        msgs = self.master.wait_for(lambda m: m.get("type") == "telemetry", timeout=timeout)
        self.assertTrue(msgs, "no telemetry received")
        return msgs[-1]

    def _wait_telemetry(self, key: str, value: Any, timeout: float = 3.0) -> bool:
        # Telemetry only flows once the agent has learned a master address,
        # which it does from an inbound command. A `stop` from IDLE is the
        # cheapest one that changes nothing.
        self.master.send_cmd(self.agent_addr, "stop")
        return self._wait(
            lambda: any(
                m.get("type") == "telemetry" and m.get(key) == value
                for _, m, _ in self.master.messages()
            ),
            timeout,
        )


class StopClosesTheBill(_RelaxTest):
    """The demo show opens the bill at 9.3 and closes it at 9.8. Stopping in
    between must not leave it open."""

    def test_stop_mid_mouth_closes_the_bill(self) -> None:
        self._load()
        self._wait_for_sync()
        self.assertTrue(self._play(from_show_time=9.35)["ok"])
        self.assertTrue(self._wait_for_state("playing"))
        self.assertTrue(
            self._wait(lambda: any(
                p.get("open", 0.0) > 0.5 for p in _params(self.robotd.by_method("robot.mouth"))
            )),
            "the show never opened the bill, so this test proves nothing",
        )

        cmd_id = self.master.send_cmd(self.agent_addr, "stop")
        self.assertTrue(self.master.wait_for_ack(cmd_id), "no ack for stop")
        self.assertTrue(self._wait_for_state("loaded"))

        self.assertTrue(
            self._wait(lambda: (_params(self.robotd.by_method("robot.mouth")) or [{"open": 1.0}])[-1]
                       .get("open", 1.0) == 0.0),
            f"bill left open after stop: {_params(self.robotd.by_method('robot.mouth'))[-3:]}",
        )

    def test_panic_closes_the_bill_too(self) -> None:
        self._load()
        self._wait_for_sync()
        self.assertTrue(self._play(from_show_time=9.35)["ok"])
        self.assertTrue(self._wait_for_state("playing"))
        cmd_id = self.master.send_cmd(self.agent_addr, "panic")
        self.assertTrue(self.master.wait_for_ack(cmd_id), "no ack for panic")
        self.assertTrue(
            self._wait(lambda: (_params(self.robotd.by_method("robot.mouth")) or [{"open": 1.0}])[-1]
                       .get("open", 1.0) == 0.0),
            "panic left the bill open",
        )


class EndOfShowClosesTheBill(_AgentTestBase):
    """meta.duration is the only end-of-show trigger, and every duck reaches it
    locally -- no stop command is involved. That path neutralised nothing, so a
    show whose mouth track ends open ended with the bill open and nothing in
    the system left to close it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        shows_dir = Path(self.tmp.name)
        doc = {
            "format": "duckshow/1",
            "meta": {"name": "openbill", "duration": 1.0},
            "requires": {"policies": []},
            "cast": [{"role": "lead"}],
            # No closing keyframe: the bill is open when duration arrives.
            "tracks": {"lead": {"mouth": [{"t": 0.0, "open": 1.0}]}},
        }
        path = shows_dir / "openbill.duckshow.json"
        path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        self.sha = _sha256_file(path)
        self._start(shows_dir=shows_dir)
        self.assertTrue(self._wait(lambda: self.agent.robotd.connected), "robotd never connected")

    def test_the_show_ending_by_itself_closes_the_bill(self) -> None:
        self._load(show="openbill", sha256=self.sha)
        self._wait_for_sync()
        self.assertTrue(self._play(show="openbill")["ok"])
        self.assertTrue(self._wait_for_state("playing"))
        self.assertTrue(
            self._wait(lambda: any(
                p.get("open", 0.0) > 0.5 for p in _params(self.robotd.by_method("robot.mouth"))
            )),
            "the show never opened the bill, so this test proves nothing",
        )
        self.assertTrue(self._wait_for_state("loaded", timeout=5.0), "the show never ended on its own")
        self.assertEqual(_params(self.robotd.by_method("robot.mouth"))[-1].get("open"), 0.0,
                         "the show ended with the bill open")

        # ...and the neutral lands before the stop, so robotd's last
        # instruction from a finished show is still robot.stop.
        methods = [m.get("method") for _, m in self.robotd.all_messages()]
        self.assertEqual(methods[-1], "robot.stop", f"end-of-show ordering wrong, tail was {methods[-4:]}")


class RelaxCommand(_RelaxTest):
    def test_relax_from_idle_neutralises_then_releases_torque(self) -> None:
        ack = self._relax()
        self.assertTrue(ack["ok"], ack)
        self.assertTrue(self.robotd.wait_for_method("robot.relax"), "no robot.relax sent")

        # Order matters: neutral has to land while the servos can still act on
        # it, or the duck is relaxed in whatever shape the last frame left it.
        order = [m.get("method") for _, m in self.robotd.all_messages()
                 if m.get("method") in ("robot.head", "robot.pose", "robot.mouth", "robot.relax")]
        self.assertIn("robot.relax", order)
        relax_at = order.index("robot.relax")
        for method in ("robot.head", "robot.pose", "robot.mouth"):
            self.assertIn(method, order[:relax_at], f"{method} was not sent before robot.relax")

        self.assertEqual(_params(self.robotd.by_method("robot.mouth"))[-1].get("open"), 0.0)

    def test_relax_is_reported_in_telemetry(self) -> None:
        self.assertTrue(self._wait_telemetry("relaxed", False), "relaxed missing from telemetry")
        self.assertTrue(self._relax()["ok"])
        self.assertTrue(self._wait_telemetry("relaxed", True), "relax not reported")

    def test_relax_off_re_torques_and_clears_the_flag(self) -> None:
        self.assertTrue(self._relax()["ok"])
        self.assertTrue(self._wait(lambda: self.agent.relaxed))
        self.assertTrue(self._relax(on=False)["ok"])
        enables = self.robotd.wait_for_method("robot.enable")
        self.assertTrue(enables, "relax off sent no robot.enable")
        self.assertEqual(enables[-1]["params"], {"on": True, "toggle": False})
        self.assertTrue(self._wait(lambda: not self.agent.relaxed))

    def test_relax_twice_is_one_relax(self) -> None:
        # Idempotent by state, like pause/resume: a repeated command from a
        # retrying master (or a twitchy operator) must not re-send.
        self.assertTrue(self._relax()["ok"])
        self.assertTrue(self.robotd.wait_for_method("robot.relax"))
        self.assertTrue(self._relax()["ok"], "a second relax must ACK, not NACK")
        time.sleep(0.2)
        self.assertEqual(len(self.robotd.by_method("robot.relax")), 1)

    def test_relax_off_when_already_torqued_sends_nothing(self) -> None:
        self.assertTrue(self._relax(on=False)["ok"])
        time.sleep(0.2)
        self.assertEqual(self.robotd.by_method("robot.enable"), [])

    def test_relax_is_refused_while_playing(self) -> None:
        self._load()
        self._wait_for_sync()
        self.assertTrue(self._play()["ok"])
        self.assertTrue(self._wait_for_state("playing"))
        ack = self._relax()
        self.assertFalse(ack["ok"], "a duck went limp mid-show")
        self.assertIn("stop first", ack["error"])
        self.assertEqual(self.robotd.by_method("robot.relax"), [])


class RelaxedDuckDoesNotPerformLimp(_RelaxTest):
    def test_play_re_enables_before_it_arms(self) -> None:
        self._load()
        self._wait_for_sync()
        self.assertTrue(self._relax()["ok"])
        self.assertTrue(self._wait(lambda: self.agent.relaxed))

        self.assertTrue(self._play(lead_s=0.3)["ok"])
        enables = self.robotd.wait_for_method("robot.enable")
        self.assertTrue(enables, "play on a relaxed duck sent no robot.enable")

        # ...and it landed before the first frame of the show, not after it.
        methods = [m.get("method") for _, m in self.robotd.all_messages()]
        self.assertIn("robot.enable", methods)
        if "robot.move" in methods:
            self.assertLess(methods.index("robot.enable"), methods.index("robot.move"),
                            "the duck was commanded to move before it was re-torqued")
        self.assertTrue(self._wait(lambda: not self.agent.relaxed))

    def test_puppet_packets_are_dropped_while_relaxed(self) -> None:
        self.assertTrue(self._relax()["ok"])
        self.assertTrue(self._wait(lambda: self.agent.relaxed))
        before = len(self.robotd.by_method("robot.move"))
        sender = PuppetSender(self.master, self.agent_addr)
        self.addCleanup(sender.stop)
        sender.start(move={"vx": 0.2, "vy": 0.0, "vyaw": 0.0})
        time.sleep(0.4)
        self.assertGreater(sender.packets_sent, 5, "the sender never ran")
        self.assertEqual(len(self.robotd.by_method("robot.move")), before,
                         "a limp duck was commanded to walk")

    def test_a_robotd_reconnect_drops_the_torque_claim(self) -> None:
        # A fresh daemon's torque state is not ours to assert; telemetry must
        # stop claiming `relaxed` rather than report a state we did not set.
        self.assertTrue(self._relax()["ok"])
        self.assertTrue(self._wait(lambda: self.agent.relaxed))
        self.robotd.sever()
        self.assertTrue(self._wait(lambda: not self.agent.relaxed, timeout=5.0),
                        "still claiming relaxed after robotd came back")


if __name__ == "__main__":
    unittest.main()
