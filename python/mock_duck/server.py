"""Protocol-faithful mock robotd.

Implements the JSON-RPC 2.0 / NDJSON surface described in
docs/robotd-api.md over a Unix domain socket and/or TCP, so duck-agent
and showmaster/SwarmLink development can proceed without hardware.

Every parsed inbound message is appended to a JSONL intent log
(see intentlog.py) -- that log is what end-to-end tests assert against.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Optional

from .errors import RpcError, METHOD_NOT_FOUND, PARSE_ERROR, INVALID_REQUEST, INVALID_PARAMS, BUSY
from .intentlog import IntentLog
from .state import DuckState

logger = logging.getLogger("mock_duck")

API_VERSION = 17
DAEMON_VERSION = "mock-0.1"

JOINT_COUNT = 15  # docs/robotd-api.md: JOINT_NAMES, 15 (left leg x5, neck/head/mouth x5, right leg x5)

PHYSICS_HZ = 50.0
DEFAULT_SUBSCRIBE_HZ = 20.0


class ServerContext:
    """Shared state + config for all connections served by one mock duck instance."""

    def __init__(
        self,
        name: str,
        intent_log: IntentLog,
        latency_ms: float = 0.0,
        jitter_ms: float = 0.0,
    ):
        self.name = name
        self.state = DuckState(name=name)
        self.intent_log = intent_log
        self.latency_ms = latency_ms
        self.jitter_ms = jitter_ms
        # Live connections, keyed by the asyncio.Task running handle_connection
        # for them -> their StreamWriter. run_server's shutdown closes these
        # explicitly and awaits the tasks before Server.wait_closed(), which
        # on Python >= 3.12.1 otherwise blocks forever while any client (a
        # reconnecting duck-agent) stays attached. See F19.
        self.connections: dict[asyncio.Task, asyncio.StreamWriter] = {}
        # method -> one-shot RpcError to return instead of dispatching, set by
        # the nonstandard `mock.fail_next` debug request (see _dispatch) so
        # tests can drive a real JSON-RPC application-error reply (BUSY /
        # PERMISSION_DENIED) through the mock -- errors.py notes the mock is
        # otherwise deliberately permissive and never raises these itself.
        self.pending_errors: dict[str, RpcError] = {}

    async def apply_reply_delay(self) -> None:
        delay_ms = self.latency_ms
        if self.jitter_ms:
            delay_ms += random.uniform(0.0, self.jitter_ms)
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)


async def physics_loop(ctx: ServerContext, stop_event: asyncio.Event) -> None:
    """Background dead-reckoning integration of the mock's x/y/heading."""
    period = 1.0 / PHYSICS_HZ
    last_ns = time.monotonic_ns()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=period)
        except asyncio.TimeoutError:
            pass
        now_ns = time.monotonic_ns()
        dt = (now_ns - last_ns) / 1e9
        last_ns = now_ns
        ctx.state.integrate(dt)


def _dispatch(method: str, params: dict[str, Any], ctx: ServerContext) -> Any:
    """Handle one discrete request/notification method. Returns the JSON-RPC result.

    Raises RpcError for unknown methods (-32601 per docs/robotd-api.md).
    """
    state = ctx.state

    if method == "hello":
        return {
            "api_version": API_VERSION,
            "daemon_version": DAEMON_VERSION,
            "revision": "mock",
        }

    if method == "robot.move":
        state.set_move(params)
        return {}

    if method == "robot.head":
        state.set_head(params)
        return {}

    if method == "robot.pose":
        state.set_pose(params)
        return {}

    if method == "robot.mouth":
        state.set_mouth(params)
        return {}

    if method == "robot.look":
        return {"head": state.head_snapshot(), "clamped": False}

    if method == "robot.do":
        state.last_do = params.get("skill")
        return {}

    if method == "robot.sound":
        state.last_sound = dict(params)
        return {}

    if method == "robot.stop":
        state.stop()
        return {}

    if method == "robot.enable":
        state.enabled = bool(params.get("on", True))
        return {}

    if method == "robot.init":
        return {}

    if method == "robot.relax":
        return {}

    if method == "robot.mode":
        # docs/robotd-api.md: "current mode" -- the result IS the mode string.
        return state.mode

    if method == "robot.setMode":
        state.mode = str(params.get("mode", state.mode))
        return {}

    if method == "robot.health":
        return state.health_snapshot()

    if method == "robot.safeToRestart":
        return True

    if method in ("robot.theremin", "robot.chorale"):
        return {"accepted": True}

    if method == "robot.shutdown":
        return {}

    if method == "mock.state":
        # Nonstandard, mock-only debug request (see docs at top of file).
        return state.mock_state_snapshot()

    if method == "mock.fail_next":
        # Nonstandard, mock-only debug request: make the *next* call to
        # params["method"] fail with a JSON-RPC error reply (application
        # code by default; any code/message can be supplied) instead of
        # being dispatched normally. One-shot -- consumed by the first
        # matching request or notification after this call.
        target = params.get("method")
        if not target or not isinstance(target, str):
            raise RpcError(INVALID_PARAMS, "mock.fail_next requires a string 'method'")
        code = int(params.get("code", BUSY))
        message = str(params.get("message", "mock.fail_next injected error"))
        ctx.pending_errors[target] = RpcError(code, message)
        return {}

    raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")


