"""duck_agent -- the on-duck daemon: SwarmLink agent side + robotd client.

See docs/swarmlink-protocol.md (agent behavior) and docs/robotd-api.md
(the JSON-RPC surface this package's robotd_client speaks).
"""

from __future__ import annotations

from .agent import DuckAgent
from .clock import Clock
from .robotd_client import RobotdClient, RobotdDisconnected, RobotdError, RobotdTimeout

__all__ = [
    "DuckAgent",
    "Clock",
    "RobotdClient",
    "RobotdDisconnected",
    "RobotdError",
    "RobotdTimeout",
]
