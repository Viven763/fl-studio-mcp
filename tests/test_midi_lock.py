import threading
import time

from fl_studio_mcp.utils.midi_connection import MIDIConnection


def test_commands_are_sent_one_at_a_time(monkeypatch):
    conn = MIDIConnection.__new__(MIDIConnection)
    conn._command_lock = threading.Lock()
    conn._transport = "sysex"
    state = {"busy": 0, "max": 0}

    def send(command, timeout):
        state["busy"] += 1
        state["max"] = max(state["max"], state["busy"])
        time.sleep(0.02)
        state["busy"] -= 1
        return {"success": True, "action": command["action"]}

    monkeypatch.setattr(conn, "ensure_connected", lambda: None, raising=False)
    monkeypatch.setattr(conn, "_choose_transport", lambda: None, raising=False)
    monkeypatch.setattr(conn, "_send_sysex", send, raising=False)
    threads = [threading.Thread(target=conn.send_command, args=(f"a{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["max"] == 1
