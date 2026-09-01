"""JSON-RPC 2.0 error codes used by mock_duck.

Reserved range (-32700..-32603) is the standard JSON-RPC 2.0 set; the
application codes (1, 14) are from docs/robotd-api.md. mock_duck does not
raise the application codes on its own -- it stays deliberately permissive
-- but a test (or an operator) can make the *next* call to a given method
fail with either one (or any other code) via the nonstandard
`mock.fail_next` debug request in server.py, so RobotdClient/agent
error-handling paths (swarmlink-protocol.md: "Any robotd error -> FAULT")
have something real to run against.
"""

from __future__ import annotations


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Application error codes (docs/robotd-api.md "Error codes")
BUSY = 1
PERMISSION_DENIED = 14
