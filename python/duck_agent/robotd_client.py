"""NDJSON JSON-RPC 2.0 client for robotd (docs/robotd-api.md).

Connects to either a Unix domain socket (a plain filesystem path) or a
TCP host:port (the mock duck's dev-convenience listener) -- auto-detected
by whether the target string contains a ":" (a bare path never does; an
absolute path like "/tmp/robotd.sock" doesn't either).

One background reader thread demultiplexes replies by `id` (for
`request()` callers, who block on a per-call event) and forwards
unsolicited notifications (e.g. `robot.state` after `robot.subscribe`)
to an optional callback. Writes are serialized by a lock so `notify()`
and `request()` can both be called from the playback tick thread and
the RPC-issuing thread without interleaving partial lines.

On connect, `hello {"api_version": 16}` is sent and the reply logged
(docs/robotd-api.md: "Version mismatch is reported, not refused").

If the connection drops, a background thread reconnects with capped
exponential backoff. While disconnected, `notify()`/`request()` raise
RobotdDisconnected so callers (duck_agent.agent) can pause intent
emission and surface state "fault" until the socket is back.

Writes are bounded: a robotd that stops *reading* but keeps the socket
open (deadlocked, SIGSTOPped, starved) would otherwise fill the kernel
send buffer and park the caller inside `sendall` forever -- on the agent
that is the 50 Hz tick thread holding its playback lock, so panic could
neither ACK nor reach robotd. `_write_line` therefore waits at most
`SEND_TIMEOUT_S` (notifications) / the request's own timeout for the
socket to become writable; if it does not, the link is treated as dead:
the socket is closed (the reconnect loop takes over, and the agent's
owed-stop logic re-sends `robot.stop` on reconnect) and
RobotdDisconnected is raised.
"""

from __future__ import annotations

import json
import logging
import select
import socket
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("duck_agent.robotd_client")

API_VERSION = 16
_RECV_BUFSIZE = 65536

# Longest a notification may wait for the socket to accept it (module
# docstring). Ten tick periods: far longer than a healthy robotd ever
# needs to drain a few hundred bytes, short enough that a panic queued
# behind it still ACKs promptly.
SEND_TIMEOUT_S = 0.2


class RobotdError(Exception):
    """A JSON-RPC error reply (docs/robotd-api.md error codes)."""

    def __init__(self, code: int, message: str):
        super().__init__(f"robotd error {code}: {message}")
        self.code = code
        self.message = message


class RobotdDisconnected(Exception):
    """Raised by notify()/request() while the socket is down."""


class RobotdTimeout(Exception):
    """Raised by request() if no reply arrives within `timeout` seconds."""


def _is_tcp_target(target: str) -> bool:
    return ":" in target


def _parse_tcp_target(target: str) -> tuple[str, int]:
    host, _, port_s = target.rpartition(":")
    return host, int(port_s)


