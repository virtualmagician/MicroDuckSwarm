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

from .errors import RpcError, METHOD_NOT_FOUND, PARSE_ERROR, INVALID_REQUEST
from .intentlog import IntentLog
from .state import DuckState

logger = logging.getLogger("mock_duck")

API_VERSION = 16
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

    raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")


async def _send_json(writer: asyncio.StreamWriter, obj: dict[str, Any], ctx: ServerContext) -> None:
    await ctx.apply_reply_delay()
    line = json.dumps(obj) + "\n"
    writer.write(line.encode("utf-8"))
    await writer.drain()


async def _stream_state(
    writer: asyncio.StreamWriter, hz: float, ctx: ServerContext, stop_event: asyncio.Event
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
                    await _send_json(writer, resp, ctx)
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
                        await _send_json(writer, resp, ctx)
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
                    await _send_json(writer, resp, ctx)
                except (ConnectionResetError, BrokenPipeError):
                    break
                _cancel_subscribe()
                subscribe_stop = asyncio.Event()
                subscribe_task = asyncio.create_task(_stream_state(writer, hz, ctx, subscribe_stop))
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
                        await _send_json(writer, resp, ctx)
                    except (ConnectionResetError, BrokenPipeError):
                        break
                continue

            if is_request:
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": result}
                try:
                    await _send_json(writer, resp, ctx)
                except (ConnectionResetError, BrokenPipeError):
                    break
            # else: notification -- no reply, per docs/robotd-api.md.
    finally:
        _cancel_subscribe()
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
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
        await asyncio.gather(*serve_tasks)
    except asyncio.CancelledError:
        pass
    finally:
        stop_event.set()
        physics_task.cancel()
        for t in serve_tasks:
            t.cancel()
        for s in servers:
            s.close()
        for s in servers:
            await s.wait_closed()
        intent_log.close()
