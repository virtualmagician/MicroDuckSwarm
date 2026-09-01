#!/usr/bin/env python3
"""showmaster.py -- reference SwarmLink MASTER CLI.

Implements the master side of docs/swarmlink-protocol.md: a UDP socket
bound to a well-known port that answers agent time_req exchanges,
receives acks/telemetry, sends the 5 Hz transport state broadcast while
a show is armed/playing, and drives the load/play/seek/stop/panic
command handshake (send, repeat until ACKed, report per duck).

Single file, stdlib only (see CLAUDE.md).

Usage:
    python3 tools/showmaster.py --duck duck-01=127.0.0.1:47801 \\
        --role lead=duck-01 load shows/demo/demo.duckshow.json

    python3 tools/showmaster.py --duck duck-01=127.0.0.1:47801 \\
        --role lead=duck-01 run shows/demo/demo.duckshow.json

Subcommands: load, play, seek, stop, panic, monitor, run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("showmaster")

PROTOCOL_VERSION = 1
DEFAULT_MASTER_PORT = 47800
DEFAULT_AGENT_PORT = 47801
STATE_HZ = 5.0
DEFAULT_LEAD_MS = 1500
DEFAULT_CMD_RETRIES = 5
DEFAULT_CMD_TIMEOUT_MS = 100
MAX_DATAGRAM_BYTES = 1200


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def show_id_for(path: Path) -> str:
    """Derive a show id from a .duckshow.json filename (strip the suffix)."""
    name = path.name
    for suffix in (".duckshow.json",):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def load_show_file(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Roster / role parsing
# --------------------------------------------------------------------------


def parse_duck_arg(value: str) -> tuple[str, tuple[str, int]]:
    """Parse "duck-01=127.0.0.1:47801" -> ("duck-01", ("127.0.0.1", 47801))."""
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"--duck must be ID=HOST:PORT, got {value!r}")
    duck_id, _, addr = value.partition("=")
    if ":" not in addr:
        raise argparse.ArgumentTypeError(f"--duck address must be HOST:PORT, got {addr!r}")
    host, _, port_s = addr.rpartition(":")
    try:
        port = int(port_s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--duck port must be an integer, got {port_s!r}")
    return duck_id, (host, port)


def parse_role_arg(value: str) -> tuple[str, str]:
    """Parse "lead=duck-01" -> ("lead", "duck-01")."""
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"--role must be ROLE=DUCK_ID, got {value!r}")
    role, _, duck_id = value.partition("=")
    return role, duck_id


def build_roster(duck_args: list[str]) -> dict[str, tuple[str, int]]:
    roster: dict[str, tuple[str, int]] = {}
    for raw in duck_args or []:
        duck_id, addr = parse_duck_arg(raw)
        roster[duck_id] = addr
    return roster


def build_roles(role_args: list[str]) -> dict[str, str]:
    roles: dict[str, str] = {}
    for raw in role_args or []:
        role, duck_id = parse_role_arg(raw)
        roles[role] = duck_id
    return roles


# --------------------------------------------------------------------------
# SwarmMaster: the protocol engine, independent of argparse/CLI concerns so
# it can be driven directly from unit tests against a fake agent socket.
# --------------------------------------------------------------------------


class SwarmMaster:
    def __init__(
        self,
        roster: dict[str, tuple[str, int]],
        roles: Optional[dict[str, str]] = None,
        port: int = DEFAULT_MASTER_PORT,
        cmd_retries: int = DEFAULT_CMD_RETRIES,
        cmd_timeout_ms: float = DEFAULT_CMD_TIMEOUT_MS,
        on_telemetry: Optional[Callable[[dict[str, Any]], None]] = None,
    ):
        self.roster = dict(roster)
        self.roles = dict(roles or {})
        self.cmd_retries = cmd_retries
        self.cmd_timeout_ms = cmd_timeout_ms
        self.on_telemetry = on_telemetry

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(0.5)
        self.bound_port = self.sock.getsockname()[1]

        self._stop_event = threading.Event()
        self._recv_thread: Optional[threading.Thread] = None
        self._state_thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], threading.Event] = {}
        self._ack_results: dict[tuple[str, str], dict[str, Any]] = {}
        self.last_telemetry: dict[str, dict[str, Any]] = {}

        # Transport state
        self.transport = "stopped"  # "stopped" | "armed" | "playing"
        self.current_show_id: Optional[str] = None
        self.play_epoch_ns: Optional[int] = None
        self.from_show_time: float = 0.0
        self._seq = 0

    # -- lifecycle --

    def start(self) -> None:
        self._stop_event.clear()
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True, name="showmaster-recv")
        self._recv_thread.start()
        self._state_thread = threading.Thread(target=self._state_loop, daemon=True, name="showmaster-state")
        self._state_thread.start()

    def close(self) -> None:
        """Lifecycle teardown: stop background threads and release the socket.

        Named `close` (not `stop`) to avoid colliding with the high-level
        `stop()` method below, which sends the protocol STOP *command*
        (docs/swarmlink-protocol.md section 3) to the cast.
        """
        self._stop_event.set()
        if self._recv_thread:
            self._recv_thread.join(timeout=2)
        if self._state_thread:
            self._state_thread.join(timeout=2)
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "SwarmMaster":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- roster/role helpers --

    def target_ducks(self) -> list[str]:
        """Ducks that should receive load/play/seek: those with an assigned role.

        Falls back to the full roster if no --role mappings were given at
        all (there is then no cast to consult, so we address everyone).
        """
        if self.roles:
            return sorted(d for d in self.roles.values() if d in self.roster)
        return sorted(self.roster.keys())

    def role_for(self, duck_id: str) -> Optional[str]:
        for role, d in self.roles.items():
            if d == duck_id:
                return role
        return None

    # -- receive loop: time_req / ack / telemetry --

    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                data, addr = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue  # protocol: malformed/unknown datagrams are dropped silently
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")
            if mtype == "time_req":
                self._handle_time_req(msg, addr)
            elif mtype == "ack":
                self._handle_ack(msg)
            elif mtype == "telemetry":
                self._handle_telemetry(msg)
            # unknown type -> dropped silently, per swarmlink-protocol.md #3

    def _handle_time_req(self, msg: dict[str, Any], addr: tuple[str, int]) -> None:
        t1 = time.monotonic_ns()
        t0 = msg.get("t0")
        resp = {"v": PROTOCOL_VERSION, "type": "time_resp", "t0": t0, "t1": t1, "t2": time.monotonic_ns()}
        try:
            self.sock.sendto(json.dumps(resp).encode("utf-8"), addr)
        except OSError:
            pass

    def _handle_ack(self, msg: dict[str, Any]) -> None:
        duck_id = msg.get("duck")
        cmd_id = msg.get("cmd_id")
        key = (duck_id, cmd_id)
        with self._lock:
            event = self._pending.get(key)
            if event is not None:
                self._ack_results[key] = {"ok": msg.get("ok"), "error": msg.get("error")}
                event.set()

    def _handle_telemetry(self, msg: dict[str, Any]) -> None:
        duck_id = msg.get("duck")
        if duck_id:
            self.last_telemetry[duck_id] = msg
        print(
            f"[telemetry] duck={duck_id} state={msg.get('state')} "
            f"show_time={msg.get('show_time')} clock_offset_ms={msg.get('clock_offset_ms')}"
        )
        if self.on_telemetry:
            self.on_telemetry(msg)

    # -- 5 Hz transport state broadcast while armed/playing --

    def _current_show_time(self) -> float:
        if self.transport == "playing" and self.play_epoch_ns is not None:
            now_ns = time.monotonic_ns()
            elapsed_s = max(0.0, (now_ns - self.play_epoch_ns) / 1e9)
            return self.from_show_time + elapsed_s
        return self.from_show_time

    def _maybe_advance_armed_to_playing(self) -> None:
        if self.transport == "armed" and self.play_epoch_ns is not None:
            if time.monotonic_ns() >= self.play_epoch_ns:
                self.transport = "playing"

    def _state_loop(self) -> None:
        period = 1.0 / STATE_HZ
        while not self._stop_event.is_set():
            self._maybe_advance_armed_to_playing()
            if self.transport in ("armed", "playing"):
                msg = {
                    "v": PROTOCOL_VERSION,
                    "type": "state",
                    "seq": self._seq,
                    "show": self.current_show_id,
                    "transport": self.transport,
                    "show_time": self._current_show_time(),
                    "master_time": time.monotonic_ns(),
                }
                self._seq += 1
                payload = json.dumps(msg).encode("utf-8")
                for addr in self.roster.values():
                    try:
                        self.sock.sendto(payload, addr)
                    except OSError:
                        pass
            self._stop_event.wait(period)

    # -- command send/retry/ack --

    def _send_and_wait(self, duck_id: str, cmd: dict[str, Any]) -> bool:
        cmd_id = cmd["cmd_id"]
        key = (duck_id, cmd_id)
        event = threading.Event()
        with self._lock:
            self._pending[key] = event
            self._ack_results[key] = None
        addr = self.roster.get(duck_id)
        ok = False
        try:
            if addr is None:
                logger.warning("no roster address for duck %s; cannot send %s", duck_id, cmd.get("cmd"))
                return False
            payload = json.dumps(cmd).encode("utf-8")
            if len(payload) > MAX_DATAGRAM_BYTES:
                logger.warning("cmd payload for %s exceeds %d bytes (%d)", duck_id, MAX_DATAGRAM_BYTES, len(payload))
            for attempt in range(1, self.cmd_retries + 1):
                try:
                    self.sock.sendto(payload, addr)
                except OSError as exc:
                    logger.warning("send to %s failed: %s", duck_id, exc)
                got = event.wait(self.cmd_timeout_ms / 1000.0)
                if got:
                    with self._lock:
                        result = self._ack_results.get(key)
                    ok = bool(result and result.get("ok"))
                    if not ok:
                        logger.warning(
                            "duck %s NACKed %s (cmd_id=%s): %s",
                            duck_id,
                            cmd.get("cmd"),
                            cmd_id,
                            result.get("error") if result else None,
                        )
                    break
                logger.debug("duck %s: no ack for %s attempt %d/%d", duck_id, cmd.get("cmd"), attempt, self.cmd_retries)
            else:
                logger.warning("duck %s TIMED OUT waiting for ack of %s (cmd_id=%s)", duck_id, cmd.get("cmd"), cmd_id)
        finally:
            with self._lock:
                self._pending.pop(key, None)
                self._ack_results.pop(key, None)
        return ok

    def send_command(
        self,
        duck_ids: list[str],
        cmd_type: str,
        base_fields: dict[str, Any],
        per_duck_fields: Optional[dict[str, dict[str, Any]]] = None,
        cmd_id: Optional[str] = None,
    ) -> dict[str, bool]:
        """Send `cmd_type` to every duck in duck_ids, in parallel, retrying
        up to cmd_retries times at cmd_timeout_ms intervals until each
        duck ACKs. Returns {duck_id: True/False}.
        """
        cmd_id = cmd_id or str(uuid.uuid4())
        results: dict[str, bool] = {}
        results_lock = threading.Lock()

        def worker(duck_id: str) -> None:
            fields = dict(base_fields)
            if per_duck_fields and duck_id in per_duck_fields:
                fields.update(per_duck_fields[duck_id])
            cmd = {"v": PROTOCOL_VERSION, "type": "cmd", "cmd_id": cmd_id, "cmd": cmd_type, **fields}
            ok = self._send_and_wait(duck_id, cmd)
            with results_lock:
                results[duck_id] = ok

        threads = [threading.Thread(target=worker, args=(d,), daemon=True) for d in duck_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    # -- high-level operations --

    def load(self, show_path: Path) -> dict[str, bool]:
        show_id = show_id_for(show_path)
        sha256 = sha256_of_file(show_path)
        duck_ids = self.target_ducks()
        per_duck = {d: {"role": self.role_for(d) or d} for d in duck_ids}
        results = self.send_command(
            duck_ids, "load", {"show": show_id, "sha256": sha256}, per_duck_fields=per_duck
        )
        if all(results.values()):
            self.current_show_id = show_id
        return results

    def play(self, show_path: Path, lead_ms: int = DEFAULT_LEAD_MS, from_show_time: float = 0.0) -> dict[str, bool]:
        show_id = show_id_for(show_path)
        duck_ids = self.target_ducks()
        at_master_time = time.monotonic_ns() + int(lead_ms * 1_000_000)
        results = self.send_command(
            duck_ids,
            "play",
            {"show": show_id, "at_master_time": at_master_time, "from_show_time": from_show_time},
        )
        if all(results.values()):
            self.current_show_id = show_id
            self.play_epoch_ns = at_master_time
            self.from_show_time = from_show_time
            self.transport = "armed"
        return results

    def seek(self, show_time: float, lead_ms: int = DEFAULT_LEAD_MS) -> dict[str, bool]:
        duck_ids = self.target_ducks()
        at_master_time = time.monotonic_ns() + int(lead_ms * 1_000_000)
        results = self.send_command(
            duck_ids, "seek", {"show_time": show_time, "at_master_time": at_master_time}
        )
        if all(results.values()):
            self.play_epoch_ns = at_master_time
            self.from_show_time = show_time
            self.transport = "armed"
        return results

    def stop(self) -> dict[str, bool]:
        duck_ids = self.target_ducks()
        results = self.send_command(duck_ids, "stop", {})
        if all(results.values()):
            self.transport = "stopped"
            self.play_epoch_ns = None
        return results

    def panic(self) -> dict[str, bool]:
        # Safety: panic always addresses the whole roster, not just the
        # current cast, per swarmlink-protocol.md ("Never NACKed").
        duck_ids = sorted(self.roster.keys())
        results = self.send_command(duck_ids, "panic", {})
        self.transport = "stopped"
        self.play_epoch_ns = None
        return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_ack_report(action: str, results: dict[str, bool]) -> None:
    for duck_id in sorted(results):
        ok = results[duck_id]
        print(f"[{action}] {duck_id}: {'ACK' if ok else 'TIMEOUT/NACK'}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="showmaster.py",
        description="Reference SwarmLink MASTER CLI (docs/swarmlink-protocol.md).",
    )
    p.add_argument(
        "--duck",
        action="append",
        default=[],
        metavar="ID=HOST:PORT",
        help="Roster entry, repeatable. E.g. --duck duck-01=127.0.0.1:47801",
    )
    p.add_argument(
        "--role",
        action="append",
        default=[],
        metavar="ROLE=DUCK_ID",
        help="Cast role -> duck-id mapping, repeatable. E.g. --role lead=duck-01",
    )
    p.add_argument("--port", type=int, default=DEFAULT_MASTER_PORT, help=f"UDP port to bind (default {DEFAULT_MASTER_PORT}).")
    p.add_argument("--lead-ms", type=int, default=DEFAULT_LEAD_MS, help=f"play/seek scheduling lead time (default {DEFAULT_LEAD_MS}).")
    p.add_argument("--cmd-retries", type=int, default=DEFAULT_CMD_RETRIES, help=f"Command retry count (default {DEFAULT_CMD_RETRIES}).")
    p.add_argument("--cmd-timeout-ms", type=float, default=DEFAULT_CMD_TIMEOUT_MS, help=f"Command retry interval (default {DEFAULT_CMD_TIMEOUT_MS}).")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")

    sub = p.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", help="Send the load command for a show file.")
    p_load.add_argument("showfile", type=Path)

    p_play = sub.add_parser("play", help="Load then play a show file.")
    p_play.add_argument("showfile", type=Path)
    p_play.add_argument("--from", dest="from_show_time", type=float, default=0.0, help="Start show_time (default 0.0).")

    p_seek = sub.add_parser("seek", help="Seek the currently-loaded show to a given show_time.")
    p_seek.add_argument("show_time", type=float)

    sub.add_parser("stop", help="Stop playback gracefully.")
    sub.add_parser("panic", help="Panic-stop the whole roster immediately.")
    sub.add_parser("monitor", help="Print telemetry and answer time syncs until interrupted.")

    p_run = sub.add_parser("run", help="Load + play a show, monitor telemetry until it ends, then exit 0.")
    p_run.add_argument("showfile", type=Path)
    p_run.add_argument("--from", dest="from_show_time", type=float, default=0.0, help="Start show_time (default 0.0).")

    return p


def _block_until_interrupted(seconds: Optional[float] = None) -> None:
    start = time.monotonic()
    try:
        while True:
            if seconds is not None and (time.monotonic() - start) >= seconds:
                return
            time.sleep(0.2)
    except KeyboardInterrupt:
        return


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s showmaster %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    roster = build_roster(args.duck)
    roles = build_roles(args.role)

    master = SwarmMaster(
        roster=roster,
        roles=roles,
        port=args.port,
        cmd_retries=args.cmd_retries,
        cmd_timeout_ms=args.cmd_timeout_ms,
    )
    master.start()
    try:
        if args.command == "load":
            results = master.load(args.showfile)
            _print_ack_report("load", results)
            return 0 if all(results.values()) else 1

        if args.command == "play":
            load_results = master.load(args.showfile)
            _print_ack_report("load", load_results)
            if not all(load_results.values()):
                return 1
            play_results = master.play(args.showfile, lead_ms=args.lead_ms, from_show_time=args.from_show_time)
            _print_ack_report("play", play_results)
            if not all(play_results.values()):
                return 1
            print(f"[play] armed; starting in ~{args.lead_ms} ms. Monitoring telemetry (Ctrl+C to exit).")
            _block_until_interrupted()
            return 0

        if args.command == "seek":
            results = master.seek(args.show_time, lead_ms=args.lead_ms)
            _print_ack_report("seek", results)
            return 0 if all(results.values()) else 1

        if args.command == "stop":
            results = master.stop()
            _print_ack_report("stop", results)
            return 0 if all(results.values()) else 1

        if args.command == "panic":
            results = master.panic()
            _print_ack_report("panic", results)
            return 0 if all(results.values()) else 1

        if args.command == "monitor":
            print("[monitor] listening for telemetry (Ctrl+C to exit)...")
            _block_until_interrupted()
            return 0

        if args.command == "run":
            load_results = master.load(args.showfile)
            _print_ack_report("load", load_results)
            if not all(load_results.values()):
                return 1
            play_results = master.play(args.showfile, lead_ms=args.lead_ms, from_show_time=args.from_show_time)
            _print_ack_report("play", play_results)
            if not all(play_results.values()):
                return 1

            show = load_show_file(args.showfile)
            duration = float(show.get("meta", {}).get("duration", 0.0))
            remaining = max(0.0, duration - args.from_show_time)
            wait_s = (args.lead_ms / 1000.0) + remaining
            print(f"[run] armed; playing for ~{wait_s:.1f}s then exiting.")
            _block_until_interrupted(seconds=wait_s)
            return 0

        return 2
    finally:
        master.close()


if __name__ == "__main__":
    raise SystemExit(main())