class RobotdClient:
    """A reconnecting NDJSON JSON-RPC client for one robotd endpoint.

    `target` is either a Unix socket path (no ":") or "host:port" (TCP).
    """

    def __init__(
        self,
        target: str,
        on_notification: Optional[Callable[[str, dict[str, Any]], None]] = None,
        on_state_change: Optional[Callable[[bool], None]] = None,
        connect_timeout: float = 5.0,
        backoff_initial: float = 0.2,
        backoff_max: float = 5.0,
    ):
        self.target = target
        self.on_notification = on_notification
        self.on_state_change = on_state_change
        self.connect_timeout = connect_timeout
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max

        self._is_tcp = _is_tcp_target(target)

        self._sock: Optional[socket.socket] = None
        self._sock_file = None  # buffered readline() helper
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._connected = False

        self._next_id = 1
        self._id_lock = threading.Lock()
        self._pending: dict[int, threading.Event] = {}
        self._replies: dict[int, dict[str, Any]] = {}

        self._stop_event = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._connector_thread: Optional[threading.Thread] = None

        self.hello_reply: Optional[dict[str, Any]] = None

    # -- public lifecycle --------------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        self._connector_thread = threading.Thread(
            target=self._connector_loop, daemon=True, name="robotd-connector"
        )
        self._connector_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._close_socket()
        if self._connector_thread:
            self._connector_thread.join(timeout=2)
        if self._reader_thread:
            self._reader_thread.join(timeout=2)

    def __enter__(self) -> "RobotdClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._connected

    # -- connection management ---------------------------------------------

    def _connector_loop(self) -> None:
        backoff = self.backoff_initial
        while not self._stop_event.is_set():
            try:
                self._connect_once()
                backoff = self.backoff_initial  # reset after a clean connect
            except OSError as exc:
                logger.warning("robotd connect to %s failed: %s (retry in %.2fs)", self.target, exc, backoff)
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, self.backoff_max)
                continue
            except Exception:  # noqa: BLE001 -- the reconnect loop must never die on show night
                logger.exception("robotd connector to %s raised; retrying in %.2fs", self.target, backoff)
                self._close_socket()
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, self.backoff_max)
                continue

            # _connect_once only returns once the reader thread has exited
            # (EOF or a socket error), i.e. the connection dropped.
            self._set_connected(False)
            if self._stop_event.is_set():
                return
            self._stop_event.wait(backoff)
            backoff = min(backoff * 2, self.backoff_max)

    def _connect_once(self) -> None:
        if self._is_tcp:
            host, port = _parse_tcp_target(self.target)
            sock = socket.create_connection((host, port), timeout=self.connect_timeout)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.connect_timeout)
            sock.connect(self.target)
        sock.settimeout(None)

        with self._state_lock:
            self._sock = sock
            self._sock_file = sock.makefile("rb")

        logger.info("robotd connected: %s", self.target)

        # Start reading *before* sending hello: _raw_request() below
        # blocks on a threading.Event that only the reader thread can
        # set when the reply line arrives. Without a reader already
        # running, the hello handshake would deadlock until it times out.
        reader_thread = threading.Thread(target=self._reader_loop, daemon=True, name="robotd-reader")
        self._reader_thread = reader_thread
        reader_thread.start()

        try:
            reply = self._raw_request("hello", {"api_version": API_VERSION}, timeout=self.connect_timeout)
            self.hello_reply = reply
            logger.info("robotd hello reply: %s", reply)
        except (RobotdTimeout, RobotdError, RobotdDisconnected, OSError) as exc:
            # RobotdDisconnected is what _write_line raises when the peer
            # accepts and immediately drops the connection (EPIPE, or the
            # reader's EOF nulling the socket first) -- it must feed the
            # same reconnect/backoff path as any other failed handshake.
            logger.warning("robotd hello handshake failed: %s", exc)
            self._close_socket()
            reader_thread.join(timeout=2)
            raise OSError(str(exc)) from exc

        daemon_api = reply.get("api_version") if isinstance(reply, dict) else None
        if daemon_api != API_VERSION:
            # docs/robotd-api.md: "Version mismatch is reported, not refused."
            logger.warning(
                "robotd api_version %s != expected %s (daemon_version=%s); proceeding, expect method/param mismatches",
                daemon_api,
                API_VERSION,
                reply.get("daemon_version") if isinstance(reply, dict) else None,
            )

        self._set_connected(True)

        # Block here until the reader thread exits (EOF or socket error),
        # i.e. until the connection actually drops.
        reader_thread.join()

    def _set_connected(self, value: bool) -> None:
        with self._state_lock:
            changed = self._connected != value
            self._connected = value
        if changed and self.on_state_change:
            try:
                self.on_state_change(value)
            except Exception:
                logger.exception("on_state_change callback raised")

    def _close_socket(self) -> None:
        with self._state_lock:
            sock, self._sock = self._sock, None
            sock_file, self._sock_file = self._sock_file, None
        if sock is not None:
            # shutdown() *before* close(): on POSIX (notably macOS/BSD),
            # closing a socket from a thread other than the one blocked in
            # recv()/readline() on it does not reliably wake that thread up.
            # shutdown(SHUT_RDWR) does -- it forces the blocked read to
            # return (EOF/empty), which is what lets the reader thread
            # (and thus _connect_once()'s reader_thread.join()) exit
            # promptly from stop() or a mid-hello failure.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if sock_file is not None:
            try:
                sock_file.close()
            except OSError:
                pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        # Wake up anyone blocked in request() with a disconnect. Done
        # entirely under _id_lock so it cannot race _raw_request's cleanup
        # (a timed-out waiter popping its id) or a second concurrent
        # closer: only ids that are *still* pending get a reply injected.
        with self._id_lock:
            pending = list(self._pending.items())
            self._pending.clear()
            for mid, event in pending:
                self._replies[mid] = {"error": {"code": -1, "message": "disconnected"}}
                event.set()

    # -- reader loop ---------------------------------------------------------

    def _reader_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                with self._state_lock:
                    sock_file = self._sock_file
                if sock_file is None:
                    return
                try:
                    line = sock_file.readline()
                except OSError:
                    return
                if not line:
                    return  # EOF
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    logger.warning("robotd sent malformed line: %r", line[:200])
                    continue
                self._handle_incoming(msg)
        finally:
            self._close_socket()

    def _handle_incoming(self, msg: dict[str, Any]) -> None:
        if not isinstance(msg, dict):
            return
        if "id" in msg and msg.get("id") is not None and ("result" in msg or "error" in msg):
            mid = msg["id"]
            with self._id_lock:
                event = self._pending.get(mid)
                if event is not None:
                    self._replies[mid] = msg
                    event.set()
                    return
            # No one is waiting (e.g. a very late hello reply) -- drop it.
            return
        method = msg.get("method")
        if method:
            if self.on_notification:
                try:
                    self.on_notification(method, msg.get("params") or {})
                except Exception:
                    logger.exception("on_notification callback raised for %s", method)

    # -- write path ------------------------------------------------------

    def _write_line(self, obj: dict[str, Any], send_timeout: float = SEND_TIMEOUT_S) -> None:
        """Write one NDJSON line, waiting at most `send_timeout` for the
        socket to accept it (module docstring). A socket that stays
        unwritable that long is a robotd that stopped reading: the link is
        closed so the reconnect path takes over, and RobotdDisconnected is
        raised. Lines are far below the socket's low-water mark, so once
        select() reports writable the sendall() below does not block.
        """
        with self._state_lock:
            sock = self._sock
        if sock is None:
            raise RobotdDisconnected(f"not connected to robotd at {self.target}")
        line = (json.dumps(obj) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                _, writable, _ = select.select([], [sock], [], send_timeout)
            except (OSError, ValueError) as exc:  # closed under us
                raise RobotdDisconnected(str(exc)) from exc
            if not writable:
                logger.warning(
                    "robotd at %s has not drained its socket for %.2fs; dropping the link", self.target, send_timeout
                )
                self._close_socket()
                raise RobotdDisconnected(f"robotd at {self.target} stopped reading (send timed out)")
            try:
                sock.sendall(line)
            except OSError as exc:
                raise RobotdDisconnected(str(exc)) from exc

    def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        """Fire-and-forget continuous intent (robot.move/head/pose/mouth):
        no `id`, no reply expected. Never blocks longer than SEND_TIMEOUT_S.
        """
        self._write_line({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _raw_request(self, method: str, params: Optional[dict[str, Any]], timeout: float) -> Any:
        with self._id_lock:
            mid = self._next_id
            self._next_id += 1
            event = threading.Event()
            self._pending[mid] = event
        try:
            self._write_line({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, send_timeout=timeout)
            got = event.wait(timeout)
            if not got:
                raise RobotdTimeout(f"robotd request {method!r} (id={mid}) timed out after {timeout}s")
            reply = self._replies.pop(mid, None)
        finally:
            with self._id_lock:
                self._pending.pop(mid, None)
            self._replies.pop(mid, None)
        if reply is None:
            raise RobotdTimeout(f"robotd request {method!r} (id={mid}) got no reply")
        if "error" in reply and reply["error"] is not None:
            err = reply["error"]
            raise RobotdError(err.get("code", -1), err.get("message", "unknown error"))
        return reply.get("result")

    def request(self, method: str, params: Optional[dict[str, Any]] = None, timeout: float = 2.0) -> Any:
        """Discrete call (robot.do/sound/setMode/stop/...): incrementing id,
        blocks for a matching reply up to `timeout` seconds.
        """
        if not self.connected:
            raise RobotdDisconnected(f"not connected to robotd at {self.target}")
        return self._raw_request(method, params, timeout)
