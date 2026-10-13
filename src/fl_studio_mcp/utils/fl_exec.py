"""Run Python inside FL Studio through the controller's debug.exec action.

The controller executes the code with FL's API modules in scope (channels,
general, midi, mixer, patterns, plugins, transport, ui; anything else can be
imported) and returns whatever the snippet assigns to `result`, which must be
JSON-serialisable. Values are handed over as JSON in `ARGS` rather than being
formatted into the code. A SysEx message carries about 18 KB of JSON in each
direction, so large data never goes through FL: notes etc. are written to the
project file instead.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

# 127 chunks x 192 base64 characters per SysEx message ~= 18 KB of JSON.
MAX_COMMAND_BYTES = 18_000


def build_code(code: str, args: dict | None = None) -> str:
    """Prefix `code` with `ARGS = <args>` decoded from JSON inside FL."""
    if args is None:
        return code
    blob = json.dumps(json.dumps(args))
    return f"import json as _json\nARGS = _json.loads({blob})\n{code}"


def exec_in_fl(
    code: str,
    *,
    timeout: float = 10.0,
    args: dict | None = None,
    send: Callable[..., dict] | None = None,
) -> Any:
    """Run `code` in FL and return its `result`.

    Raises ValueError when the command is too large for one SysEx message and
    RuntimeError when FL reports an error or doesn't answer in time. Never
    retried: FL may still run a command whose reply was lost.
    """
    full = build_code(code, args)
    size = len(json.dumps({"action": "debug.exec", "params": {"code": full}}).encode())
    if size > MAX_COMMAND_BYTES:
        raise ValueError(f"debug.exec command too large ({size} > {MAX_COMMAND_BYTES} bytes)")
    if send is None:
        from fl_studio_mcp.utils.connection import get_connection

        send = get_connection().send_command
    reply = send("debug.exec", {"code": full}, timeout=timeout)
    if "result" not in reply:
        raise RuntimeError(reply.get("error") or "FL did not answer")
    return reply["result"]