async def _send_json(
    writer: asyncio.StreamWriter, obj: dict[str, Any], ctx: ServerContext, write_lock: asyncio.Lock
) -> None:
    await ctx.apply_reply_delay()
    line = json.dumps(obj) + "\n"
    # Serialize against the background _stream_state task: two concurrently
    # pending drain() calls on the same StreamWriter is a real hazard on
    # early 3.10.x point releases (predates the CPython gh-74116 fix). See
    # F22.
    async with write_lock:
        writer.write(line.encode("utf-8"))
        await writer.drain()


async def _stream_state(
    writer: asyncio.StreamWriter,
    hz: float,
    ctx: ServerContext,
    stop_event: asyncio.Event,
    write_lock: asyncio.Lock,
) -> None:
    hz = hz if hz and hz > 0 else DEFAULT_SUBSCRIBE_HZ
    period = 1.0 / hz
    try:
        while not stop_event.is_set():
            notification = {
                "jsonrpc": "2.0",
                "method": "robot.state",
                "params": {
                    "joints": [0.0] * JOINT_COUNT,
                    "targets": [0.0] * JOINT_COUNT,
                },
            }
            line = json.dumps(notification) + "\n"
            async with write_lock:
                writer.write(line.encode("utf-8"))
                await writer.drain()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=period)
            except asyncio.TimeoutError:
                pass
    except (ConnectionResetError, BrokenPipeError):
        pass


async def handle_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ctx: ServerContext, peer_desc: str
) -> None:
    logger.info("connection from %s", peer_desc)
    subscribe_task: Optional[asyncio.Task] = None
    subscribe_stop: Optional[asyncio.Event] = None
    write_lock = asyncio.Lock()

    # Register this connection so run_server's shutdown can close it and
    # wait for this task to actually finish teardown, instead of hanging in
    # Server.wait_closed() for as long as the peer stays connected. See F19.
    task = asyncio.current_task()
    if task is not None:
        ctx.connections[task] = writer

    def _cancel_subscribe() -> None:
        if subscribe_stop is not None:
            subscribe_stop.set()
        if subscribe_task is not None:
            subscribe_task.cancel()

    try:
        while True:
            try:
                line = await reader.readline()
            except (ConnectionResetError, asyncio.IncompleteReadError):
                break
            if not line:
                break  # EOF: client disconnected
            stripped = line.strip()
            if not stripped:
                continue

            try:
                msg = json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": PARSE_ERROR, "message": "Parse error"},
                }
                try:
                    await _send_json(writer, resp, ctx, write_lock)
                except (ConnectionResetError, BrokenPipeError):
                    break
                continue  # malformed line: reply with -32700, keep the connection alive

            # Log every successfully-parsed received message, request or notification.
            ctx.intent_log.write_message(msg)

            if not isinstance(msg, dict) or "method" not in msg:
                if isinstance(msg, dict) and "id" in msg:
                    resp = {
                        "jsonrpc": "2.0",
                        "id": msg.get("id"),
                        "error": {"code": INVALID_REQUEST, "message": "Invalid Request"},
                    }
                    try:
                        await _send_json(writer, resp, ctx, write_lock)
                    except (ConnectionResetError, BrokenPipeError):
                        break
                continue

            method = msg.get("method")
            params = msg.get("params") or {}
            is_request = "id" in msg
            msg_id = msg.get("id")

            if method == "robot.subscribe" and is_request:
                hz = params.get("hz", DEFAULT_SUBSCRIBE_HZ)
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"accepted": True, "hz": hz}}
                try:
                    await _send_json(writer, resp, ctx, write_lock)
                except (ConnectionResetError, BrokenPipeError):
                    break
                _cancel_subscribe()
                subscribe_stop = asyncio.Event()
                subscribe_task = asyncio.create_task(
                    _stream_state(writer, hz, ctx, subscribe_stop, write_lock)
                )
                continue

            injected = ctx.pending_errors.pop(method, None)
            if injected is not None:
                # mock.fail_next fired for this method: reply with the
                # injected JSON-RPC error instead of dispatching.
                if is_request:
                    resp = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": injected.code, "message": injected.message},
                    }
                    try:
                        await _send_json(writer, resp, ctx, write_lock)
                    except (ConnectionResetError, BrokenPipeError):
                        break
                continue

            try:
                result = _dispatch(method, params, ctx)
            except RpcError as exc:
                if is_request:
                    resp = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": exc.code, "message": exc.message},
                    }
                    try:
                        await _send_json(writer, resp, ctx, write_lock)
                    except (ConnectionResetError, BrokenPipeError):
                        break
                continue

            if is_request:
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": result}
                try:
                    await _send_json(writer, resp, ctx, write_lock)
                except (ConnectionResetError, BrokenPipeError):
                    break
            # else: notification -- no reply, per docs/robotd-api.md.
    finally:
        _cancel_subscribe()
        if subscribe_task is not None:
            # F22: observe whatever the cancelled task raised instead of
            # silently dropping it (Task.cancel() suppresses the "exception
            # was never retrieved" warning even for an already-crashed task,
            # so without this await, a real bug in _stream_state would be
            # invisible).
            try:
                await subscribe_task
            except asyncio.CancelledError:
                pass
            except (ConnectionResetError, BrokenPipeError):
                pass
            except Exception:
                logger.exception("subscribe stream for %s failed", peer_desc)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        if task is not None:
            ctx.connections.pop(task, None)
        logger.info("connection closed %s", peer_desc)


