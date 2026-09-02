#!/usr/bin/env python3
"""puppet.py -- stream scripted puppet frames to ONE duck-agent.

The stdlib-only sender side of docs/swarmlink-protocol.md section 6 (the
puppet stream) for tests, CI, and poking a duck by hand without a gamepad:

    python3 tools/puppet.py --agent 127.0.0.1:47801 --script frames.json [--hz 50] [--hold-seconds N]

`frames.json` is docs/authoring.md's input-frame list, but expressed
directly as puppet payloads -- a JSON list of timed frames, every field
except `t` optional:

    [
      {"t": 0.0, "move": {"vx": 0.1, "vy": 0.0, "vyaw": 0.0}},
      {"t": 1.0, "move": {"vx": 0.1}, "head": {"head_yaw": 0.5}, "do": "kick_left"},
      {"t": 2.0, "move": {"vx": 0.0}, "sound": "chirp"}
    ]

Semantics:

  * Frames must be sorted by `t` (seconds from the start of the stream).
    Nothing is sent before the first frame's `t`.
  * Every tick (1/--hz s) the *current* frame -- the last one whose `t`
    has been reached -- is sent: its continuous channels (`move`, `head`,
    `pose`, `mouth`) are held and re-sent until the next frame's `t`. A
    frame is a complete assertion: a channel it does not carry is not
    sent, so the agent's per-channel deadman releases it (an override
    ends, locomotion is zeroed) 250 ms later.
  * `do`/`sound` are sent exactly once each, in the first packet at or
    after their frame's `t` (one `do` and one `sound` per packet; if a
    low --hz makes several frames' actions due at once they go out in
    consecutive packets, in script order).
  * The last frame is sent once at its `t`, or repeatedly until
    `t + --hold-seconds` when that is given. The tool then waits 0.3 s
    (the agent's 250 ms deadman fires and zeroes the duck) and exits 0.
  * `seq` starts at the wall-clock millisecond and increments per
    packet, so re-running the tool against the same agent is never
    dropped as a stale sequence (the agent ignores seq <= last seen).

Values are passed through verbatim: the agent clamps out-of-range values
to the .duckshow limits and drops malformed packets (unknown skill /
sound tag, non-numeric fields) with a debug log. There are no ACKs on
this stream, so this tool cannot report either.

Single file, stdlib only (see CLAUDE.md), no dependency on the rest of
this repo -- `load_frames` / `PuppetStreamer` are importable for tests.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("puppet")

PROTOCOL_VERSION = 1
DEFAULT_HZ = 50.0
MAX_HZ = 50.0  # docs/swarmlink-protocol.md #6: the puppet stream is "<= 50 Hz"
# Exit this long after the last packet: one deadman period (250 ms) plus
# slack, so the duck is already zeroed when the prompt comes back.
TAIL_S = 0.3
CONTINUOUS_CHANNELS = ("move", "head", "pose", "mouth")
MAX_DATAGRAM_BYTES = 1200


class ScriptError(ValueError):
    """frames.json is not a usable frame list."""


# --------------------------------------------------------------------------
# Script loading / validation
# --------------------------------------------------------------------------


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_frames(frames: Any) -> list[dict[str, Any]]:
    """Shape-check a decoded script. Raises ScriptError with the offending
    frame index in the message. Unknown keys are ignored (kept as-is on the
    frame, not sent)."""
    if not isinstance(frames, list) or not frames:
        raise ScriptError("script must be a non-empty JSON list of frames")
    prev_t: Optional[float] = None
    for i, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise ScriptError(f"frame {i}: not an object")
        t = frame.get("t")
        if not _is_number(t) or t < 0:
            raise ScriptError(f"frame {i}: t must be a finite number >= 0, got {t!r}")
        if prev_t is not None and t < prev_t:
            raise ScriptError(f"frame {i}: t={t} is before the previous frame's t={prev_t} (frames must be sorted)")
        prev_t = float(t)
        for ch in CONTINUOUS_CHANNELS:
            if ch in frame and not isinstance(frame[ch], dict):
                raise ScriptError(f"frame {i}: {ch} must be an object, got {frame[ch]!r}")
        for key in ("do", "sound"):
            if key in frame and not isinstance(frame[key], str):
                raise ScriptError(f"frame {i}: {key} must be a string, got {frame[key]!r}")
    return frames


def load_frames(path: Path) -> list[dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ScriptError(f"cannot read script {path}: {exc}") from exc
    return validate_frames(data)


# --------------------------------------------------------------------------
# Streamer: pure scheduling over injectable send/clock/sleep (testable)
# --------------------------------------------------------------------------


def default_seq0() -> int:
    return time.time_ns() // 1_000_000


class PuppetStreamer:
    """Turns a validated frame list into a timed sequence of puppet
    datagrams. `send(payload_dict)` is called once per packet; `clock()`
    is a monotonic seconds source and `sleep(s)` the wait primitive --
    both injectable so tests can run the schedule instantly.
    """

    def __init__(
        self,
        frames: list[dict[str, Any]],
        send: Callable[[dict[str, Any]], None],
        hz: float = DEFAULT_HZ,
        hold_seconds: float = 0.0,
        seq0: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        master_time: Callable[[], int] = time.monotonic_ns,
        tail_s: float = TAIL_S,
    ) -> None:
        if not (0.0 < hz <= MAX_HZ):
            raise ValueError(f"hz must be in (0, {MAX_HZ}], got {hz}")
        if hold_seconds < 0 or not math.isfinite(hold_seconds):
            raise ValueError(f"hold_seconds must be a finite number >= 0, got {hold_seconds}")
        self.frames = frames
        self.send = send
        self.period_s = 1.0 / hz
        self.hold_seconds = float(hold_seconds)
        self.seq = seq0 if seq0 is not None else default_seq0()
        self.clock = clock
        self.sleep = sleep
        self.master_time = master_time
        self.tail_s = tail_s
        self.packets_sent = 0

    def _packet(self, frame: dict[str, Any], do: Optional[str], sound: Optional[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": "puppet",
            "seq": self.seq,
            "master_time": self.master_time(),
        }
        for ch in CONTINUOUS_CHANNELS:
            if ch in frame:
                payload[ch] = frame[ch]
        if do is not None:
            payload["do"] = do
        if sound is not None:
            payload["sound"] = sound
        self.seq += 1
        return payload

    def run(self) -> int:
        """Stream to completion; returns the number of packets sent."""
        frames = self.frames
        last_index = len(frames) - 1
        t_end = float(frames[-1]["t"]) + self.hold_seconds
        start = self.clock()
        current = -1  # index of the frame whose values are being held
        pending_do: list[str] = []
        pending_sound: list[str] = []
        sent_last = False
        tick = 0
        while True:
            now = self.clock() - start
            while current < last_index and float(frames[current + 1]["t"]) <= now:
                current += 1
                frame = frames[current]
                if "do" in frame:
                    pending_do.append(frame["do"])
                if "sound" in frame:
                    pending_sound.append(frame["sound"])
                logger.info("t=%.3f frame %d/%d: %s", now, current + 1, len(frames), _describe(frame))
            if current >= 0:
                do = pending_do.pop(0) if pending_do else None
                sound = pending_sound.pop(0) if pending_sound else None
                self.send(self._packet(frames[current], do, sound))
                self.packets_sent += 1
                if current == last_index:
                    sent_last = True
            if sent_last and now >= t_end and not pending_do and not pending_sound:
                break
            tick += 1
            self.sleep(max(0.0, start + tick * self.period_s - self.clock()))
        if self.tail_s > 0:
            self.sleep(self.tail_s)
        return self.packets_sent


def _describe(frame: dict[str, Any]) -> str:
    parts = [f"{ch}={json.dumps(frame[ch], separators=(',', ':'))}" for ch in CONTINUOUS_CHANNELS if ch in frame]
    for key in ("do", "sound"):
        if key in frame:
            parts.append(f"{key}={frame[key]}")
    return " ".join(parts) if parts else "(empty)"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_agent_arg(value: str) -> tuple[str, int]:
    if ":" not in value:
        raise argparse.ArgumentTypeError(f"--agent must be HOST:PORT, got {value!r}")
    host, _, port_s = value.rpartition(":")
    try:
        port = int(port_s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--agent port must be an integer, got {port_s!r}")
    if not host or not (0 < port < 65536):
        raise argparse.ArgumentTypeError(f"--agent must be HOST:PORT with a valid port, got {value!r}")
    return host, port


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="puppet.py",
        description="Stream scripted puppet frames (docs/swarmlink-protocol.md #6) to one duck-agent.",
    )
    p.add_argument("--agent", required=True, type=parse_agent_arg, metavar="HOST:PORT", help="The duck-agent's UDP address, e.g. 127.0.0.1:47801.")
    p.add_argument("--script", required=True, type=Path, metavar="FRAMES_JSON", help="JSON list of timed frames (see module docstring).")
    p.add_argument("--hz", type=float, default=DEFAULT_HZ, help=f"Packet rate (default {DEFAULT_HZ:g}, max {MAX_HZ:g}).")
    p.add_argument(
        "--hold-seconds",
        type=float,
        default=0.0,
        metavar="N",
        help="Keep repeating the last frame for N seconds past its t (default 0: sent once).",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Log every frame change.")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="puppet %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        frames = load_frames(args.script)
    except ScriptError as exc:
        print(f"puppet: {exc}", file=sys.stderr)
        return 2
    if not (0.0 < args.hz <= MAX_HZ):
        print(f"puppet: --hz must be in (0, {MAX_HZ:g}], got {args.hz}", file=sys.stderr)
        return 2
    if args.hold_seconds < 0 or not math.isfinite(args.hold_seconds):
        print(f"puppet: --hold-seconds must be a finite number >= 0, got {args.hold_seconds}", file=sys.stderr)
        return 2

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    addr = args.agent

    def send(payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_DATAGRAM_BYTES:
            logger.warning("packet seq=%s is %d bytes (> %d): frame too large", payload.get("seq"), len(data), MAX_DATAGRAM_BYTES)
        try:
            sock.sendto(data, addr)
        except OSError as exc:
            logger.warning("send to %s:%d failed: %s", addr[0], addr[1], exc)

    streamer = PuppetStreamer(frames, send, hz=args.hz, hold_seconds=args.hold_seconds)
    t0 = time.monotonic()
    try:
        sent = streamer.run()
    except KeyboardInterrupt:
        # Going quiet is the safe exit: the agent's deadman zeroes locomotion
        # (head/pose/mouth are held by robotd at their last value -- send a
        # neutral frame first if that matters; docs/authoring.md #1).
        print("puppet: interrupted; stream stopped (agent deadman zeroes locomotion)", file=sys.stderr)
        return 130
    finally:
        sock.close()
    logger.info("sent %d packets over %.2f s to %s:%d", sent, time.monotonic() - t0, addr[0], addr[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
