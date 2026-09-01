"""CLI entry point: python -m mock_duck ...

Example:
    python -m mock_duck --name duck-01 --tcp 127.0.0.1:7010 \\
        --unix /tmp/duck01.sock --log ./duck-01.intents.jsonl --latency-ms 5
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Optional

from .server import run_server

DEFAULT_TCP_HOST = "127.0.0.1"
DEFAULT_TCP_PORT = 7010


def _parse_tcp(value: str) -> Optional[tuple[str, int]]:
    """Parse a HOST:PORT string. Empty string disables TCP."""
    value = value.strip()
    if not value:
        return None
    if ":" not in value:
        raise argparse.ArgumentTypeError(f"--tcp must be HOST:PORT, got {value!r}")
    host, _, port_s = value.rpartition(":")
    try:
        port = int(port_s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--tcp port must be an integer, got {port_s!r}")
    return (host or DEFAULT_TCP_HOST, port)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mock_duck",
        description="Protocol-faithful mock robotd (docs/robotd-api.md) for MicroDuckSwarm dev.",
    )
    p.add_argument("--name", default="duck-01", help="Duck id, used in log messages and default log filename.")
    p.add_argument(
        "--tcp",
        default=f"{DEFAULT_TCP_HOST}:{DEFAULT_TCP_PORT}",
        help=(
            "HOST:PORT to serve JSON-RPC/NDJSON over TCP "
            f"(default {DEFAULT_TCP_HOST}:{DEFAULT_TCP_PORT}). Pass an empty string to disable TCP."
        ),
    )
    p.add_argument("--unix", default=None, help="Unix domain socket path to also serve on (optional).")
    p.add_argument("--log", default=None, help="Intent log JSONL path (default ./mock-duck-<name>.intents.jsonl).")
    p.add_argument("--latency-ms", type=float, default=0.0, help="Fixed delay before every reply/notification.")
    p.add_argument("--jitter-ms", type=float, default=0.0, help="Extra uniform-random delay on top of --latency-ms.")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return p


async def _amain(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s mock_duck[%(process)d] %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    tcp_addr = _parse_tcp(args.tcp)
    unix_path = args.unix
    log_path = args.log or f"./mock-duck-{args.name}.intents.jsonl"

    if not tcp_addr and not unix_path:
        print("mock_duck: need at least one of --tcp or --unix to listen on", file=sys.stderr)
        return 2

    logging.getLogger("mock_duck").info(
        "starting mock_duck name=%s tcp=%s unix=%s log=%s latency_ms=%s jitter_ms=%s",
        args.name,
        tcp_addr,
        unix_path,
        log_path,
        args.latency_ms,
        args.jitter_ms,
    )

    try:
        await run_server(
            name=args.name,
            tcp_addr=tcp_addr,
            unix_path=unix_path,
            log_path=log_path,
            latency_ms=args.latency_ms,
            jitter_ms=args.jitter_ms,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    try:
        return asyncio.run(_amain(argv))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