async def run_server(
    name: str,
    tcp_addr: Optional[tuple[str, int]],
    unix_path: Optional[str],
    log_path: str,
    latency_ms: float = 0.0,
    jitter_ms: float = 0.0,
) -> None:
    if not tcp_addr and not unix_path:
        raise ValueError("mock_duck needs at least one of --tcp or --unix to listen on")

    intent_log = IntentLog(log_path)
    ctx = ServerContext(name=name, intent_log=intent_log, latency_ms=latency_ms, jitter_ms=jitter_ms)
    stop_event = asyncio.Event()

    servers: list[asyncio.base_events.Server] = []

    if tcp_addr:
        host, port = tcp_addr

        async def _tcp_handler(reader, writer, host=host):
            peer = writer.get_extra_info("peername")
            await handle_connection(reader, writer, ctx, f"tcp://{peer}")

        tcp_server = await asyncio.start_server(_tcp_handler, host=host, port=port)
        servers.append(tcp_server)
        logger.info("mock_duck %r listening on tcp://%s:%d", name, host, port)

    if unix_path:
        # Remove a stale socket file from a previous run, if any.
        try:
            os.unlink(unix_path)
        except FileNotFoundError:
            pass

        async def _unix_handler(reader, writer):
            await handle_connection(reader, writer, ctx, f"unix://{unix_path}")

        unix_server = await asyncio.start_unix_server(_unix_handler, path=unix_path)
        servers.append(unix_server)
        logger.info("mock_duck %r listening on unix://%s", name, unix_path)

    logger.info("intent log -> %s", Path(log_path).resolve())

    physics_task = asyncio.create_task(physics_loop(ctx, stop_event))
    serve_tasks = [asyncio.create_task(s.serve_forever()) for s in servers]

    try:
        # Deliberately asyncio.wait(), not asyncio.gather(): unlike
        # gather(), wait() does not cancel its member tasks when the
        # *awaiting* task itself is cancelled (Ctrl-C / asyncio.run
        # shutdown / a parent task's .cancel()). That distinction matters
        # here: Server.serve_forever() itself, on cancellation,
        # internally runs `self.close(); await self.wait_closed()` (see
        # asyncio.base_events.Server.serve_forever in the stdlib), and
        # wait_closed() blocks on Python >= 3.12.1 until every connection
        # ever accepted through that server has closed. If our own
        # cancellation cascaded straight into serve_tasks the way
        # gather() would, it would hit that internal wait_closed() call
        # *before* the finally block below ever runs -- and nothing else
        # would ever close a still-attached client connection (a
        # duck-agent reconnects forever), deadlocking run_server
        # indefinitely. wait() leaves serve_tasks running untouched, so
        # the finally block gets to close live connections *first*, in
        # the order that actually lets everything unwind. See F19.
        done, _pending = await asyncio.wait(serve_tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    raise exc
    except asyncio.CancelledError:
        pass
    finally:
        stop_event.set()
        physics_task.cancel()
        # Stop accepting new connections before touching existing ones.
        for s in servers:
            s.close()
        # Close every live client connection so the handler tasks (and,
        # via Server._detach(), Server.wait_closed() -- both our own call
        # below and serve_forever()'s internal one triggered by
        # Server.close() above) can actually finish, instead of waiting
        # forever for a duck-agent that keeps reconnecting. See F19.
        live = list(ctx.connections.items())
        for _conn_task, w in live:
            try:
                w.close()
            except Exception:
                pass
        if live:
            await asyncio.gather(*(t for t, _w in live), return_exceptions=True)
        for t in serve_tasks:
            t.cancel()
        for t in serve_tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        for s in servers:
            await s.wait_closed()
        try:
            await physics_task
        except asyncio.CancelledError:
            pass
        intent_log.close()
