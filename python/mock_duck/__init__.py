"""mock_duck: a protocol-faithful mock robotd for MicroDuckSwarm development.

Serves docs/robotd-api.md's JSON-RPC 2.0 / NDJSON surface over a Unix
domain socket and/or TCP so duck-agent, showmaster and SwarmLink can be
developed and tested without hardware. See server.py for the
implementation and __main__.py for the CLI.
"""

from .server import run_server  # noqa: F401

__version__ = "0.1"
