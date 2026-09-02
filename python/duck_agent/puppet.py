"""Puppet stream -- docs/swarmlink-protocol.md section 6, docs/authoring.md
section 1.

Two pieces, both free of any robotd / FSM knowledge so they can be unit
tested in isolation and so `agent.py` stays the single place that decides
*what* a fresh value does (forward it in IDLE/LOADED, nudge/override the
timeline while PLAYING):

  * `parse_puppet_packet()` turns one decoded ``{"type": "puppet", ...}``
    datagram into a `PuppetPacket`: every carried channel is validated
    against the .duckshow limits (docs/duckshow-format.md, via
    `duckshow.limits`) -- out-of-range values are **clamped**, anything
    malformed (non-numeric, non-finite, wrong shape, unknown skill /
    sound tag, missing or non-integer `seq`) raises `PuppetPacketError`
    and the caller drops the whole packet.

  * `PuppetChannel` is the receive-side bookkeeping: sequence-number
    de-duplication, the per-channel last value with its arrival time, the
    250 ms deadman, the once-per-`seq` `do`/`sound` queue, and the
    panic/stop mute latch.

Freshness is tracked **per channel**: "a packet carries only what the
sender wants to assert this tick", so a `head` asserted by packet N stays
in force for 250 ms after N even if packets N+1.. carry only `move`; a
sender releases a channel simply by no longer asserting it. The stream
as a whole counts as fresh (telemetry ``"puppet": true``) for 250 ms
after the last accepted packet, whatever it carried.

Mute latch (`mute()`): panic and stop must win over a sender that keeps
streaming through them, otherwise a held gamepad stick would re-drive
the duck 20 ms after a panic. After `mute()`, packets are dropped until
the stream has been *quiet* for one deadman period -- the sender has to
stop (or restart) to regain control. `mute()` also bumps `epoch`: an
action already taken out of the queue but not yet delivered to robotd
is stamped with the epoch it was taken under, and the firer drops it if
the epoch has moved (a `do` drained one tick before a panic must never
land after the panic's robot.stop).

Sequence numbers: packets with ``seq`` <= the last accepted one are
dropped (reordering / duplicates). After `SEQ_RESET_S` of silence the
tracking resets so a restarted sender that starts counting from 1 again
is not locked out for the rest of the day.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any, Optional

from duckshow.limits import DEFAULT_LIMITS, SKILLS, SOUND_TAGS, Limits

# docs/swarmlink-protocol.md #6: "A packet is fresh for 250 ms (the deadman)".
PUPPET_FRESH_S = 0.25
PUPPET_FRESH_NS = int(PUPPET_FRESH_S * 1e9)

# After this much silence the seq tracking resets (see module docstring).
SEQ_RESET_S = 2.0
SEQ_RESET_NS = int(SEQ_RESET_S * 1e9)

MOVE_FIELDS = ("vx", "vy", "vyaw")
HEAD_FIELDS = ("neck_pitch", "head_pitch", "head_yaw", "head_roll")
POSE_FIELDS = ("z", "roll", "pitch")


class PuppetPacketError(ValueError):
    """The datagram is not a well-formed puppet packet; drop it."""


@dataclass(frozen=True)
class PuppetPacket:
    seq: int
    move: Optional[dict[str, float]] = None
    head: Optional[dict[str, float]] = None
    pose: Optional[dict[str, Any]] = None  # z/roll/pitch floats + active bool
    mouth: Optional[dict[str, float]] = None
    do: Optional[str] = None
    sound: Optional[str] = None

    def carries_anything(self) -> bool:
        return any(v is not None for v in (self.move, self.head, self.pose, self.mouth, self.do, self.sound))


# -- parsing / clamping ----------------------------------------------------


def clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else (hi if value > hi else value)


def _number(obj: dict[str, Any], key: str, channel: str) -> float:
    """A finite JSON number (never a bool); missing fields default to 0.0
    like .duckshow keyframes do (python/duckshow/loader.py).
    """
    if key not in obj:
        return 0.0
    v = obj[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise PuppetPacketError(f"{channel}.{key} is not a number: {v!r}")
    try:
        v = float(v)
    except OverflowError as exc:
        # A JSON integer literal beyond float range parses to a Python int
        # that float() cannot represent; that is "malformed", not a crash.
        raise PuppetPacketError(f"{channel}.{key} is out of float range") from exc
    if not math.isfinite(v):
        raise PuppetPacketError(f"{channel}.{key} is not finite: {v!r}")
    return v


def _channel_dict(msg: dict[str, Any], channel: str) -> Optional[dict[str, Any]]:
    raw = msg.get(channel)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PuppetPacketError(f"{channel} is not an object: {raw!r}")
    return raw


def clamp_move(move: dict[str, float], limits: Limits = DEFAULT_LIMITS) -> dict[str, float]:
    return {
        "vx": clamp(move["vx"], -limits.max_abs_vx, limits.max_abs_vx),
        "vy": clamp(move["vy"], -limits.max_abs_vy, limits.max_abs_vy),
        "vyaw": clamp(move["vyaw"], -limits.max_abs_vyaw, limits.max_abs_vyaw),
    }


def nudge_move(timeline: dict[str, float], puppet: dict[str, float], limits: Limits = DEFAULT_LIMITS) -> dict[str, float]:
    """docs/authoring.md #1: puppet `move` is *added* to the timeline's
    locomotion (vector sum), clamped to the validation limits.
    """
    return clamp_move({k: timeline[k] + puppet[k] for k in MOVE_FIELDS}, limits)


def parse_puppet_packet(msg: dict[str, Any], limits: Limits = DEFAULT_LIMITS) -> PuppetPacket:
    """Validate + clamp one decoded puppet datagram. Raises
    PuppetPacketError for anything malformed (the caller drops it).
    """
    if not isinstance(msg, dict):
        raise PuppetPacketError("packet is not an object")
    seq = msg.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise PuppetPacketError(f"seq missing or not an integer: {seq!r}")

    move = None
    raw = _channel_dict(msg, "move")
    if raw is not None:
        move = clamp_move({k: _number(raw, k, "move") for k in MOVE_FIELDS}, limits)

    head = None
    raw = _channel_dict(msg, "head")
    if raw is not None:
        lim = limits.max_abs_head_angle
        head = {k: clamp(_number(raw, k, "head"), -lim, lim) for k in HEAD_FIELDS}

    pose = None
    raw = _channel_dict(msg, "pose")
    if raw is not None:
        active = raw.get("active", False)
        if not isinstance(active, bool):
            raise PuppetPacketError(f"pose.active is not a boolean: {active!r}")
        pose = {
            "z": clamp(_number(raw, "z", "pose"), -limits.max_abs_pose_z, limits.max_abs_pose_z),
            "roll": clamp(_number(raw, "roll", "pose"), -limits.max_abs_pose_roll, limits.max_abs_pose_roll),
            "pitch": clamp(_number(raw, "pitch", "pose"), -limits.max_abs_pose_pitch, limits.max_abs_pose_pitch),
            "active": active,
        }

    mouth = None
    raw = _channel_dict(msg, "mouth")
    if raw is not None:
        mouth = {"open": clamp(_number(raw, "open", "mouth"), limits.min_mouth_open, limits.max_mouth_open)}

    do = msg.get("do")
    if do is not None:
        if not isinstance(do, str) or do not in SKILLS:
            raise PuppetPacketError(f"do={do!r} is not a recognized skill")

    sound = msg.get("sound")
    if sound is not None:
        if not isinstance(sound, str) or sound not in SOUND_TAGS:
            raise PuppetPacketError(f"sound={sound!r} is not a recognized sound tag")

    return PuppetPacket(seq=seq, move=move, head=head, pose=pose, mouth=mouth, do=do, sound=sound)


# -- receive-side state ------------------------------------------------------


@dataclass
class PuppetValues:
    """The channels that are fresh *now* (None = not asserted / stale)."""

    move: Optional[dict[str, float]] = None
    head: Optional[dict[str, float]] = None
    pose: Optional[dict[str, Any]] = None
    mouth: Optional[dict[str, float]] = None


class PuppetChannel:
    """Thread-safe last-value store with per-channel deadman, seq dedup,
    once-per-seq action queue, and the panic/stop mute latch. All times
    are agent time.monotonic_ns() values supplied by the caller.
    """

    def __init__(self, fresh_ns: int = PUPPET_FRESH_NS, seq_reset_ns: int = SEQ_RESET_NS) -> None:
        self._fresh_ns = fresh_ns
        self._seq_reset_ns = seq_reset_ns
        self._lock = threading.Lock()
        self._last_seq: Optional[int] = None
        self._last_seen_ns: Optional[int] = None  # any well-formed packet offered
        self._last_accepted_ns: Optional[int] = None
        self._move: Optional[tuple[dict[str, float], int]] = None
        self._head: Optional[tuple[dict[str, float], int]] = None
        self._pose: Optional[tuple[dict[str, Any], int]] = None
        self._mouth: Optional[tuple[dict[str, float], int]] = None
        self._actions: list[tuple[str, str]] = []
        self._muted = False
        self._epoch = 0

    # -- feeding ------------------------------------------------------------

    def offer(self, packet: PuppetPacket, now_ns: int) -> bool:
        """Accept (True) or drop (False) one parsed packet."""
        with self._lock:
            prev_seen = self._last_seen_ns
            self._last_seen_ns = now_ns

            if prev_seen is not None and now_ns - prev_seen >= self._seq_reset_ns:
                self._last_seq = None  # a restarted sender after a long silence

            if self._muted:
                if prev_seen is not None and now_ns - prev_seen < self._fresh_ns:
                    return False  # still streaming through the panic/stop: stay muted
                self._muted = False

            if self._last_seq is not None and packet.seq <= self._last_seq:
                return False
            self._last_seq = packet.seq
            self._last_accepted_ns = now_ns

            if packet.move is not None:
                self._move = (packet.move, now_ns)
            if packet.head is not None:
                self._head = (packet.head, now_ns)
            if packet.pose is not None:
                self._pose = (packet.pose, now_ns)
            if packet.mouth is not None:
                self._mouth = (packet.mouth, now_ns)
            if packet.do is not None:
                self._actions.append(("do", packet.do))
            if packet.sound is not None:
                self._actions.append(("sound", packet.sound))
            return True

    def mute(self) -> None:
        """Panic/stop: drop everything held and ignore the stream until it
        has been quiet for one deadman period (see module docstring).
        """
        with self._lock:
            self._muted = True
            self._move = self._head = self._pose = self._mouth = None
            self._actions = []
            self._last_accepted_ns = None
            self._epoch += 1

    # -- queries --------------------------------------------------------

    def _fresh(self, entry, now_ns: int):
        if entry is None:
            return None
        value, rx_ns = entry
        return value if now_ns - rx_ns < self._fresh_ns else None

    def values(self, now_ns: int) -> PuppetValues:
        with self._lock:
            return PuppetValues(
                move=self._fresh(self._move, now_ns),
                head=self._fresh(self._head, now_ns),
                pose=self._fresh(self._pose, now_ns),
                mouth=self._fresh(self._mouth, now_ns),
            )

    def is_fresh(self, now_ns: int) -> bool:
        """Telemetry `"puppet"`: any accepted packet within the deadman."""
        with self._lock:
            return self._last_accepted_ns is not None and now_ns - self._last_accepted_ns < self._fresh_ns

    def take_actions(self) -> list[tuple[str, str]]:
        """Hand over (and clear) the queued do/sound actions, in arrival order."""
        with self._lock:
            actions, self._actions = self._actions, []
            return actions

    @property
    def epoch(self) -> int:
        """Bumped by every `mute()`; see the module docstring."""
        with self._lock:
            return self._epoch

    @property
    def last_seq(self) -> Optional[int]:
        with self._lock:
            return self._last_seq
