"""CLI entry point: python -m duck_agent ...

Runs the on-duck daemon: connects to robotd, binds the SwarmLink agent
UDP port, and drives the state machine in agent.py until interrupted.
SIGINT (Ctrl-C) and SIGTERM (systemd stop) both go through
`DuckAgent.stop()`, which stops a duck in motion before the robotd link
is closed -- robotd is last-value-wins, and no agent means no deadman.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from .agent import DuckAgent


def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def install_sigterm_handler() -> None:
    """Route SIGTERM into the same KeyboardInterrupt path as Ctrl-C so
    `main()`'s finally block runs `agent.stop()` on a systemd stop.
    """
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

DEFAULT_AGENT_PORT = 47801
DEFAULT_MASTER_PORT = 47800


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="duck_agent",
        description="MicroDuckSwarm on-duck agent (SwarmLink agent side + robotd client).",
    )
    p.add_argument("--duck-id", required=True, help="This duck's id, e.g. duck-01.")
    p.add_argument(
        "--robotd",
        required=True,
        metavar="UNIX_PATH_OR_HOST:PORT",
        help="robotd target: a Unix socket path, or host:port for TCP (mock duck dev mode).",
    )
    p.add_argument("--shows-dir", required=True, type=Path, help="Directory .duckshow.json files are loaded from.")
    p.add_argument("--listen-port", type=int, default=DEFAULT_AGENT_PORT, help=f"UDP port to bind (default {DEFAULT_AGENT_PORT}).")
    p.add_argument("--master-port", type=int, default=DEFAULT_MASTER_PORT, help=f"Master's UDP port, used with --master-host (default {DEFAULT_MASTER_PORT}).")
    p.add_argument(
        "--master-host",
        default=None,
        help="Master's host/IP. Optional: if omitted, the master's address is learned from the "
        "source address of its first packet to this agent.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s duck_agent[%(name)s] %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    agent = DuckAgent(
        duck_id=args.duck_id,
        robotd_target=args.robotd,
        shows_dir=args.shows_dir,
        listen_port=args.listen_port,
        master_host=args.master_host,
        master_port=args.master_port,
    )
    install_sigterm_handler()
    agent.start()
    logging.getLogger("duck_agent").info(
        "%s: listening on UDP %d, robotd=%s, shows_dir=%s", args.duck_id, agent.bound_port, args.robotd, args.shows_dir
    )
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
