"""MIDI-based connection to FL Studio.

Primary transport: SysEx. Commands and responses travel as JSON inside MIDI
system-exclusive messages over the virtual MIDI port, so nothing touches disk:

    F0 7D 'M' 'C' 'P' <dir> <seq_hi> <seq_lo> <chunk> <count> <base64 JSON> F7

dir 1 = request (host -> FL), 2 = response (FL -> host). On a loopback port
both sides hear their own messages too; the direction byte filters echoes.

Fallback transport (controller scripts that predate SysEx support): write the
command to a JSON file, send MIDI note 127 as a trigger, read the response file.
The transport is chosen once per connection with a harmless probe; after SysEx
is confirmed a lost reply is reported as a timeout instead of being retried over
files, so a command can never run twice.
"""

from __future__ import annotations

import base64
import json
import platform
import random
import threading
import time
from pathlib import Path
from typing import Any


def _get_fl_hardware_dir() -> Path:
    """Get the FL Studio Hardware scripts directory."""
    system = platform.system()

    if system in ("Darwin", "Windows"):
        base = Path.home() / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    else:
        # Linux fallback
        base = Path.home() / ".fl-studio" / "Settings"

    hardware_dir = base / "Hardware" / "FLStudioMCP"
    hardware_dir.mkdir(parents=True, exist_ok=True)
    return hardware_dir


