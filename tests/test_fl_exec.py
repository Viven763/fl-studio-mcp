import pytest

from fl_studio_mcp.utils.fl_exec import MAX_COMMAND_BYTES, build_code, exec_in_fl
from fl_studio_mcp.utils.midi_connection import MIDIConnection


def test_exec_returns_result(fake_send):
    send = fake_send(lambda a, p: {"success": True, "result": 42})
    assert exec_in_fl("result = 42", send=send) == 42
    assert send.calls[0][0] == "debug.exec"


def test_exec_raises_on_error_reply(fake_send):
    send = fake_send(lambda a, p: {"success": False, "error": "Error executing command: boom"})
    with pytest.raises(RuntimeError, match="boom"):
        exec_in_fl("x", send=send)


def test_args_injection_roundtrip():
    args = {"name": 'he said "hi"\n', "values": [1, 2.5, None], "nested": {"a": True}}
    scope: dict = {}
    exec(build_code("result = ARGS", args), scope)
    assert scope["result"] == args


def test_size_guard(fake_send):
    send = fake_send()
    with pytest.raises(ValueError, match="too large"):
        exec_in_fl("x = 1\n" * (MAX_COMMAND_BYTES // 4), send=send)
    assert send.calls == []


def test_sysex_chunk_guard():
    conn = MIDIConnection.__new__(MIDIConnection)  # no ports needed: the guard runs first
    with pytest.raises(ValueError, match="too large for SysEx"):
        conn._send_sysex({"action": "debug.exec", "params": {"code": "x" * 30000}}, 1.0)
