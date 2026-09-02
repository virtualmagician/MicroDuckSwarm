"""Unit tests for python/tools/puppet.py -- the stdlib puppet-stream
sender (docs/swarmlink-protocol.md #6): script loading/validation
(`load_frames`/`validate_frames`) and the pure, injectable-clock
scheduler (`PuppetStreamer`), plus the `--agent HOST:PORT` argument
parser. No sockets: `PuppetStreamer` takes `send`/`clock`/`sleep` as
plain callables specifically so its scheduling can be tested instantly
and deterministically, per the module's own docstring ("load_frames /
PuppetStreamer are importable for tests").
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.puppet import (  # noqa: E402
    MAX_HZ,
    PuppetStreamer,
    ScriptError,
    load_frames,
    parse_agent_arg,
    validate_frames,
)


class FakeClock:
    """A monotonic seconds source paired with a `sleep` that just
    advances it -- makes PuppetStreamer's real-time schedule run
    instantly and deterministically under test.
    """

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _streamer(frames, **kwargs):
    sent: list[dict] = []
    clock = kwargs.pop("clock", None) or FakeClock()
    kwargs.setdefault("seq0", 1)
    kwargs.setdefault("master_time", lambda: 999)
    kwargs.setdefault("tail_s", 0.0)
    streamer = PuppetStreamer(frames, sent.append, clock=clock.now, sleep=clock.sleep, **kwargs)
    return streamer, sent, clock


# -- script loading / validation --------------------------------------------


class ValidateFramesTests(unittest.TestCase):
    def test_valid_script_passes_through(self):
        frames = [{"t": 0.0, "move": {"vx": 0.1}}, {"t": 1.0, "do": "kick_left"}]
        self.assertEqual(validate_frames(frames), frames)

    def test_empty_list_is_rejected(self):
        with self.assertRaises(ScriptError):
            validate_frames([])

    def test_non_list_is_rejected(self):
        with self.assertRaises(ScriptError):
            validate_frames({"t": 0.0})

    def test_non_dict_frame_is_rejected(self):
        with self.assertRaises(ScriptError):
            validate_frames([{"t": 0.0}, "not a frame"])

    def test_missing_t_is_rejected(self):
        with self.assertRaises(ScriptError):
            validate_frames([{"move": {"vx": 0.1}}])

    def test_negative_t_is_rejected(self):
        with self.assertRaises(ScriptError):
            validate_frames([{"t": -0.5}])

    def test_non_numeric_t_is_rejected(self):
        with self.assertRaises(ScriptError):
            validate_frames([{"t": "soon"}])

    def test_frames_must_be_sorted_by_t(self):
        with self.assertRaises(ScriptError):
            validate_frames([{"t": 1.0}, {"t": 0.5}])

    def test_equal_consecutive_t_is_allowed(self):
        # e.g. a do and a sound issued in the same instant, as separate
        # frames -- see PuppetStreamerActionQueueTests below.
        validate_frames([{"t": 0.0, "do": "kick_left"}, {"t": 0.0, "sound": "chirp"}])

    def test_channel_must_be_an_object(self):
        for channel in ("move", "head", "pose", "mouth"):
            with self.assertRaises(ScriptError):
                validate_frames([{"t": 0.0, channel: "nope"}])

    def test_do_and_sound_must_be_strings(self):
        with self.assertRaises(ScriptError):
            validate_frames([{"t": 0.0, "do": 5}])
        with self.assertRaises(ScriptError):
            validate_frames([{"t": 0.0, "sound": 5}])

    def test_unknown_keys_are_preserved_not_stripped(self):
        # Module docstring: "Unknown keys are ignored (kept as-is on the
        # frame, not sent)" -- validate_frames itself does not strip them;
        # PuppetStreamer is what omits them from the wire payload (see
        # PuppetStreamerPacketShapeTests below).
        frames = validate_frames([{"t": 0.0, "note": "for the editor"}])
        self.assertEqual(frames[0]["note"], "for the editor")


class LoadFramesTests(unittest.TestCase):
    def test_loads_a_valid_script_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "frames.json"
            path.write_text(json.dumps([{"t": 0.0, "move": {"vx": 0.1}}]))
            frames = load_frames(path)
            self.assertEqual(frames[0]["move"]["vx"], 0.1)

    def test_missing_file_raises_script_error(self):
        with self.assertRaises(ScriptError):
            load_frames(Path("/nonexistent/does-not-exist.json"))

    def test_malformed_json_raises_script_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "frames.json"
            path.write_text("{not json")
            with self.assertRaises(ScriptError):
                load_frames(path)

    def test_invalid_shape_raises_script_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "frames.json"
            path.write_text(json.dumps({"t": 0.0}))  # an object, not a list
            with self.assertRaises(ScriptError):
                load_frames(path)


# -- PuppetStreamer -----------------------------------------------------


class PuppetStreamerConstructionTests(unittest.TestCase):
    def test_hz_must_be_positive_and_at_most_max_hz(self):
        frames = [{"t": 0.0}]
        clock = FakeClock()
        with self.assertRaises(ValueError):
            PuppetStreamer(frames, lambda p: None, hz=0.0, clock=clock.now, sleep=clock.sleep)
        with self.assertRaises(ValueError):
            PuppetStreamer(frames, lambda p: None, hz=MAX_HZ + 1, clock=clock.now, sleep=clock.sleep)
        # MAX_HZ itself is fine (inclusive upper bound).
        PuppetStreamer(frames, lambda p: None, hz=MAX_HZ, clock=clock.now, sleep=clock.sleep)

    def test_hold_seconds_must_be_non_negative_and_finite(self):
        frames = [{"t": 0.0}]
        clock = FakeClock()
        with self.assertRaises(ValueError):
            PuppetStreamer(frames, lambda p: None, hold_seconds=-1.0, clock=clock.now, sleep=clock.sleep)
        with self.assertRaises(ValueError):
            PuppetStreamer(frames, lambda p: None, hold_seconds=float("inf"), clock=clock.now, sleep=clock.sleep)


class PuppetStreamerSingleFrameTests(unittest.TestCase):
    def test_single_frame_sends_exactly_once_by_default(self):
        streamer, sent, _ = _streamer([{"t": 0.0, "move": {"vx": 0.1}}])
        packets_sent = streamer.run()
        self.assertEqual(packets_sent, 1)
        self.assertEqual(len(sent), 1)

    def test_hold_seconds_repeats_the_last_frame(self):
        # hz=50 (period 0.02s), hold_seconds=0.05: sends at t=0, 0.02,
        # 0.04, 0.06 (the first tick at/after t_end=0.05) -- 4 packets,
        # worked out by hand against PuppetStreamer.run()'s loop.
        streamer, sent, clock = _streamer([{"t": 0.0, "move": {"vx": 0.1}}], hz=50.0, hold_seconds=0.05)
        packets_sent = streamer.run()
        self.assertEqual(packets_sent, 4)
        self.assertEqual(len(sent), 4)
        self.assertAlmostEqual(clock.t, 0.06)


class PuppetStreamerPacketShapeTests(unittest.TestCase):
    def test_packet_carries_protocol_envelope_and_injected_seq_and_master_time(self):
        streamer, sent, _ = _streamer(
            [{"t": 0.0, "move": {"vx": 0.1}}], seq0=42, master_time=lambda: 123456789
        )
        streamer.run()
        payload = sent[0]
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["type"], "puppet")
        self.assertEqual(payload["seq"], 42)
        self.assertEqual(payload["master_time"], 123456789)
        self.assertEqual(payload["move"], {"vx": 0.1})

    def test_seq_increments_once_per_packet_sent(self):
        streamer, sent, _ = _streamer([{"t": 0.0, "move": {"vx": 0.1}}], hz=50.0, hold_seconds=0.04, seq0=10)
        streamer.run()
        seqs = [p["seq"] for p in sent]
        self.assertEqual(seqs, list(range(10, 10 + len(seqs))))
        self.assertGreaterEqual(len(seqs), 2)

    def test_only_channels_present_on_the_frame_are_sent(self):
        streamer, sent, _ = _streamer([{"t": 0.0, "head": {"head_yaw": 0.3}}])
        streamer.run()
        payload = sent[0]
        self.assertIn("head", payload)
        self.assertNotIn("move", payload)
        self.assertNotIn("pose", payload)
        self.assertNotIn("mouth", payload)
        self.assertNotIn("do", payload)
        self.assertNotIn("sound", payload)

    def test_unknown_frame_keys_are_not_sent(self):
        streamer, sent, _ = _streamer([{"t": 0.0, "move": {"vx": 0.1}, "note": "editor-only"}])
        streamer.run()
        self.assertNotIn("note", sent[0])


class PuppetStreamerContinuousHoldTests(unittest.TestCase):
    def test_held_value_is_resent_every_tick_until_the_next_frame(self):
        # hz=50 (period 0.02s): frame0 at t=0 held through t<0.1, frame1
        # takes over at t=0.1 -- 6 packets total (5 holding frame0's
        # value, the 6th switching to frame1's), worked out by hand.
        frames = [{"t": 0.0, "move": {"vx": 0.1}}, {"t": 0.1, "move": {"vx": 0.0}}]
        streamer, sent, _ = _streamer(frames, hz=50.0)
        packets_sent = streamer.run()
        self.assertEqual(packets_sent, 6)
        vx_values = [p["move"]["vx"] for p in sent]
        self.assertEqual(vx_values, [0.1, 0.1, 0.1, 0.1, 0.1, 0.0])


class PuppetStreamerActionQueueTests(unittest.TestCase):
    def test_do_and_sound_on_the_same_frame_share_one_packet(self):
        streamer, sent, _ = _streamer([{"t": 0.0, "do": "kick_left", "sound": "chirp"}])
        streamer.run()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["do"], "kick_left")
        self.assertEqual(sent[0]["sound"], "chirp")

    def test_each_action_fires_exactly_once(self):
        streamer, sent, _ = _streamer(
            [{"t": 0.0, "do": "kick_left"}, {"t": 0.02, "move": {"vx": 0.0}}], hz=50.0
        )
        streamer.run()
        do_count = sum(1 for p in sent if "do" in p)
        self.assertEqual(do_count, 1)

    def test_two_actions_due_at_once_go_out_in_consecutive_packets_in_script_order(self):
        # Module docstring: "if a low --hz makes several frames' actions
        # due at once they go out in consecutive packets, in script
        # order." hz=1 (period 1.0s) collapses both t=0.0 do-frames into
        # the same tick.
        frames = [
            {"t": 0.0, "do": "kick_left"},
            {"t": 0.0, "do": "kick_right"},
            {"t": 1.0},
        ]
        streamer, sent, _ = _streamer(frames, hz=1.0)
        packets_sent = streamer.run()
        self.assertEqual(packets_sent, 2)
        self.assertEqual(sent[0]["do"], "kick_left")
        self.assertEqual(sent[1]["do"], "kick_right")


class PuppetStreamerTailTests(unittest.TestCase):
    def test_tail_s_sleeps_once_more_after_the_last_packet(self):
        clock = FakeClock()
        streamer, sent, _ = _streamer([{"t": 0.0, "move": {"vx": 0.1}}], clock=clock, tail_s=0.3)
        streamer.run()
        self.assertEqual(len(sent), 1)
        self.assertEqual(clock.sleeps[-1], 0.3)

    def test_tail_s_zero_sleeps_no_further(self):
        clock = FakeClock()
        streamer, sent, _ = _streamer([{"t": 0.0, "move": {"vx": 0.1}}], clock=clock, tail_s=0.0)
        streamer.run()
        self.assertEqual(clock.t, 0.0)


# -- CLI argument parsing -----------------------------------------------


class ParseAgentArgTests(unittest.TestCase):
    def test_valid_host_and_port(self):
        self.assertEqual(parse_agent_arg("127.0.0.1:47801"), ("127.0.0.1", 47801))

    def test_hostname_is_accepted(self):
        self.assertEqual(parse_agent_arg("duck-01.local:47801"), ("duck-01.local", 47801))

    def test_missing_colon_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_agent_arg("127.0.0.1")

    def test_non_integer_port_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_agent_arg("127.0.0.1:abc")

    def test_out_of_range_port_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_agent_arg("127.0.0.1:0")
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_agent_arg("127.0.0.1:70000")

    def test_empty_host_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_agent_arg(":47801")


if __name__ == "__main__":
    unittest.main()