class MIDIConnection:
    """MIDI-based connection to FL Studio.

    Communicates with FL Studio via:
    - JSON files for command/response data
    - MIDI trigger note to execute commands
    """

    # MIDI trigger note (same as in FL Studio controller script)
    TRIGGER_NOTE = 127
    SYSEX_HEADER = [0x7D, 0x4D, 0x43, 0x50]  # manufacturer 0x7D (non-commercial) + "MCP"
    DIR_REQUEST, DIR_RESPONSE = 1, 2
    SYSEX_CHUNK = 192

    def __init__(self) -> None:
        self._port = None
        self._in_port = None
        self._port_name: str | None = None
        self._connected = False
        self._error: str | None = None
        self._transport: str | None = None  # "sysex" or "file", decided on first command
        # Several clients (the MCP server, scripts) can share the loopback port: start each
        # at a random sequence number and only accept replies to our own pending requests.
        self._seq = random.randrange(16384)
        self._pending: set[int] = set()
        self._chunks: dict[int, dict[int, bytes]] = {}
        self._replies: dict[int, dict[str, Any]] = {}
        self._cond = threading.Condition()
        self._lock = threading.Lock()
        # One command in flight at a time: FL handles them one by one anyway, and a
        # command arriving while FL is saving the project can stall FL for minutes.
        self._command_lock = threading.Lock()

        # File paths for JSON communication
        self._hardware_dir = _get_fl_hardware_dir()
        self._command_file = self._hardware_dir / "mcp_command.json"
        self._response_file = self._hardware_dir / "mcp_response.json"

    @property
    def is_connected(self) -> bool:
        """Check if connected to FL Studio."""
        return self._connected and self._port is not None

    @property
    def connection_error(self) -> str | None:
        """Get the last connection error, if any."""
        return self._error

    def connect(self) -> bool:
        """Attempt to connect to FL Studio via MIDI.

        Returns True if connection successful, False otherwise.
        """
        if self._connected and self._port is not None:
            return True

        try:
            import mido
        except ImportError:
            self._error = (
                "mido library not installed. Install with: pip install mido python-rtmidi"
            )
            return False

        # Find available MIDI output ports
        try:
            output_ports = mido.get_output_names()
        except Exception as e:
            self._error = f"Failed to get MIDI ports: {e}"
            return False

        if not output_ports:
            self._error = (
                "No MIDI output ports found. On Mac, enable IAC Driver in Audio MIDI Setup."
            )
            return False

        # Look for IAC Driver (Mac) or other virtual MIDI ports
        target_port = None
        for port_name in output_ports:
            # Prefer IAC Driver on Mac
            if "IAC" in port_name:
                target_port = port_name
                break
            # Also accept loopMIDI on Windows or any port with "FL" in name
            if "loopMIDI" in port_name or "FL" in port_name.upper():
                target_port = port_name
                break

        # If no specific port found, use the first available
        if target_port is None:
            target_port = output_ports[0]

        try:
            self._port = mido.open_output(target_port)
            self._port_name = target_port
            self._connected = True
            self._error = None
        except Exception as e:
            self._error = f"Failed to open MIDI port '{target_port}': {e}"
            return False
        try:  # the reply channel; without it only the file transport works
            if target_port in mido.get_input_names():
                self._in_port = mido.open_input(target_port, callback=self._on_midi)
        except Exception:
            self._in_port = None
        return True

    def _on_midi(self, msg) -> None:
        """Collect SysEx response chunks (runs on mido's callback thread)."""
        if msg.type != "sysex":
            return
        data = list(msg.data)
        if data[:4] != self.SYSEX_HEADER or len(data) < 9 or data[4] != self.DIR_RESPONSE:
            return
        seq = (data[5] << 7) | data[6]
        idx, count = data[7], data[8]
        with self._cond:
            if seq not in self._pending:
                return  # a reply to another client
            parts = self._chunks.setdefault(seq, {})
            parts[idx] = bytes(data[9:])
            if len(parts) < count:
                return
            raw = b"".join(parts[i] for i in range(count))
            del self._chunks[seq]
            try:
                self._replies[seq] = json.loads(base64.b64decode(raw).decode("utf-8"))
            except Exception as e:
                self._replies[seq] = {"success": False, "error": f"Bad SysEx reply: {e}"}
            self._cond.notify_all()

    def _send_sysex(self, command: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        """Send one command over SysEx; None if no reply arrived in time."""
        import mido

        payload = base64.b64encode(json.dumps(command).encode("utf-8"))
        size = self.SYSEX_CHUNK
        chunks = [payload[i:i + size] for i in range(0, len(payload), size)]
        if len(chunks) > 127:  # chunk index/count are single 7-bit bytes
            raise ValueError(
                f"command too large for SysEx ({len(payload)} base64 bytes > {127 * size})"
            )
        with self._lock:
            self._seq = (self._seq + 1) % 16384
            seq = self._seq
        with self._cond:
            self._pending.add(seq)
            self._replies.pop(seq, None)
        for idx, chunk in enumerate(chunks or [b""]):
            meta = [self.DIR_REQUEST, (seq >> 7) & 0x7F, seq & 0x7F, idx, len(chunks)]
            head = self.SYSEX_HEADER + meta
            self._port.send(mido.Message("sysex", data=head + list(chunk)))
        deadline = time.time() + timeout
        with self._cond:
            try:
                while seq not in self._replies:
                    left = deadline - time.time()
                    if left <= 0:
                        self._chunks.pop(seq, None)
                        return None
                    self._cond.wait(left)
                return self._replies.pop(seq)
            finally:
                self._pending.discard(seq)

    def _choose_transport(self) -> None:
        """Probe once: does the controller script answer over SysEx?"""
        if self._transport is not None:
            return
        reply = None
        if self._in_port is not None:
            reply = self._send_sysex({"action": "channels.getCount", "params": {}}, timeout=1.0)
        self._transport = "sysex" if reply is not None else "file"

    @property
    def transport(self) -> str | None:
        return self._transport

    def disconnect(self) -> None:
        """Close the MIDI connection."""
        if self._in_port is not None:
            try:
                self._in_port.close()
            except Exception:
                pass
            self._in_port = None
        self._transport = None
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None
        self._connected = False
        self._port_name = None

    def ensure_connected(self) -> None:
        """Ensure connection to FL Studio is active. Raises RuntimeError if not."""
        if not self.is_connected:
            if not self.connect():
                raise RuntimeError(
                    self._error or "Failed to connect to FL Studio via MIDI"
                )

    def send_command(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        """Send a command to FL Studio and wait for response.

        Args:
            action: The command action (e.g., "transport.start", "mixer.setTrackVolume")
            params: Optional parameters for the command
            timeout: Maximum time to wait for response in seconds

        Returns:
            Response dictionary from FL Studio

        Raises:
            RuntimeError: If not connected or command fails
        """
        self.ensure_connected()

        # Prepare command
        command = {
            "action": action,
            "params": params or {},
        }

        with self._command_lock:
            return self._send_locked(command, timeout)

    def _send_locked(self, command: dict[str, Any], timeout: float) -> dict[str, Any]:
        self._choose_transport()
        if self._transport == "sysex":
            reply = self._send_sysex(command, timeout)
            if reply is None:
                return {
                    "success": False,
                    "error": f"Timeout waiting for FL Studio SysEx reply after {timeout}s.",
                }
            return reply

        # Write command to file
        try:
            self._command_file.write_text(json.dumps(command, indent=2))
        except Exception as e:
            return {"success": False, "error": f"Failed to write command file: {e}"}

        # Clear old response file
        if self._response_file.exists():
            try:
                self._response_file.unlink()
            except Exception:
                pass

        # Send MIDI trigger
        try:
            import mido
            trigger_msg = mido.Message("note_on", note=self.TRIGGER_NOTE, velocity=127)
            self._port.send(trigger_msg)
        except Exception as e:
            return {"success": False, "error": f"Failed to send MIDI trigger: {e}"}

        # Wait for response
        return self._wait_for_response(timeout)

    def _wait_for_response(self, timeout: float) -> dict[str, Any]:
        """Wait for response file to appear and read it.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            Response dictionary or error dict if timeout
        """
        start_time = time.time()
        poll_interval = 0.02  # 20ms between checks

        while time.time() - start_time < timeout:
            if self._response_file.exists():
                try:
                    response_text = self._response_file.read_text()
                    response = json.loads(response_text)

                    # Clean up response file
                    try:
                        self._response_file.unlink()
                    except Exception:
                        pass

                    return response
                except json.JSONDecodeError as e:
                    return {"success": False, "error": f"Invalid JSON in response: {e}"}
                except Exception as e:
                    return {"success": False, "error": f"Failed to read response: {e}"}

            time.sleep(poll_interval)

        return {
            "success": False,
            "error": (
                f"Timeout waiting for FL Studio response after {timeout}s. "
                "Make sure FL Studio is running and the MCP controller is enabled "
                "in MIDI Settings."
            ),
        }

    def get_status(self) -> dict[str, Any]:
        """Get connection status information."""
        try:
            import mido
            output_ports = mido.get_output_names()
        except Exception:
            output_ports = []

        return {
            "connected": self.is_connected,
            "transport": self._transport,
            "port_name": self._port_name,
            "available_ports": output_ports,
            "command_file": str(self._command_file),
            "response_file": str(self._response_file),
            "error": self._error,
        }


# Global connection instance
_connection: MIDIConnection | None = None


def get_connection() -> MIDIConnection:
    """Get the global MIDI connection instance."""
    global _connection
    if _connection is None:
        _connection = MIDIConnection()
    return _connection


def reset_connection() -> None:
    """Reset the connection state to allow reconnection attempts."""
    global _connection
    if _connection is not None:
        _connection.disconnect()
        _connection = None
