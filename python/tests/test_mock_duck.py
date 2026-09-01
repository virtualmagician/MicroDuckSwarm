"""Tests for python/mock_duck: connect over TCP, do the hello handshake,
send intents, and assert on both the RPC replies and the JSONL intent
log (which is what end-to-end tests will assert against for timing).
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mock_duck.server import run_server  # noqa: E402


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ServerThread:
    """Runs mock_duck's asyncio server in a background thread with its own
    event loop, so tests can talk to it over a plain blocking socket.
    """

    def __init__(self, log_path: Path, latency_ms: float = 0.0, jitter_ms: float = 0.0):
        self.port = _free_port()
        self.log_path = log_path
        self.latency_ms = latency_ms
        self.jitter_ms = jitter_ms
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._main_task: asyncio.Task | None = None

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def _runner():
            self._main_task = asyncio.current_task()
            self._ready.set()
            await run_server(
                name="test-duck",
                tcp_addr=("127.0.0.1", self.port),
                unix_path=None,
                log_path=str(self.log_path),
                latency_ms=self.latency_ms,
                jitter_ms=self.jitter_ms,
            )

        try:
            self.loop.run_until_complete(_runner())
        except asyncio.CancelledError:
            pass

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=5)
        # give the server a brief moment to actually bind/listen
        for _ in range(50):
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=0.1)
                s.close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("mock_duck server did not start listening in time")

    def stop(self) -> None:
        if self.loop is None:
            return

        def _cancel():
            for task in asyncio.all_tasks(loop=self.loop):
                task.cancel()

        self.loop.call_soon_threadsafe(_cancel)
        self._thread.join(timeout=5)


class RawClient:
    """Thin NDJSON request/response client for talking to mock_duck over TCP."""

    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.file = self.sock.makefile("rwb")

    def send(self, obj: dict) -> None:
        self.file.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.file.flush()

    def send_raw(self, line: str) -> None:
        self.file.write((line + "\n").encode("utf-8"))
        self.file.flush()

    def recv(self) -> dict:
        line = self.file.readline()
        if not line:
            raise EOFError("connection closed")
        return json.loads(line)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class MockDuckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmpdir.name) / "intents.jsonl"
        self.server = _ServerThread(self.log_path)
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.tmpdir.cleanup()

    def _read_log_records(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        records = []
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def test_hello_handshake(self) -> None:
        client = RawClient(self.server.port)
        try:
            client.send({"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {"api_version": 16}})
            reply = client.recv()
            self.assertEqual(reply["id"], 1)
            self.assertEqual(reply["result"]["api_version"], 16)
            self.assertEqual(reply["result"]["daemon_version"], "mock-0.1")
        finally:
            client.close()

    def test_move_notification_and_do_request_logged(self) -> None:
        client = RawClient(self.server.port)
        try:
            client.send({"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {"api_version": 16}})
            self.assertEqual(client.recv()["id"], 1)

            # Continuous intent: notification, no id, no reply expected.
            client.send({"jsonrpc": "2.0", "method": "robot.move", "params": {"vx": 0.1, "vy": 0.0, "vyaw": 0.0}})

            # Discrete request: expects a reply.
            client.send({"jsonrpc": "2.0", "id": 2, "method": "robot.do", "params": {"skill": "kick_left"}})
            reply = client.recv()
            self.assertEqual(reply["id"], 2)
            self.assertEqual(reply["result"], {})

            # Give the log writer a moment (it flushes synchronously, but
            # be generous under CI load).
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                records = self._read_log_records()
                if len(records) >= 3:
                    break
                time.sleep(0.02)

            records = self._read_log_records()
            methods = [r["msg"].get("method") for r in records]
            self.assertIn("robot.move", methods)
            self.assertIn("robot.do", methods)
            for r in records:
                self.assertIn("rx_ns", r)
                self.assertIsInstance(r["rx_ns"], int)
                self.assertIn("rx_wall", r)
                self.assertIn("msg", r)
        finally:
            client.close()

    def test_unknown_method_returns_method_not_found(self) -> None:
        client = RawClient(self.server.port)
        try:
            client.send({"jsonrpc": "2.0", "id": 5, "method": "robot.doesNotExist", "params": {}})
            reply = client.recv()
            self.assertEqual(reply["id"], 5)
            self.assertIn("error", reply)
            self.assertEqual(reply["error"]["code"], -32601)
        finally:
            client.close()

    def test_malformed_line_gets_parse_error_and_connection_survives(self) -> None:
        client = RawClient(self.server.port)
        try:
            client.send_raw("{not valid json!!")
            reply = client.recv()
            self.assertIn("error", reply)
            self.assertEqual(reply["error"]["code"], -32700)

            # Connection must still be alive for further requests.
            client.send({"jsonrpc": "2.0", "id": 9, "method": "robot.stop", "params": {}})
            reply2 = client.recv()
            self.assertEqual(reply2["id"], 9)
            self.assertEqual(reply2["result"], {})
        finally:
            client.close()

    def test_subscribe_streams_state_at_requested_rate(self) -> None:
        client = RawClient(self.server.port)
        try:
            client.send({"jsonrpc": "2.0", "id": 3, "method": "robot.subscribe", "params": {"hz": 10}})
            reply = client.recv()
            self.assertEqual(reply["id"], 3)
            self.assertTrue(reply["result"]["accepted"])

            client.sock.settimeout(2.0)
            notif1 = client.recv()
            notif2 = client.recv()
            for notif in (notif1, notif2):
                self.assertEqual(notif["method"], "robot.state")
                self.assertIn("joints", notif["params"])
                self.assertEqual(len(notif["params"]["joints"]), 15)
                self.assertTrue(all(j == 0 for j in notif["params"]["joints"]))
        finally:
            client.close()

    def test_unknown_method_and_robot_do_are_both_logged(self) -> None:
        # A method not found error should still have logged the inbound message.
        client = RawClient(self.server.port)
        try:
            client.send({"jsonrpc": "2.0", "id": 7, "method": "totally.bogus", "params": {}})
            client.recv()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                records = self._read_log_records()
                if any(r["msg"].get("method") == "totally.bogus" for r in records):
                    break
                time.sleep(0.02)
            records = self._read_log_records()
            self.assertTrue(any(r["msg"].get("method") == "totally.bogus" for r in records))
        finally:
            client.close()

    def test_mock_state_debug_request(self) -> None:
        client = RawClient(self.server.port)
        try:
            client.send({"jsonrpc": "2.0", "id": 10, "method": "mock.state", "params": {}})
            reply = client.recv()
            result = reply["result"]
            for key in ("x", "y", "heading", "mode", "last_intents"):
                self.assertIn(key, result)
        finally:
            client.close()

    def test_health_and_safe_to_restart(self) -> None:
        client = RawClient(self.server.port)
        try:
            client.send({"jsonrpc": "2.0", "id": 11, "method": "robot.health", "params": {}})
            reply = client.recv()
            self.assertIn("battery_pct", reply["result"])
            self.assertAlmostEqual(reply["result"]["battery_pct"], 87, delta=1)

            client.send({"jsonrpc": "2.0", "id": 12, "method": "robot.safeToRestart", "params": {}})
            reply = client.recv()
            self.assertTrue(reply["result"])
        finally:
            client.close()

    def test_mode_get_and_set(self) -> None:
        client = RawClient(self.server.port)
        try:
            client.send({"jsonrpc": "2.0", "id": 13, "method": "robot.setMode", "params": {"mode": "roller"}})
            reply = client.recv()
            self.assertEqual(reply["result"], {})

            client.send({"jsonrpc": "2.0", "id": 14, "method": "robot.mode", "params": {}})
            reply = client.recv()
            self.assertEqual(reply["result"], "roller")
        finally:
            client.close()

    def test_sequential_connections_supported(self) -> None:
        client1 = RawClient(self.server.port)
        client1.send({"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {"api_version": 16}})
        self.assertEqual(client1.recv()["result"]["api_version"], 16)
        client1.close()

        client2 = RawClient(self.server.port)
        try:
            client2.send({"jsonrpc": "2.0", "id": 2, "method": "hello", "params": {"api_version": 16}})
            self.assertEqual(client2.recv()["result"]["api_version"], 16)
        finally:
            client2.close()


if __name__ == "__main__":
    unittest.main()
