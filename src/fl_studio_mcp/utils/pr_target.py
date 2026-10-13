"""Verify which channel AND pattern the piano roll is showing before writing to it.

The piano-roll script only sees "the notes in the open piano roll" - it can't
tell which channel or pattern that is, and neither can the window caption in
FL 2026. So the controller API is used as the source of truth:

* Signature check: the controller can read a channel's step grid in the
  *current* pattern. Notes that start exactly on a step show up there, so the
  grid-aligned notes in the piano roll must equal that channel's grid.
* Probe check (empty or all-off-grid parts): drop one marker note (MIDI 127,
  which no part uses) on a free step, confirm through the controller that it
  appeared on exactly that channel in the current pattern, then delete only
  that marker.

Nothing is ever cleared here; callers should only write after verify() passes.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

PROBE_PITCH = 127
SCRIPTS = Path(os.path.expanduser("~/Documents/Image-Line/FL Studio/Settings/Piano roll scripts"))


class TargetError(RuntimeError):
    pass


class PianoRollTarget:
    def __init__(self, conn, trigger):
        self.conn = conn
        self.trigger = trigger

    # -- controller side -------------------------------------------------
    def _exec(self, code: str):
        r = self.conn.send_command("debug.exec", {"code": code}, timeout=30)
        if r.get("error"):
            raise TargetError(r["error"])
        return r.get("result")

    def grid(self, channel: int, steps: int) -> set[int]:
        return set(self._exec(
            f"result = [s for s in range({steps}) if channels.getGridBit({channel}, s, True)]"
        ))

    def open(self, channel: int, pattern: int) -> None:
        """Select pattern + channel and (re)open the piano roll on them."""
        ok = self._exec(
            f"patterns.jumpToPattern({pattern})\n"
            "ui.hideWindow(midi.widPianoRoll)\n"
            f"channels.selectOneChannel({channel}, True)\n"
            "ui.showWindow(midi.widPianoRoll)\n"
            "ui.setFocused(midi.widPianoRoll)\n"
            f"result = [patterns.patternNumber(), channels.selectedChannel(False, True)]"
        )
        if ok != [pattern, channel]:
            raise TargetError(f"could not select pattern {pattern} / channel {channel} (got {ok})")

    # -- piano-roll side ---------------------------------------------------
    def run_script(self, requests: list) -> list[dict]:
        state = SCRIPTS / "piano_roll_state.json"
        (SCRIPTS / "mcp_request.json").write_text(json.dumps(requests))
        before = state.stat().st_mtime
        for _ in range(2):  # a keystroke occasionally gets lost while focus moves
            self.trigger.trigger(delay=0)
            deadline = time.time() + 4
            while time.time() < deadline:
                time.sleep(0.3)
                if state.stat().st_mtime > before:
                    time.sleep(0.2)
                    return json.loads(state.read_text())["notes"]
        (SCRIPTS / "mcp_request.json").write_text("[]")
        raise TargetError("piano roll script did not run (run Scripts > ComposeWithLLM once)")

    # -- verification -----------------------------------------------------
    def verify(self, channel: int, pattern: int, steps: int) -> str:
        """Raise TargetError unless the open piano roll is (channel, pattern)."""
        cur = self._exec("result = patterns.patternNumber()")
        if cur != pattern:
            raise TargetError(f"current pattern is {cur}, expected {pattern}")
        notes = self.run_script([])
        grid = self.grid(channel, steps)
        # FL counts a note on the step its start tick falls into (start // step length).
        ppq = 96
        aligned = {int(round(n["time"] * ppq)) // (ppq // 4) for n in notes}
        aligned = {s for s in aligned if s < steps}
        if grid or aligned:
            if grid != aligned:
                raise TargetError(
                    f"piano roll content doesn't match channel {channel} in pattern {pattern} "
                    f"(grid {len(grid)} steps vs {len(aligned)} on-grid notes)"
                )
            return "signature"
        # Nothing on the grid to compare: drop a marker note and see where it lands.
        busy = aligned
        free = next((s for s in range(steps) if s not in grid and s not in busy), None)
        if free is None:
            raise TargetError("no free step for a probe")
        marker = {"midi": PROBE_PITCH, "time": free / 4, "duration": 0.25, "velocity": 0.01}
        self.run_script([{"action": "add_notes", "notes": [marker]}])
        try:
            landed: set[int] = set()
            for _ in range(6):  # the step grid can lag a moment behind the script
                landed = self.grid(channel, steps) - grid
                if landed:
                    break
                time.sleep(0.25)
        finally:
            probe = {"midi": PROBE_PITCH, "time": free / 4}
            self.run_script([{"action": "delete_notes", "notes": [probe]}])
        if landed != {free}:
            raise TargetError(f"probe did not land on channel {channel} in pattern {pattern}")
        return "probe"
