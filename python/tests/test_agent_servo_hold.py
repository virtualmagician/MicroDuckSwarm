"""A servo `{"mode": "hold"}` window must COMMAND zero velocity, not stop
commanding.

docs/duckshow-format.md has always described this mode as "freeze locomotion".
The agent implemented it by skipping the whole locomotion block, which leaves
`move_out` as None, so no `robot.move` is emitted at all for the duration of
the window. A duck that was walking into the hold therefore keeps its last
commanded velocity until robotd's 500 ms deadman (deploy/robotd.toml) happens
to catch it: up to half a second of unwanted travel at the start of every
freeze, and a silent dependence on a safety net that exists for lost links,
not for choreography.

Surfaced while designing the timeline control track (a "pause" cue), whose
first draft proposed reusing this same path to hold the cast -- which would
have inherited the bug at exactly the moment eight ducks are meant to stand
still together.

Reuses test_agent.py's FakeRobotd/FakeMaster rather than a second copy.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_agent import FakeMaster, FakeRobotd, _sha256_file  # noqa: E402
from duck_agent.agent import DuckAgent  # noqa: E402

WALK_VX = 0.3
HOLD_T = 0.3
HOLD_DURATION = 3.0


def _hold_show() -> dict:
    """One role walking at WALK_VX from t=0, frozen by a servo hold at HOLD_T."""
    return {
        "format": "duckshow/1",
        "meta": {"name": "servo hold probe", "duration": 6.0, "bpm": 120},
        "cast": [{"role": "lead"}],
        "tracks": {
            "lead": {
                "locomotion": [
                    {"t": 0.0, "vx": WALK_VX, "vy": 0.0, "vyaw": 0.0, "interp": "linear"},
                    {"t": 6.0, "vx": WALK_VX, "vy": 0.0, "vyaw": 0.0, "interp": "linear"},
                ],
                "servo": [{"t": HOLD_T, "mode": "hold", "duration": HOLD_DURATION}],
            }
        },
    }


class ServoHoldFreezesLocomotion(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        shows_dir = Path(self.tmp.name)
        show_dir = shows_dir / "holdprobe"
        show_dir.mkdir()
        self.show_path = show_dir / "holdprobe.duckshow.json"
        self.show_path.write_text(json.dumps(_hold_show(), indent=2), encoding="utf-8")
        self.sha = _sha256_file(self.show_path)

        self.robotd = FakeRobotd()
        self.master = FakeMaster()
        self.agent = DuckAgent(
            duck_id="duck-hold",
            robotd_target=self.robotd.target,
            shows_dir=shows_dir,
            listen_port=0,
        )
        self.agent.start()
        self.agent_addr = ("127.0.0.1", self.agent.bound_port)
        self.addCleanup(self.agent.stop)
        self.addCleanup(self.robotd.stop)
        self.addCleanup(self.master.stop)

    def _play(self) -> None:
        cmd_id = self.master.send_cmd(
            self.agent_addr, "load", show="holdprobe", sha256=self.sha, role="lead"
        )
        acks = self.master.wait_for_ack(cmd_id)
        self.assertTrue(acks and acks[0]["ok"], f"load not acked: {acks}")
        at = time.monotonic_ns() + 200_000_000
        cmd_id = self.master.send_cmd(
            self.agent_addr, "play", show="holdprobe", at_master_time=at, from_show_time=0.0
        )
        acks = self.master.wait_for_ack(cmd_id)
        self.assertTrue(acks and acks[0]["ok"], f"play not acked: {acks}")

    def _moves_after(self, wall: float) -> list[dict]:
        return [
            m for t, m in self.robotd.all_messages()
            if t >= wall and m.get("method") == "robot.move"
        ]

    def test_walks_before_the_hold(self) -> None:
        """Guards the test itself: without this the zero assertion below would
        pass for a duck that never moved at all."""
        self._play()
        moves = self.robotd.wait_for_method("robot.move", count=3, timeout=3.0)
        self.assertTrue(moves, "no robot.move at all while playing")
        self.assertTrue(
            any(abs(m["params"].get("vx", 0.0) - WALK_VX) < 1e-6 for m in moves),
            f"expected vx={WALK_VX} before the hold, saw {[m['params'].get('vx') for m in moves[:6]]}",
        )

    def test_hold_keeps_emitting_move_intents(self) -> None:
        """The freeze must not go silent: robotd's deadman is a safety net for
        a lost link, not the mechanism choreography stops a duck with."""
        self._play()
        self.robotd.wait_for_method("robot.move", count=2, timeout=3.0)
        time.sleep(HOLD_T + 0.5)          # comfortably inside the hold window
        mark = time.monotonic()
        time.sleep(0.4)                    # ~20 ticks at 50 Hz
        during = self._moves_after(mark)
        self.assertGreaterEqual(
            len(during), 5,
            "a servo hold emitted (almost) no robot.move -- the duck is coasting on "
            "robotd's 500 ms deadman instead of being commanded to stop",
        )

    def test_hold_commands_zero_velocity(self) -> None:
        self._play()
        self.robotd.wait_for_method("robot.move", count=2, timeout=3.0)
        time.sleep(HOLD_T + 0.5)
        mark = time.monotonic()
        time.sleep(0.4)
        during = self._moves_after(mark)
        self.assertTrue(during, "no robot.move during the hold window")
        for m in during:
            p = m["params"]
            self.assertAlmostEqual(p.get("vx", 0.0), 0.0, places=6, msg=f"vx not zero during hold: {p}")
            self.assertAlmostEqual(p.get("vy", 0.0), 0.0, places=6, msg=f"vy not zero during hold: {p}")
            self.assertAlmostEqual(p.get("vyaw", 0.0), 0.0, places=6, msg=f"vyaw not zero during hold: {p}")


if __name__ == "__main__":
    unittest.main()
