"""The on-duck agent state machine -- docs/swarmlink-protocol.md section 5.

    IDLE --load--> LOADED --play--> ARMED --(start time)--> PLAYING --(end/stop)--> LOADED
      ^                                                        |
      +----------------------- panic (from any state) <--------+

Two extra situations layered on top of that diagram, per the protocol doc
and the project brief:

  * PLAYING with sync lost keeps performing from the local copy; the
    *telemetry* `state` field reports "degraded" (see `_telemetry_state`)
    without touching the internal FSM value -- the duck is still,
    mechanically, PLAYING.
  * Any robotd problem (a discrete request failing, or the robotd link
    dropping while ARMED/PLAYING) moves the FSM itself to "fault" via
    `_enter_fault`, which also zeroes locomotion and sends `robot.stop`
    (best-effort; if the link is down the stop is *owed* and re-sent the
    moment robotd is back). From "fault" only `load` and `panic` make
    progress; `play`/`seek`/`stop` are NACKed until a fresh `load`
    proves the duck is healthy again. While robotd is disconnected for
    *any* reason, telemetry also always reports "fault" (even from
    IDLE/LOADED) so the master's preflight view is never lying about
    whether this duck can be trusted.

Show-night rules this module enforces on every exit from PLAYING (end of
show, `stop`, a fresh `load`/`play`, fault): zero locomotion + `robot.stop`
and release any held sounds, because robotd is last-value-wins and the
local socket has no watchdog -- a duck that merely stops *sending*
intents keeps walking at its last velocity.

Scheduling: `play`/`seek` keep the *master-time* instant and re-derive
the local start every 50 Hz tick from the current clock offset (the
500 ms ARMED-phase time sync exists precisely to refine that), and the
show clock is anchored at the scheduled instant, never at the tick that
happened to notice it. A `seek` received while PLAYING is a true clock
jump: the duck keeps performing the old position until the seek instant
and re-bases then (no ARMED detour, no coasting with a stale velocity).

`panic` resets the FSM synchronously (so the tick loop stops emitting
immediately), ACKs at once, and performs its `robot.stop` / neutral pose
from a dedicated thread -- its ACK must never wait on a robotd reply.

Puppet channel (docs/swarmlink-protocol.md #6, docs/authoring.md #1):
`"type": "puppet"` datagrams on the same UDP socket feed a
`puppet.PuppetChannel` (seq dedup, clamping, 250 ms per-channel deadman).
The tick loop is the only consumer:

  * IDLE/LOADED ("puppet mode"): fresh move/head/pose/mouth are forwarded
    as the corresponding robot.* notification every tick, last value
    wins. When the move channel goes stale (or the FSM leaves a
    puppet-eligible state under a live stream, e.g. `play` -> ARMED),
    locomotion is zeroed exactly once and forwarding stops.
  * PLAYING ("nudge layer"): puppet move is *added* to the sampled
    timeline locomotion (clamped); puppet head/pose/mouth *override* the
    timeline's; stale -> the timeline resumes. A role without a
    locomotion track is nudged from zero and zeroed once on staleness.
  * ARMED and FAULT ignore puppet values; queued do/sound are discarded.
  * do/sound fire once per accepted seq through the same request path
    timeline events use, but from a dedicated puppet-action thread fed by
    a short bounded queue (never under _playback_lock, never on the
    receive thread, never on the tick thread): a slow or hung robot.do
    must delay neither a panic cmd queued behind a puppet packet nor the
    tick loop itself -- the 250 ms deadman and the timeline run on that
    thread, and a sender that violates #6 (a `do` in every packet) must
    not be able to stretch the tick period. Actions that do not fit the
    queue are dropped with a warning (robotd would answer BUSY anyway).
    A JSON-RPC error reply (e.g. BUSY) is logged, not faulted: an
    operator's extra kick must never take the duck out of a number; a
    timeout/disconnect faults like an event.
  * panic and stop mute the channel until the stream has been quiet for
    one deadman period, so a sender that keeps streaming cannot re-drive
    the duck 20 ms after a panic. load leaves it alone. Muting also bumps
    the channel's epoch; an action taken off the queue under an older
    epoch is never delivered, so a `do` drained just before a panic/stop
    cannot land after their robot.stop (the FSM lands in IDLE/LOADED,
    which are puppet-eligible states, so a state check alone cannot tell
    "before the panic" from "after it").
  * The master address is never learned from puppet packets: the puppet
    sender (tools/puppet.py, `swarmctl record`) may be a different
    process than the master, and telemetry/time sync must keep going to
    the master.

Shutdown (`stop()`, reached from SIGINT/SIGTERM in __main__): if the
duck is in motion -- ARMED/PLAYING, or a puppet stream is driving it --
the same zero move + `robot.stop` + neutral head/pose as any other exit
from motion goes out before the robotd link is closed. Nothing else can
run the deadman or accept a panic once the agent process is gone.

This module owns four background threads (UDP receive, time sync,
telemetry, puppet actions) plus the 50 Hz playback tick thread;
`duckshow.Sampler` does the actual curve math. The UDP receive loop
never lets a handler exception escape: a datagram that trips a bug is
logged and dropped, because that thread is the only reader of cmd
datagrams and a dead reader would leave a duck that ignores panic while
its telemetry still looks healthy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import queue
import socket
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

import duckshow

from .clock import Clock, slew_towards
from .puppet import PuppetChannel, PuppetPacketError, PuppetValues, nudge_move, parse_puppet_packet
from .robotd_client import RobotdClient, RobotdDisconnected, RobotdError, RobotdTimeout

logger = logging.getLogger("duck_agent.agent")

PROTOCOL_VERSION = 1

TICK_HZ = 50.0
TICK_PERIOD_S = 1.0 / TICK_HZ

TIME_SYNC_PERIOD_S = 2.0
TIME_SYNC_PERIOD_ARMED_S = 0.5

TELEMETRY_PERIOD_IDLE_S = 1.0
TELEMETRY_PERIOD_PLAYING_S = 0.2  # 5 Hz

# The time-sync and telemetry loops sleep in short slices and re-evaluate
# their cadence from the *current* state, so the switch to the ARMED
# sync rate / the PLAYING telemetry rate lags a state change by at most
# one slice instead of a whole idle period.
LOOP_SLICE_S = 0.1

# docs/swarmlink-protocol.md #3, `play`: "already > 0.25s past on arrival"
# is the late-join threshold; ">= 2s late" is the missed-start cutoff.
LATE_JOIN_IMMEDIATE_S = 0.25
LATE_JOIN_MAX_S = 2.0

# Slew cap for the small `state`-broadcast drift correction, same order
# of magnitude as the clock-offset slew (docs/swarmlink-protocol.md #1).
SHOW_TIME_SLEW_S_PER_S = 0.005  # 5 ms/s

ROBOTD_REQUEST_TIMEOUT_S = 1.0

# Puppet do/sound waiting for the puppet-action thread (module docstring):
# one `do` plus one `sound` is what a single packet can carry; anything
# beyond that is a sender out of spec, and is dropped rather than queued.
PUPPET_ACTION_QUEUE_MAX = 2

_MAX_CMD_CACHE = 512

_NEUTRAL_HEAD = {"neck_pitch": 0.0, "head_pitch": 0.0, "head_yaw": 0.0, "head_roll": 0.0}
_NEUTRAL_POSE = {"z": 0.0, "roll": 0.0, "pitch": 0.0, "active": False}
_NEUTRAL_MOUTH = {"open": 0.0}
_ZERO_MOVE = {"vx": 0.0, "vy": 0.0, "vyaw": 0.0}

_ROBOTD_FAILURES = (RobotdError, RobotdDisconnected, RobotdTimeout)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class DuckAgent:
    def __init__(
        self,
        duck_id: str,
        robotd_target: str,
        shows_dir: Path,
        listen_port: int,
        master_host: Optional[str] = None,
        master_port: int = 47800,
    ):
        self.duck_id = duck_id
        self.shows_dir = Path(shows_dir)
        self.listen_port = listen_port

        self._configured_master_addr: Optional[tuple[str, int]] = (
            (master_host, master_port) if master_host else None
        )
        self.master_addr: Optional[tuple[str, int]] = self._configured_master_addr

        self.robotd = RobotdClient(robotd_target, on_state_change=self._on_robotd_state_change)

        # -- show / role state (mutated only under _playback_lock) --
        self.show: Optional[duckshow.Show] = None
        self.show_id: Optional[str] = None
        self.show_path: Optional[Path] = None
        self.role: Optional[str] = None
        self.sampler: Optional[duckshow.Sampler] = None
        self.policies_ok: bool = True
        self.current_mode: Optional[str] = None
        self.last_error: Optional[str] = None

        self.state: str = "idle"  # idle | loaded | armed | playing | fault

        # play/seek scheduling: the *master-time* instant is kept and the
        # local start re-derived every tick from the current clock offset.
        self._scheduled_at_master_ns: Optional[int] = None
        self._scheduled_from_show_time: float = 0.0
        # seek received while PLAYING: (at_master_ns, target show_time),
        # applied by the tick loop once the instant arrives.
        self._pending_seek: Optional[tuple[int, float]] = None
        # Operator pause (docs/swarmlink-protocol.md "Pause and resume").
        # _paused_show_time freezes the clock; _pending_pause/_pending_resume
        # are parked instants the tick loop applies, exactly like _pending_seek
        # -- never validated on arrival, because both reference masters break
        # their retry loop on any ACK including a NACK, so one early NACK would
        # strand one duck for the rest of the show.
        self._paused_show_time: Optional[float] = None
        self._pending_pause: Optional[int] = None
        self._pending_resume: Optional[int] = None
        # Safe-to-handle: robot.relax has been sent and no play has
        # re-enabled since. Surfaced in telemetry so an operator can see
        # which ducks are safe to pick up (docs/swarmlink-protocol.md
        # "Relax: a state that is safe to pick up").
        self.relaxed = False
        # the running show clock
        self._play_epoch_local_ns: Optional[int] = None
        self._play_epoch_show_time: float = 0.0
        self._last_processed_show_time: float = 0.0
        self._show_time_correction_s: float = 0.0
        self._show_time_correction_target_s: float = 0.0

        self._playback_lock = threading.RLock()
        # tag -> absolute monotonic_ns deadline for sounds started with
        # hold=True. Per docs/duckshow-format.md ("Held sounds") this is
        # not a one-shot timer: `_tick_loop` re-sends `hold: true` for
        # every tag still in this dict on every tick (a notification per
        # tick, like robot.mouth -- upstream's own hold state decays
        # without it) and releases it exactly once, either when the
        # deadline passes or when _take_held_sounds_locked empties the
        # dict early (stop/panic/load/end-of-show).
        self._held_sounds: dict[str, int] = {}
        # A stop that could not be delivered (link down / request failed)
        # is re-sent as soon as robotd is reachable again.
        self._stop_owed: bool = False

        # Puppet channel (module docstring). `_puppet_move_applied` is
        # whether the last robot.move we emitted carried puppet influence,
        # so the tick that first sees the stream stale (or the FSM leave a
        # puppet-eligible state) can zero locomotion exactly once. Mutated
        # only under _playback_lock; the channel itself has its own lock.
        self._puppet = PuppetChannel()
        self._puppet_move_applied: bool = False
        # (kind, name, channel epoch) handed from the tick loop to the
        # puppet-action thread.
        self._puppet_actions: "queue.Queue[tuple[str, str, int]]" = queue.Queue(maxsize=PUPPET_ACTION_QUEUE_MAX)

        self.clock = Clock()

        self._cmd_lock = threading.Lock()
        self._cmd_results: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.bind(("0.0.0.0", listen_port))
        self.udp_sock.settimeout(0.5)
        self.bound_port = self.udp_sock.getsockname()[1]

        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

        self._telemetry_seq = 0

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        self.robotd.start()
        self._threads = [
            threading.Thread(target=self._recv_loop, daemon=True, name=f"{self.duck_id}-recv"),
            threading.Thread(target=self._time_sync_loop, daemon=True, name=f"{self.duck_id}-timesync"),
            threading.Thread(target=self._telemetry_loop, daemon=True, name=f"{self.duck_id}-telemetry"),
            threading.Thread(target=self._tick_loop, daemon=True, name=f"{self.duck_id}-tick"),
            threading.Thread(target=self._puppet_action_loop, daemon=True, name=f"{self.duck_id}-puppet-actions"),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        """Shut the agent down; idempotent. See the module docstring on
        why a duck in motion gets its stop sequence first.
        """
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        with self._playback_lock:
            self._cancel_scheduled()
            held = self._take_held_sounds_locked()
        for t in self._threads:
            t.join(timeout=2)
        # The emitters are gone now; whatever robotd is holding is the last
        # thing we sent it, and it is last-value-wins with no local watchdog.
        with self._playback_lock:
            in_motion = (
                self.state in ("armed", "playing")
                or self._puppet_move_applied
                or self._puppet.is_fresh(time.monotonic_ns())
            )
        if in_motion:
            err = self._send_stop_sequence()
            if err is not None:
                logger.warning("%s: robot.stop on shutdown failed: %s", self.duck_id, err)
            self._notify_neutral()
        self._release_held_sounds(held)
        try:
            self.udp_sock.close()
        except OSError:
            pass
        self.robotd.stop()

    def __enter__(self) -> "DuckAgent":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- telemetry-facing state -------------------------------------------

    def _telemetry_state(self) -> str:
        if not self.robotd.connected:
            return "fault"
        if self.state == "playing" and self.clock.degraded():
            return "degraded"
        return self.state

    def _current_show_time(self, now_ns: Optional[int] = None) -> float:
        now_ns = now_ns if now_ns is not None else time.monotonic_ns()
        if self._paused_show_time is not None:
            return self._paused_show_time
        if self._play_epoch_local_ns is None:
            # ARMED: `_arm` parks this at the scheduled from_show_time.
            return self._play_epoch_show_time
        raw = self._play_epoch_show_time + (now_ns - self._play_epoch_local_ns) / 1e9
        return raw + self._show_time_correction_s

    def build_telemetry(self, now_ns: Optional[int] = None) -> dict[str, Any]:
        now_ns = now_ns if now_ns is not None else time.monotonic_ns()
        show_time = 0.0
        if self.state in ("armed", "playing", "paused"):
            show_time = self._current_show_time(now_ns)
        self._telemetry_seq += 1
        return {
            "v": PROTOCOL_VERSION,
            "type": "telemetry",
            "duck": self.duck_id,
            "seq": self._telemetry_seq,
            "state": self._telemetry_state(),
            "show": self.show_id,
            "show_time": show_time,
            "clock_offset_ms": self.clock.telemetry_offset_ms(),
            "clock_rtt_ms": self.clock.telemetry_rtt_ms(),
            "policies_ok": self.policies_ok,
            # Which ducks are safe to pick up right now. An operator
            # repositioning a cast between chapters needs to see this per
            # duck, not assume it.
            "relaxed": self.relaxed,
            "battery_pct": None,
            "rssi_dbm": None,
            "last_error": self.last_error,
            "puppet": self._puppet.is_fresh(now_ns),
        }

    # -- robotd link ---------------------------------------------------

    def _on_robotd_state_change(self, connected: bool) -> None:
        if connected:
            logger.info("%s: robotd reconnected", self.duck_id)
            with self._playback_lock:
                owed = self._stop_owed or self.state == "fault"
            if owed:
                # The link came back after a fault/failed stop: robotd may
                # still be executing the last velocity we ever sent it.
                self._send_stop_sequence()
                self._notify_neutral()
            with self._playback_lock:
                # A fresh daemon's torque state is not ours to assert. Claiming
                # `relaxed: true` on an operator's screen for a duck that is
                # actually standing is worse than admitting we no longer know,
                # and the play path re-enables unconditionally when unsure.
                self.relaxed = False
            return
        if self._stop_event.is_set():
            return  # our own shutdown closing the link, not a robotd failure
        logger.warning("%s: robotd disconnected", self.duck_id)
        self._enter_fault("robotd disconnected")

    # -- robotd helpers ------------------------------------------------------

    def _send_stop_sequence(self, timeout: float = ROBOTD_REQUEST_TIMEOUT_S) -> Optional[str]:
        """Zero locomotion, then `robot.stop`. Returns None on success or
        the error text on failure; a failed stop is remembered as owed so
        `_on_robotd_state_change(True)` re-sends it.
        """
        with self._playback_lock:
            # Whatever happens below, the puppet's last velocity is no
            # longer what robotd holds (zeroed here, or owed on reconnect).
            self._puppet_move_applied = False
        try:
            self.robotd.notify("robot.move", dict(_ZERO_MOVE))
            self.robotd.request("robot.stop", {}, timeout=timeout)
        except _ROBOTD_FAILURES as exc:
            with self._playback_lock:
                self._stop_owed = True
            return str(exc)
        with self._playback_lock:
            self._stop_owed = False
        return None

    def _notify_neutral(self) -> None:
        try:
            self.robotd.notify("robot.head", dict(_NEUTRAL_HEAD))
            self.robotd.notify("robot.pose", dict(_NEUTRAL_POSE))
            # The mouth was previously closed by nothing at all -- not stop,
            # not end-of-show, not even panic. robot.mouth was only ever sent
            # from the two tick paths, so a show ending mid-mouthOpen left the
            # bill open indefinitely, on stage, until something else happened
            # to move it.
            self.robotd.notify("robot.mouth", dict(_NEUTRAL_MOUTH))
        except RobotdDisconnected as exc:
            logger.warning("%s: neutral head/pose/mouth notify failed: %s", self.duck_id, exc)

    def _fault_locked(self, reason: str) -> None:
        """FSM side of entering FAULT; caller holds _playback_lock."""
        self._cancel_scheduled()
        self._play_epoch_local_ns = None
        self.state = "fault"
        self.last_error = reason

    def _enter_fault(self, reason: str) -> None:
        """docs/swarmlink-protocol.md #5: "Any robotd error -> FAULT:
        robot.stop, report last_error". Only ARMED/PLAYING can fault (a
        duck that is idle/loaded has nothing in motion to protect); the
        stop is best-effort and owed if robotd is unreachable.
        """
        with self._playback_lock:
            if self.state not in ("armed", "playing"):
                return
            held = self._take_held_sounds_locked()
            self._fault_locked(reason)
        logger.warning("%s: entering fault: %s", self.duck_id, reason)
        if not self.robotd.connected:
            with self._playback_lock:
                self._stop_owed = True
            return
        self._send_stop_sequence()
        self._notify_neutral()
        self._release_held_sounds(held)

    # -- UDP receive loop -------------------------------------------------

    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                data, addr = self.udp_sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")
            try:
                if mtype == "time_resp":
                    self._on_time_resp(msg, addr)
                elif mtype == "state":
                    self._on_state_msg(msg, addr)
                elif mtype == "cmd":
                    self._handle_cmd_message(msg, addr)
                elif mtype == "puppet":
                    self._on_puppet_msg(msg)
                # unknown type: dropped silently (swarmlink-protocol.md #3)
            except Exception:  # noqa: BLE001 -- no datagram may kill the only cmd reader (module docstring)
                logger.exception("%s: dropping %r datagram from %s that raised", self.duck_id, mtype, addr)

    def _on_puppet_msg(self, msg: dict[str, Any]) -> None:
        """docs/swarmlink-protocol.md #6. Parse + clamp, then offer to the
        channel; the tick loop does the forwarding. Deliberately does
        *not* call _set_master_addr (module docstring).
        """
        try:
            packet = parse_puppet_packet(msg)
        except PuppetPacketError as exc:
            logger.debug("%s: dropping malformed puppet packet: %s", self.duck_id, exc)
            return
        with self._playback_lock:
            relaxed = self.relaxed
        if relaxed:
            # swarmlink-protocol.md, "Relax": driving a duck whose torque is
            # off commands motion that cannot happen, and the editor's ghost
            # would then show it somewhere the real duck is not. Drop rather
            # than buffer -- these are 30-60 Hz live packets, a backlog is
            # worse than a gap.
            logger.debug("%s: dropping puppet packet seq=%s (relaxed)", self.duck_id, packet.seq)
            return
        if not self._puppet.offer(packet, time.monotonic_ns()):
            logger.debug("%s: dropping puppet packet seq=%s (stale seq or muted)", self.duck_id, packet.seq)

    def _set_master_addr(self, addr: tuple[str, int]) -> None:
        """Learn/refresh the master's address from any inbound packet's
        source (swarmlink-protocol.md #4: "or configured"). The first
        time we learn it (or it changes), kick off a time_req right away
        instead of waiting for the next periodic tick -- there's no
        reason to sit on a stale/empty clock window when we could be
        syncing immediately.
        """
        changed = self.master_addr != addr
        self.master_addr = addr
        if changed:
            self._send_time_req()

    def _on_time_resp(self, msg: dict[str, Any], addr: tuple[str, int]) -> None:
        self._set_master_addr(addr)
        t0, t1, t2 = msg.get("t0"), msg.get("t1"), msg.get("t2")
        if t0 is None or t1 is None or t2 is None:
            return
        self.clock.record_exchange(int(t0), int(t1), int(t2))

    def _on_state_msg(self, msg: dict[str, Any], addr: tuple[str, int]) -> None:
        """Secondary drift correction (swarmlink-protocol.md #2): while
        PLAYING, compare our local show clock against the master's
        broadcast show_time (translated through the master_time it was
        stamped with) and slew out any difference. This never *steps*
        the show clock -- only `play`/`seek` commands do that -- it just
        nudges the rate so small drift doesn't accumulate.

        Only a master that is itself `playing` the same show carries a
        meaningful show_time (while it is armed for a play/seek the value
        is static), so anything else is ignored.
        """
        self._set_master_addr(addr)
        transport = msg.get("transport")
        if transport is not None and transport != "playing":
            return
        show_time = msg.get("show_time")
        master_time = msg.get("master_time")
        if show_time is None or master_time is None:
            return
        master_show = msg.get("show")
        now_ns = time.monotonic_ns()
        est_master_now = self.clock.estimated_master_time(now_ns)
        predicted_show_time = float(show_time) + (est_master_now - int(master_time)) / 1e9
        with self._playback_lock:
            if self.state != "playing" or self._pending_seek is not None:
                return
            if master_show is not None and self.show_id is not None and master_show != self.show_id:
                return
            local_show_time = self._current_show_time(now_ns) - self._show_time_correction_s
            self._show_time_correction_target_s = predicted_show_time - local_show_time

    # -- command handling --------------------------------------------------

    def _handle_cmd_message(self, msg: dict[str, Any], addr: tuple[str, int]) -> None:
        self._set_master_addr(addr)
        if self.clock.sample_count() == 0:
            # No sync yet (first contact, or the first time_resp was
            # lost): ask right away so a `play` that follows this `load`
            # finds an offset instead of being NACKed "no time sync yet".
            self._send_time_req()
        cmd_id = msg.get("cmd_id")
        cmd = msg.get("cmd")
        if not cmd_id or not cmd:
            return

        with self._cmd_lock:
            cached = self._cmd_results.get(cmd_id)
        if cached is not None:
            self._send_ack(cmd_id, cached["ok"], cached["error"], addr)
            return

        handlers = {
            "load": self._handle_load,
            "play": self._handle_play,
            "seek": self._handle_seek,
            "pause": self._handle_pause,
            "resume": self._handle_resume,
            "relax": self._handle_relax,
            "stop": self._handle_stop,
            "panic": self._handle_panic,
        }
        handler = handlers.get(cmd)
        if handler is None:
            logger.warning("%s: unknown cmd %r", self.duck_id, cmd)
            return

        try:
            ok, error = handler(msg)
        except Exception as exc:  # noqa: BLE001 -- a malformed field (e.g. a non-numeric time) is a NACK, not a crash
            logger.exception("%s: cmd %r raised", self.duck_id, cmd)
            ok, error = False, f"{cmd} rejected: {exc}"
        with self._cmd_lock:
            self._cmd_results[cmd_id] = {"ok": ok, "error": error}
            while len(self._cmd_results) > _MAX_CMD_CACHE:
                self._cmd_results.popitem(last=False)
        self._send_ack(cmd_id, ok, error, addr)

    def _send_ack(self, cmd_id: str, ok: bool, error: Optional[str], addr: tuple[str, int]) -> None:
        ack = {"v": PROTOCOL_VERSION, "type": "ack", "duck": self.duck_id, "cmd_id": cmd_id, "ok": ok, "error": error}
        try:
            self.udp_sock.sendto(json.dumps(ack).encode("utf-8"), addr)
        except OSError:
            pass

    # -- load ------------------------------------------------------------

    def _resolve_show_path(self, show_id: str) -> Optional[Path]:
        flat = self.shows_dir / f"{show_id}.duckshow.json"
        if flat.exists():
            return flat
        nested = self.shows_dir / show_id / f"{show_id}.duckshow.json"
        if nested.exists():
            return nested
        return None

    def _check_policies(self, show: duckshow.Show) -> tuple[bool, Optional[str]]:
        """v1 policy check (docs/duckshow-format.md "Custom .onnx
        policies"): every required policy's .onnx must already be
        present locally (pushed by SwarmLink pre-show) with a matching
        sha256. Missing file or hash mismatch -> NACK the load.
        """
        for p in show.requires.policies:
            candidate = Path(p.file)
            if not candidate.is_absolute():
                candidate = self.shows_dir / p.file
            if not candidate.exists():
                return False, f"required policy {p.name!r} file not found locally: {candidate}"
            if _sha256_file(candidate) != p.sha256:
                return False, f"required policy {p.name!r} sha256 mismatch"
        return True, None

    def _handle_load(self, fields: dict[str, Any]) -> tuple[bool, Optional[str]]:
        show_id = fields.get("show")
        role = fields.get("role")
        expected_sha256 = fields.get("sha256")
        if not show_id or not role:
            return False, "load command missing show or role"
        if not isinstance(expected_sha256, str) or not expected_sha256:
            # The hash check is what makes out-of-band show distribution
            # safe (swarmlink-protocol.md #3); a load without one cannot
            # prove every duck holds the same revision.
            return False, "load command missing sha256"

        path = self._resolve_show_path(show_id)
        if path is None:
            return False, f"show file not found for id {show_id!r}"

        actual_sha256 = _sha256_file(path)
        if actual_sha256 != expected_sha256:
            return False, f"sha256 mismatch for show {show_id!r}"

        try:
            show = duckshow.load_show(path)
        except duckshow.DuckShowFormatError as exc:
            return False, f"parse error: {exc}"

        if role not in show.role_names():
            return False, f"role {role!r} not in cast"

        issues = duckshow.validate(show)
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            extra = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
            return False, f"validation failed: {errors[0].message}{extra}"

        # meta.duration is the only end-of-show trigger (duckshow-format.md:
        # "playback ends here regardless of track contents"); without it a
        # show would hold its last locomotion keyframe forever.
        duration = show.meta.duration
        if duration is None or not duration > 0:
            return False, "meta.duration missing or not positive"

        policies_ok, policy_error = self._check_policies(show)
        if not policies_ok:
            with self._playback_lock:
                self.policies_ok = False
                self.last_error = policy_error
            return False, policy_error

        try:
            sampler = duckshow.Sampler(show, role)
            sampler.at(0.0)  # sample-check: make sure sampling doesn't blow up
        except Exception as exc:  # noqa: BLE001 -- any sampler failure NACKs the load
            return False, f"sample-check failed: {exc}"

        held: list[str] = []
        try:
            with self._playback_lock:
                self._cancel_scheduled()
                held = self._take_held_sounds_locked()
                if self.state in ("armed", "playing"):
                    # Leaving PLAYING by any route zeroes locomotion and
                    # sends robot.stop -- robotd would otherwise keep the
                    # last velocity of the show we are replacing.
                    err = self._send_stop_sequence()
                    if err is not None:
                        self._fault_locked(f"stop failed: {err}")
                        return False, f"load rejected: {self.last_error}"
                self.show = show
                self.show_id = show_id
                self.show_path = path
                self.role = role
                self.sampler = sampler
                self.policies_ok = True
                self.current_mode = None
                self._play_epoch_local_ns = None
                self._play_epoch_show_time = 0.0
                self._last_processed_show_time = 0.0
                self._show_time_correction_s = 0.0
                self._show_time_correction_target_s = 0.0
                self.state = "loaded"
                self.last_error = None
        finally:
            self._release_held_sounds(held)
        return True, None

    # -- play / seek scheduling --------------------------------------------

    def _cancel_scheduled(self) -> None:
        self._scheduled_at_master_ns = None
        self._pending_seek = None
        # A pause must never outlive the thing it was pausing: stop, panic and
        # load all come through here, and a duck left frozen after one of them
        # is a duck that cannot be recovered without a reload.
        self._paused_show_time = None
        self._pending_pause = None
        self._pending_resume = None

    def _arm(self, at_master_ns: int, from_show_time: float) -> None:
        self.state = "armed"
        self._scheduled_at_master_ns = at_master_ns
        self._scheduled_from_show_time = from_show_time
        self._pending_seek = None
        self._play_epoch_local_ns = None
        self._play_epoch_show_time = from_show_time  # telemetry show_time while ARMED
        self._show_time_correction_s = 0.0
        self._show_time_correction_target_s = 0.0

    def _start_playing_now(self, show_time: float, epoch_local_ns: int) -> None:
        """Anchor the show clock: show_time == `show_time` at local instant
        `epoch_local_ns` (the *scheduled* instant, which may already be a
        little in the past -- the first tick then lands at the correct
        elapsed show time instead of restarting the clock at 'now').
        """
        self._scheduled_at_master_ns = None
        self._pending_seek = None
        # A resume goes through here, and so does any fresh play/seek: in every
        # case the clock is being re-anchored, so a frozen position must not
        # survive it (the control-track review's "seek out of a hold strands
        # the duck" failure).
        self._paused_show_time = None
        self._pending_pause = None
        self._pending_resume = None
        self._play_epoch_local_ns = epoch_local_ns
        self._play_epoch_show_time = show_time
        # Open-left event window (sampler.events_between is (t0, t1]):
        # seed strictly below the start so an event exactly *at* the
        # start instant fires on the first tick (duckshow-format.md:
        # "at the first 50 Hz tick >= t"), while earlier ones stay skipped.
        self._last_processed_show_time = math.nextafter(show_time, -math.inf)
        self._show_time_correction_s = 0.0
        self._show_time_correction_target_s = 0.0
        self.state = "playing"
        self._apply_mode_for_show_time(show_time)

    def _begin_playback(self, show_time: float, local_start_ns: int, now_ns: int) -> None:
        """Start (or re-base, for seek) playback whose scheduled local
        instant is `local_start_ns`. Within the 0.25 s 'on time' window the
        clock is anchored at that instant; beyond it we join in progress at
        the correct point (events in the gap are skipped, never replayed).
        """
        late_s = (now_ns - local_start_ns) / 1e9
        if late_s <= LATE_JOIN_IMMEDIATE_S:
            self._start_playing_now(show_time, local_start_ns)
        else:
            self._start_playing_now(show_time + late_s, now_ns)

    def _apply_mode_for_show_time(self, show_time: float) -> None:
        if self.sampler is None:
            return
        mode = self.sampler.mode_at(show_time)
        if mode is None or mode == self.current_mode:
            return
        try:
            self.robotd.request("robot.setMode", {"mode": mode}, timeout=ROBOTD_REQUEST_TIMEOUT_S)
            self.current_mode = mode
        except _ROBOTD_FAILURES as exc:
            logger.warning("%s: setMode(%s) failed: %s", self.duck_id, mode, exc)
            self._enter_fault(f"setMode failed: {exc}")

    def _handle_play(self, fields: dict[str, Any]) -> tuple[bool, Optional[str]]:
        at_master_time = fields.get("at_master_time")
        if at_master_time is None:
            return False, "play command missing at_master_time"
        from_show_time = float(fields.get("from_show_time", 0.0))

        # Use the freshest sample rather than whatever the last tick applied.
        self.clock.update_applied_offset(playing=(self.state == "playing"))
        # A relaxed duck handed a play would try to perform limp. Re-torque
        # before arming, outside the lock (it is a robotd round trip).
        self._reenable_if_relaxed()

        held: list[str] = []
        try:
            with self._playback_lock:
                if self.state == "fault":
                    return False, "duck is in fault state; reload required"
                if not self.robotd.connected:
                    return False, "robotd not connected"
                if self.show_id is None or fields.get("show") != self.show_id:
                    return False, f"show {fields.get('show')!r} not loaded"
                if self.clock.sample_count() == 0:
                    # An offset of exactly 0.0 is not an estimate, it is the
                    # absence of one; scheduling against it would be a
                    # start at a random point of the master's uptime.
                    return False, "no time sync yet"

                now_ns = time.monotonic_ns()
                local_start_ns = self.clock.local_time_for_master(int(at_master_time))
                late_s = (now_ns - local_start_ns) / 1e9
                if late_s >= LATE_JOIN_MAX_S:
                    # >= 2s late: never improvise to catch up. Stay put
                    # (an ARMED schedule or a running performance is left
                    # untouched) and report missed_start.
                    if self.state != "playing":
                        self.last_error = "missed_start"
                    return False, "missed_start"

                self._cancel_scheduled()
                held = self._take_held_sounds_locked()
                if self.state in ("armed", "playing"):
                    err = self._send_stop_sequence()
                    if err is not None:
                        self._fault_locked(f"stop failed: {err}")
                        return False, f"play rejected: {self.last_error}"
                # Arm on the master-time instant; the tick loop re-derives
                # the local start from the live offset and starts exactly
                # at (or, within the grace windows, correctly after) it.
                self._arm(int(at_master_time), from_show_time)
                self.last_error = None
        finally:
            self._release_held_sounds(held)
        return True, None

    def _handle_seek(self, fields: dict[str, Any]) -> tuple[bool, Optional[str]]:
        show_time = fields.get("show_time")
        at_master_time = fields.get("at_master_time")
        if show_time is None or at_master_time is None:
            return False, "seek command missing show_time or at_master_time"

        self.clock.update_applied_offset(playing=(self.state == "playing"))

        with self._playback_lock:
            if self.state == "fault":
                return False, "duck is in fault state; reload required"
            if not self.robotd.connected:
                return False, "robotd not connected"
            if self.state not in ("armed", "playing", "paused"):
                return False, "cannot seek: not armed, playing or paused"
            if self.clock.sample_count() == 0:
                return False, "no time sync yet"
            if self.state == "armed":
                self._arm(int(at_master_time), float(show_time))
            else:
                # PLAYING or PAUSED both park it; _play_tick applies it at the
                # instant, and while paused it moves the frozen point rather
                # than starting this duck performing alone.
                # Keep performing until the seek instant, then jump
                # (_play_tick); the duck never coasts on a stale velocity.
                self._pending_seek = (int(at_master_time), float(show_time))
            self.last_error = None
        return True, None

    def _handle_pause(self, fields: dict[str, Any]) -> tuple[bool, Optional[str]]:
        at_master_time = fields.get("at_master_time")
        if at_master_time is None:
            return False, "pause command missing at_master_time"
        with self._playback_lock:
            if self.state == "fault":
                return False, "duck is in fault state; reload required"
            if self._paused_show_time is not None or self._pending_pause is not None:
                return True, None  # already pausing/paused: idempotent by state
            if self.state != "playing":
                return False, "cannot pause: not playing"
            self._pending_pause = int(at_master_time)
            self.last_error = None
        return True, None

    def _handle_resume(self, fields: dict[str, Any]) -> tuple[bool, Optional[str]]:
        at_master_time = fields.get("at_master_time")
        if at_master_time is None:
            return False, "resume command missing at_master_time"
        with self._playback_lock:
            if self.state == "fault":
                return False, "duck is in fault state; reload required"
            # Idempotent by STATE, not just by cmd_id: two distinct GO presses
            # are two cmd_ids, and re-anchoring on the second would move this
            # duck's epoch away from the rest of the cast.
            if self._paused_show_time is None and self._pending_pause is None:
                return True, None
            self._pending_resume = int(at_master_time)
            self.last_error = None
        return True, None

    def _handle_stop(self, fields: dict[str, Any]) -> tuple[bool, Optional[str]]:
        error: Optional[str] = None
        with self._playback_lock:
            if self.state == "fault":
                return False, "duck is in fault state; reload required"
            self._cancel_scheduled()
            held = self._take_held_sounds_locked()
            # stop wins over a live puppet stream (module docstring): mute
            # it, and if the puppet was driving locomotion in IDLE/LOADED
            # that motion is stopped exactly like a playing show's.
            self._puppet.mute()
            if self.state in ("armed", "playing", "paused") or self._puppet_move_applied:
                err = self._send_stop_sequence()
                if err is not None:
                    self._fault_locked(f"stop failed: {err}")
                    error = self.last_error
                elif self.state in ("armed", "playing", "paused"):
                    self.state = "loaded" if self.show is not None else "idle"
            self._play_epoch_local_ns = None
            self._play_epoch_show_time = 0.0
            needs_neutral = self.state in ("loaded", "idle")
        # Outside the lock: these are notifies, but _release_held_sounds below
        # is a blocking request and the same rule applies -- never hold the
        # lock panic needs across a robotd round trip.
        if needs_neutral:
            self._notify_neutral()
        self._release_held_sounds(held)
        if error is not None:
            return False, error
        return True, None

    def _handle_relax(self, fields: dict[str, Any]) -> tuple[bool, Optional[str]]:
        """`on: true` (default) makes this duck safe to pick up: neutral first,
        so it is not frozen mid-gesture in someone's hands, then robot.relax.
        `on: false` re-torques it.

        Refused while armed/playing/paused: going limp mid-number is not a
        thing an operator can mean, and the failure mode is a duck on the
        floor. Idempotent by state, like pause/resume.
        """
        want_relaxed = bool(fields.get("on", True))
        with self._playback_lock:
            if self.state == "fault":
                return False, "duck is in fault state; reload required"
            if self.state in ("armed", "playing", "paused"):
                return False, "cannot relax while armed, playing or paused: stop first"
            if not self.robotd.connected:
                return False, "robotd not connected"
            if self.relaxed == want_relaxed:
                return True, None
        if not want_relaxed:
            err = self._enable_torque()
            if err is not None:
                return False, err
            return True, None
        # Neutral BEFORE the torque goes away: afterwards nothing can move.
        self._notify_neutral()
        try:
            self.robotd.request("robot.relax", {}, timeout=ROBOTD_REQUEST_TIMEOUT_S)
        except _ROBOTD_FAILURES as exc:
            return False, f"robot.relax failed: {exc}"
        with self._playback_lock:
            self.relaxed = True
            self.last_error = None
        return True, None

    def _enable_torque(self) -> Optional[str]:
        """`robot.enable {on: true}` and clear the relaxed flag. Returns an
        error string on failure, and leaves the flag set: a duck whose
        re-torque failed is still limp and telemetry must keep saying so."""
        try:
            self.robotd.request("robot.enable", {"on": True, "toggle": False},
                                timeout=ROBOTD_REQUEST_TIMEOUT_S)
        except _ROBOTD_FAILURES as exc:
            return f"robot.enable failed: {exc}"
        with self._playback_lock:
            self.relaxed = False
            self.last_error = None
        return None

    def _reenable_if_relaxed(self) -> None:
        """Re-torque before motion. Called from the play path: a relaxed duck
        that was handed a `play` would otherwise try to perform limp."""
        with self._playback_lock:
            if not self.relaxed:
                return
        err = self._enable_torque()
        if err is not None:
            logger.warning("%s: re-torque before play failed: %s", self.duck_id, err)

    def _handle_panic(self, fields: dict[str, Any]) -> tuple[bool, Optional[str]]:
        # "Never NACKed" (swarmlink-protocol.md #3): always ACK true, even
        # if the robotd calls fail -- panic's job is to get the FSM back
        # to a known-safe IDLE, which it does unconditionally and
        # synchronously (the tick loop stops emitting the moment the
        # state flips). Every robotd write, the zero move included, runs
        # on its own thread so the ACK never waits on robotd at all -- not
        # on a reply, not on a hung one, not on a socket it stopped
        # draining.
        with self._playback_lock:
            self._cancel_scheduled()
            held = self._take_held_sounds_locked()
            self.state = "idle"
            self.show = None
            self.show_id = None
            self.show_path = None
            self.role = None
            self.sampler = None
            self.current_mode = None
            self._play_epoch_local_ns = None
            self._play_epoch_show_time = 0.0
            self.last_error = None
            # Panic wins over a live puppet stream too: mute it until it
            # goes quiet (module docstring). The panic thread's zero move
            # covers whatever puppet velocity robotd was holding; with the
            # FSM already reset and the channel muted, the tick loop has
            # nothing left to emit, so that zero move is the last
            # robot.move robotd sees from us.
            self._puppet.mute()
            self._puppet_move_applied = False
        threading.Thread(
            target=self._execute_panic, args=(held,), daemon=True, name=f"{self.duck_id}-panic"
        ).start()
        return True, None

    def _execute_panic(self, held: list[str]) -> None:
        err = self._send_stop_sequence()
        if err is not None:
            logger.warning("%s: panic robot.stop failed: %s", self.duck_id, err)
        self._notify_neutral()
        self._release_held_sounds(held)

    # -- sound hold/release ------------------------------------------------

    def _register_held_sound_locked(self, tag: str, hold_s: float, now_ns: int) -> None:
        """Track `tag` as held until `now_ns + hold_s`; `_tick_loop` takes
        it from here (module docstring / class docstring on
        `_held_sounds`): re-send `hold: true` every tick until the
        deadline, then release exactly once.
        """
        self._held_sounds[tag] = now_ns + int(hold_s * 1e9)

    def _take_held_sounds_locked(self) -> list[str]:
        """Hand back the tags that are still (nominally) sounding -- the
        caller must release them. Clearing the dict here is also what
        stops `_tick_loop` from re-sending `hold: true` for them on the
        very next tick.
        """
        held, self._held_sounds = self._held_sounds, {}
        return list(held.keys())

    def _collect_held_sound_actions_locked(self, now_ns: int) -> tuple[list[str], list[str]]:
        """Split currently-held tags into "still within `hold` seconds"
        (re-send `hold: true` this tick) and "deadline reached" (release
        once and stop tracking). Caller holds _playback_lock; called every
        tick regardless of FSM state -- the dict is normally empty except
        while PLAYING, since every other exit already emptied it via
        `_take_held_sounds_locked`.
        """
        resend: list[str] = []
        expired: list[str] = []
        for tag, deadline_ns in list(self._held_sounds.items()):
            if now_ns >= deadline_ns:
                expired.append(tag)
                del self._held_sounds[tag]
            else:
                resend.append(tag)
        return resend, expired

    def _resend_held_sound(self, tag: str) -> None:
        """Re-notify a still-held sound. docs/duckshow-format.md ("Held
        sounds"): `hold: true` is "a notification per tick, like the
        mouth" -- fire-and-forget, same as robot.move/head/pose/mouth, so
        a slow/unreachable robotd never stalls the tick loop waiting on a
        reply for this. A disconnect is already handled by
        `_on_robotd_state_change` -> `_enter_fault`, which empties
        `_held_sounds`, so nothing further is owed from here.
        """
        try:
            self.robotd.notify("robot.sound", {"tag": tag, "hold": True})
        except RobotdDisconnected as exc:
            logger.debug("%s: held-sound resend for %r failed: %s", self.duck_id, tag, exc)

    def _send_sound_release(self, tag: str) -> None:
        try:
            self.robotd.request("robot.sound", {"tag": tag, "hold": False}, timeout=ROBOTD_REQUEST_TIMEOUT_S)
        except _ROBOTD_FAILURES as exc:
            logger.warning("%s: release sound %r failed: %s", self.duck_id, tag, exc)

    def _release_held_sounds(self, tags: list[str]) -> None:
        for tag in tags:
            self._send_sound_release(tag)

    # -- event firing ----------------------------------------------------

    def _fire_event(self, event: duckshow.Event) -> None:
        """Runs on the tick thread *without* _playback_lock held across
        the (blocking) robotd request, so a slow discrete call never
        delays panic/stop. State is re-checked under the lock first.
        """
        kind = event.action_kind()
        with self._playback_lock:
            if self.state != "playing":
                return  # stop/panic/fault intervened since this tick was planned
        try:
            if kind == "do":
                self.robotd.request("robot.do", {"skill": event.do}, timeout=ROBOTD_REQUEST_TIMEOUT_S)
            elif kind == "sound":
                # duckshow-format.md's event `hold` is a *duration in
                # seconds*; robotd-api.md's robot.sound `hold` is a
                # boolean start/loop/end flag that (per upstream) must
                # keep arriving once per tick or it decays on its own.
                # Reconciled here: this first hold=true starts the loop
                # (a request, so a refusal -- e.g. BUSY -- is caught
                # below like any other discrete call); _tick_loop then
                # re-sends hold=true every tick for `hold` seconds
                # (_register_held_sound_locked / _collect_held_sound_
                # actions_locked) and sends hold=false exactly once when
                # that elapses.
                if event.hold is not None:
                    self.robotd.request(
                        "robot.sound", {"tag": event.sound, "hold": True}, timeout=ROBOTD_REQUEST_TIMEOUT_S
                    )
                    with self._playback_lock:
                        if self.state == "playing":
                            self._register_held_sound_locked(event.sound, event.hold, time.monotonic_ns())
                            return
                    # Playback ended while the sound was starting.
                    self._send_sound_release(event.sound)
                else:
                    self.robotd.request("robot.sound", {"tag": event.sound}, timeout=ROBOTD_REQUEST_TIMEOUT_S)
            elif kind == "mode":
                self.robotd.request("robot.setMode", {"mode": event.mode}, timeout=ROBOTD_REQUEST_TIMEOUT_S)
                with self._playback_lock:
                    self.current_mode = event.mode
        except _ROBOTD_FAILURES as exc:
            logger.warning("%s: event %s at t=%s failed: %s", self.duck_id, kind, event.t, exc)
            self._enter_fault(f"event {kind} failed: {exc}")

    def _fire_puppet_action(self, kind: str, name: str, epoch: int) -> None:
        """A puppet `do`/`sound`: same request path as `_fire_event`, on
        the puppet-action thread, outside _playback_lock. State and the
        channel epoch are re-checked first so an action drained just
        before a panic/stop (epoch moved) or a fault/arm (state) never
        fires -- the state check alone cannot catch panic/stop, whose
        landing states IDLE/LOADED are puppet-eligible.
        """
        with self._playback_lock:
            if self.state not in ("idle", "loaded", "playing") or self._puppet.epoch != epoch:
                return
        try:
            if kind == "do":
                self.robotd.request("robot.do", {"skill": name}, timeout=ROBOTD_REQUEST_TIMEOUT_S)
            else:
                self.robotd.request("robot.sound", {"tag": name}, timeout=ROBOTD_REQUEST_TIMEOUT_S)
        except RobotdError as exc:
            # A refused operator input (e.g. BUSY mid-skill) is not a duck
            # health problem -- never take a duck out of a number for it.
            logger.warning("%s: puppet %s %r refused by robotd: %s", self.duck_id, kind, name, exc)
        except (RobotdDisconnected, RobotdTimeout) as exc:
            logger.warning("%s: puppet %s %r failed: %s", self.duck_id, kind, name, exc)
            self._enter_fault(f"puppet {kind} failed: {exc}")

    # -- 50 Hz playback tick loop ------------------------------------------

    def _tick_loop(self) -> None:
        last_ns = time.monotonic_ns()
        while not self._stop_event.is_set():
            now_ns = time.monotonic_ns()
            dt_s = max(0.0, (now_ns - last_ns) / 1e9)
            last_ns = now_ns

            self.clock.update_applied_offset(playing=(self.state == "playing"), now_ns=now_ns)

            events: list[duckshow.Event] = []
            end_of_show = False
            puppet_actions: list[tuple[str, str]] = []
            puppet_epoch = 0
            with self._playback_lock:
                self._show_time_correction_s = slew_towards(
                    self._show_time_correction_s,
                    self._show_time_correction_target_s,
                    SHOW_TIME_SLEW_S_PER_S,
                    dt_s,
                )
                if self.state == "armed":
                    self._maybe_start_from_armed(now_ns)
                if self.state in ("playing", "paused"):
                    # PAUSED still ticks. The frame is frozen and locomotion is
                    # commanded to zero, but going silent would leave the duck
                    # coasting until robotd's 500 ms deadman caught it.
                    events, end_of_show = self._play_tick(now_ns)
                else:
                    self._puppet_tick(now_ns)
                # Queued do/sound are drained every tick; outside
                # IDLE/LOADED/PLAYING they are discarded (never fired late).
                # The epoch is read under the same lock panic/stop mute
                # under, so it is exactly the one these actions were
                # accepted under (_fire_puppet_action).
                puppet_actions = self._puppet.take_actions()
                puppet_epoch = self._puppet.epoch
                if self.state not in ("idle", "loaded", "playing"):
                    puppet_actions = []
                # Held sounds (docs/duckshow-format.md "Held sounds"):
                # decide, once per tick, which tags still need hold=true
                # re-sent and which just reached their deadline. Computed
                # every tick regardless of state -- the dict is normally
                # empty outside PLAYING, since every other exit already
                # emptied it via _take_held_sounds_locked.
                held_resend, held_expired = self._collect_held_sound_actions_locked(now_ns)

            # Discrete robotd requests happen outside the lock; puppet
            # actions go to their own thread so this loop's period never
            # depends on a robot.do round trip (module docstring).
            for event in events:
                self._fire_event(event)
            for tag in held_resend:
                self._resend_held_sound(tag)
            for tag in held_expired:
                self._send_sound_release(tag)
            for kind, name in puppet_actions:
                try:
                    self._puppet_actions.put_nowait((kind, name, puppet_epoch))
                except queue.Full:
                    logger.warning("%s: puppet %s %r dropped: robotd is still busy with earlier puppet actions", self.duck_id, kind, name)
            if end_of_show:
                self._end_of_show()

            self._stop_event.wait(TICK_PERIOD_S)

    def _puppet_action_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                kind, name, epoch = self._puppet_actions.get(timeout=LOOP_SLICE_S)
            except queue.Empty:
                continue
            self._fire_puppet_action(kind, name, epoch)

    def _puppet_tick(self, now_ns: int) -> None:
        """Puppet mode (IDLE/LOADED): forward fresh values; caller holds
        _playback_lock. In ARMED/FAULT nothing is forwarded, but a
        locomotion the puppet was driving is still zeroed once.
        """
        values = self._puppet.values(now_ns) if self.state in ("idle", "loaded") else PuppetValues()
        try:
            if values.move is not None:
                self.robotd.notify("robot.move", dict(values.move))
            elif self._puppet_move_applied:
                self.robotd.notify("robot.move", dict(_ZERO_MOVE))
            if values.head is not None:
                self.robotd.notify("robot.head", dict(values.head))
            if values.pose is not None:
                self.robotd.notify("robot.pose", dict(values.pose))
            if values.mouth is not None:
                self.robotd.notify("robot.mouth", dict(values.mouth))
        except RobotdDisconnected:
            if self._puppet_move_applied or values.move is not None:
                # robotd may still be executing the last velocity it got
                # from us; the stop is owed and sent on reconnect.
                self._stop_owed = True
            self._puppet_move_applied = False
            return
        self._puppet_move_applied = values.move is not None

    def _maybe_start_from_armed(self, now_ns: int) -> None:
        if self._scheduled_at_master_ns is None:
            return
        # Re-derived every tick: the clock steps to the refined offset
        # while ARMED, so the start tracks the best estimate up to the
        # instant it fires.
        local_start_ns = self.clock.local_time_for_master(self._scheduled_at_master_ns)
        if now_ns < local_start_ns:
            return
        late_s = (now_ns - local_start_ns) / 1e9
        if late_s >= LATE_JOIN_MAX_S:
            # The offset moved under us by seconds (or we were asleep):
            # never improvise to catch up. Sit this one out.
            logger.warning("%s: scheduled start is %.2fs in the past; sitting out", self.duck_id, late_s)
            self._cancel_scheduled()
            self._play_epoch_show_time = 0.0
            self.state = "loaded"
            self.last_error = "missed_start"
            return
        self._begin_playback(self._scheduled_from_show_time, local_start_ns, now_ns)

    def _play_tick(self, now_ns: int) -> tuple[list[duckshow.Event], bool]:
        """Emit this tick's continuous intents; return the discrete events
        that became due and whether the show just ended (both handled by
        the caller after releasing _playback_lock).
        """
        assert self.sampler is not None

        if self._pending_pause is not None:
            target_local_ns = self.clock.local_time_for_master(self._pending_pause)
            if now_ns >= target_local_ns:
                # Freeze at the instant the master named, not at "now", so
                # every duck stops at the same show-time.
                self._paused_show_time = self._current_show_time(target_local_ns)
                self._pending_pause = None
                self.state = "paused"

        if self._pending_resume is not None:
            target_local_ns = self.clock.local_time_for_master(self._pending_resume)
            if now_ns >= target_local_ns:
                resume_from = self._paused_show_time
                self._pending_resume = None
                if resume_from is not None:
                    # _start_playing_now, not _begin_playback: the latter adds
                    # lateness to the target show-time and re-seeds the event
                    # window past it, which on a resume would silently skip
                    # every event in the gap. Anchoring at the scheduled
                    # instant makes the clock identical on every duck.
                    self._start_playing_now(resume_from, target_local_ns)
                    if self.state != "playing":
                        return [], False  # setMode at the resume point faulted us

        if self._pending_seek is not None:
            at_master_ns, target_show_time = self._pending_seek
            target_local_ns = self.clock.local_time_for_master(at_master_ns)
            if now_ns >= target_local_ns:
                self._pending_seek = None
                if self._paused_show_time is not None:
                    # Scrubbing during a hold: move the frozen position, stay
                    # frozen. Falling through to _begin_playback would clear
                    # the pause and start the duck performing alone.
                    self._paused_show_time = float(target_show_time)
                    self._last_processed_show_time = math.nextafter(float(target_show_time), -math.inf)
                    self._apply_mode_for_show_time(float(target_show_time))
                else:
                    self._begin_playback(target_show_time, target_local_ns, now_ns)
                if self.state != "playing":
                    return [], False  # setMode at the seek point faulted us

        show_time = self._current_show_time(now_ns)
        duration = self.sampler.show.meta.duration

        frame = self.sampler.at(show_time)
        servo = self.sampler.servo_at(show_time)
        locomotion_frozen = (servo is not None and servo.mode == "hold") or self._paused_show_time is not None

        # Nudge layer (module docstring): puppet move adds to the timeline's
        # locomotion, puppet head/pose/mouth replace the timeline's while
        # fresh. A servo hold owns locomotion outright (no nudge either).
        puppet = self._puppet.values(now_ns)
        move_out: Optional[dict[str, float]] = None
        puppet_applied = False
        if not locomotion_frozen:
            if frame.locomotion is not None:
                v = frame.locomotion
                move_out = {"vx": v.vx, "vy": v.vy, "vyaw": v.vyaw}
                if puppet.move is not None:
                    move_out = nudge_move(move_out, puppet.move)
                    puppet_applied = True
            elif puppet.move is not None:
                move_out = dict(puppet.move)  # no locomotion track: nudged from standing
                puppet_applied = True
        if locomotion_frozen:
            # Freeze means COMMAND zero, not stop commanding. Leaving move_out
            # as None sends no robot.move at all, so the duck coasts at its
            # last commanded velocity until robotd's 500 ms deadman
            # (deploy/robotd.toml) happens to catch it -- up to half a second
            # of unwanted travel at the start of every servo hold, and a
            # silent dependence on a safety net that exists for lost links,
            # not for choreography. docs/duckshow-format.md has always
            # described this mode as "freeze locomotion"; this makes the code
            # mean that. Sending zero every tick also keeps the deadman fed,
            # which is what every other playing tick already does.
            move_out = dict(_ZERO_MOVE)
        elif move_out is None and self._puppet_move_applied:
            move_out = dict(_ZERO_MOVE)  # the puppet's velocity would otherwise be held forever

        head_out: Optional[dict[str, Any]] = None
        if puppet.head is not None:
            head_out = dict(puppet.head)
        elif frame.head is not None:
            h = frame.head
            head_out = {"neck_pitch": h.neck_pitch, "head_pitch": h.head_pitch, "head_yaw": h.head_yaw, "head_roll": h.head_roll}

        pose_out: Optional[dict[str, Any]] = None
        if puppet.pose is not None:
            pose_out = dict(puppet.pose)
        elif frame.pose is not None:
            p = frame.pose
            pose_out = {"z": p.z, "roll": p.roll, "pitch": p.pitch, "active": p.active}

        mouth_out: Optional[dict[str, Any]] = None
        if puppet.mouth is not None:
            mouth_out = dict(puppet.mouth)
        elif frame.mouth is not None:
            mouth_out = {"open": frame.mouth.open}

        try:
            if move_out is not None:
                self.robotd.notify("robot.move", move_out)
            if head_out is not None:
                self.robotd.notify("robot.head", head_out)
            if pose_out is not None:
                self.robotd.notify("robot.pose", pose_out)
            if mouth_out is not None:
                self.robotd.notify("robot.mouth", mouth_out)
        except RobotdDisconnected:
            # The link is gone mid-number: this is terminal for the number
            # (a duck never resumes into the middle of it when robotd
            # returns); the stop is owed and sent on reconnect.
            self._enter_fault("robotd disconnected")
            return [], False
        self._puppet_move_applied = puppet_applied

        events = self.sampler.events_between(self._last_processed_show_time, show_time)
        self._last_processed_show_time = show_time

        return events, (duration is not None and show_time >= duration)

    def _end_of_show(self) -> None:
        with self._playback_lock:
            if self.state != "playing":
                return  # an event failure / stop / panic already handled the exit
            held = self._take_held_sounds_locked()
            # Put the duck back to a known shape while it is still being
            # commanded, and before the stop, so the last thing robotd hears
            # is still robot.stop. Without this a show ending between a
            # mouthOpen keyframe and its close left the bill open on stage:
            # meta.duration is the only end-of-show trigger, and nothing on
            # this path ever touched head, pose or mouth again. These are
            # notifies, not round trips, unlike the stop sequence below that
            # already runs under this lock.
            self._notify_neutral()
            err = self._send_stop_sequence()
            if err is not None:
                # Any robotd error -> FAULT, reported -- never a silent
                # "loaded" for a duck whose robotd never acknowledged the stop.
                logger.warning("%s: end-of-show robot.stop failed: %s", self.duck_id, err)
                self._fault_locked(f"end-of-show stop failed: {err}")
            else:
                self._play_epoch_local_ns = None
                self._play_epoch_show_time = 0.0
                self.state = "loaded"
        self._release_held_sounds(held)

    # -- time sync / telemetry sender loops --------------------------------

    def _time_sync_loop(self) -> None:
        last_sent: Optional[float] = None
        while not self._stop_event.is_set():
            now = time.monotonic()
            period = TIME_SYNC_PERIOD_ARMED_S if self.state == "armed" else TIME_SYNC_PERIOD_S
            if last_sent is None or now - last_sent >= period:
                self._send_time_req()
                last_sent = now
            self._stop_event.wait(LOOP_SLICE_S)

    def _send_time_req(self) -> None:
        addr = self.master_addr
        if addr is None:
            return
        msg = {"v": PROTOCOL_VERSION, "type": "time_req", "duck": self.duck_id, "t0": time.monotonic_ns()}
        try:
            self.udp_sock.sendto(json.dumps(msg).encode("utf-8"), addr)
        except OSError:
            pass

    def _telemetry_loop(self) -> None:
        last_sent: Optional[float] = None
        while not self._stop_event.is_set():
            now = time.monotonic()
            period = TELEMETRY_PERIOD_PLAYING_S if self.state == "playing" else TELEMETRY_PERIOD_IDLE_S
            if last_sent is None or now - last_sent >= period:
                self._send_telemetry()
                last_sent = now
            self._stop_event.wait(LOOP_SLICE_S)

    def _send_telemetry(self) -> None:
        addr = self.master_addr
        if addr is None:
            return
        msg = self.build_telemetry()
        try:
            self.udp_sock.sendto(json.dumps(msg).encode("utf-8"), addr)
        except OSError:
            pass
