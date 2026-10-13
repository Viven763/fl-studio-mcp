"""Load Roland ZENOLOGY tones into FL Studio by name (macOS).

ZENOLOGY ignores MIDI program changes and exposes no host presets, so tones
are selected like this:

1. Build an FL channel preset (.fst) from a saved ZENOLOGY state whose
   "current tone" reference points at the target pack/number. ZENOLOGY only
   uses that reference for its prev/next arrows - the sound itself comes from
   the stored tone data.
2. Load the .fst (FL opens it as a new channel and shows the plugin window).
3. Press the tone arrow once, which makes ZENOLOGY load the neighbouring tone
   from its library for real.
4. OCR the plugin window to confirm the tone name, then close the window.
"""

from __future__ import annotations

import json
import plistlib
import re
import struct
import subprocess
import tempfile
import time
from pathlib import Path

from fl_studio_mcp.utils.fl_files import open_in_fl, user_data_dir
from fl_studio_mcp.utils.flp_events import read_events, write_events

DATA = Path(__file__).resolve().parent.parent / "data"
TONES_FILE = DATA / "zenology_tones.json"
STATE_TEMPLATE = DATA / "zenology_state_template.bin"
HELPER_SRC = Path(__file__).resolve().parent / "macos" / "flhelper.swift"
CACHE = Path.home() / "Library" / "Caches" / "fl-studio-mcp"
FST_TEMPLATE = (
    user_data_dir() / "Presets" / "Plugin database"
    / "Installed" / "Generators" / "AudioUnit" / "ZENOLOGY.fst"
)

# Layout of the ZENOLOGY wrapper payload (FL event 213) in the template state.
PLIST_OFFSET = 241
SIZE_FIELDS = (221, 233)  # int64 sizes of the state chunk and the plist inside it
ADDR_PACK, ADDR_TONE = 0xFFFC008, 0xFFFC00C
# Tone arrows relative to the plugin window's top-left corner (points, BASIC page).
ARROW_PREV = (100, 108)  # ▲ - previous tone number
ARROW_NEXT = (100, 129)  # ▼ - next tone number


class ZenologyError(RuntimeError):
    pass


def _norm(s: str) -> str:
    # OCR confuses "l" / "I" / "1" in tone names ("VlnSRX" read as "VInSRX")
    return re.sub(r"[^a-z0-9]", "", s.lower()).replace("1", "l").replace("i", "l")


def find_tones(query: str, limit: int = 20) -> list[dict]:
    tones = json.loads(TONES_FILE.read_text())
    q = _norm(query)
    exact = [t for t in tones if _norm(t["name"]) == q]
    if exact:
        return exact[:limit]
    words = [w for w in re.split(r"\s+", query.lower()) if w]
    def hit(t: dict) -> bool:
        return all(_norm(w) in _norm(t["name"]) or _norm(w) in _norm(t["category"]) for w in words)

    return [t for t in tones if hit(t)][:limit]


def _pack_size(pack: str) -> int:
    return max(t["no"] for t in json.loads(TONES_FILE.read_text()) if t["pack"] == pack)


def _set_pairs(data: bytes, updates: dict[int, int]) -> bytes:
    out = bytearray(data)
    found = set()
    for i in range(0, len(out) - 7, 8):
        addr = struct.unpack(">I", out[i:i + 4])[0]
        if addr in updates:
            out[i + 4:i + 8] = struct.pack(">I", updates[addr])
            found.add(addr)
    if found != set(updates):
        raise ZenologyError("ZENOLOGY state template is missing the tone reference fields")
    return bytes(out)


def build_fst(pack_id: int, tone_index: int, channel_name: str, out_path: Path) -> Path:
    state = STATE_TEMPLATE.read_bytes()
    plist = plistlib.loads(state[PLIST_OFFSET:])
    key = "jp.co.roland.ZENOLOGY.data"
    plist[key] = _set_pairs(plist[key], {ADDR_PACK: pack_id, ADDR_TONE: tone_index})
    blob = plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)
    header = bytearray(state[:PLIST_OFFSET])
    struct.pack_into("<Q", header, SIZE_FIELDS[0], len(blob) + 12)
    struct.pack_into("<Q", header, SIZE_FIELDS[1], len(blob))
    payload = bytes(header) + blob

    fst_header, events = read_events(FST_TEMPLATE.read_bytes())
    patched = []
    for eid, val in events:
        if eid == 213:
            val = payload
        elif eid == 203:
            val = (channel_name + "\0").encode("utf-16-le")
        patched.append((eid, val))
    out_path.write_bytes(write_events(fst_header, patched))
    return out_path


def _helper() -> Path:
    exe = CACHE / "flhelper"
    if not exe.exists() or exe.stat().st_mtime < HELPER_SRC.stat().st_mtime:
        CACHE.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["swiftc", "-O", str(HELPER_SRC), "-o", str(exe)], check=True, capture_output=True
        )
    return exe


def _osa(script: str) -> str:
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    return proc.stdout.strip()


def _front_is_fl() -> bool:
    events = 'tell application "System Events" to '
    _osa(events + 'tell process "OsxFL" to set frontmost to true')
    time.sleep(1.0)  # FL may live in its own fullscreen Space
    return _osa(events + "get name of first process whose frontmost is true") == "OsxFL"


def _window_rect(title_prefix: str) -> tuple[int, int, int, int] | None:
    out = _osa(
        'tell application "System Events" to tell process "OsxFL"\n'
        "set r to {}\n"
        "repeat with w in windows\n"
        "set end of r to (name of w) & \"|\" & (item 1 of (position of w as list)) & \",\" & "
        "(item 2 of (position of w as list)) & \",\" & (item 1 of (size of w as list)) & \",\" & "
        "(item 2 of (size of w as list))\n"
        "end repeat\n"
        "set AppleScript's text item delimiters to \"\\n\"\n"
        "return r as text\n"
        "end tell"
    )
    for line in out.splitlines():
        name, _, rect = line.partition("|")
        if name.startswith(title_prefix) and rect:
            x, y, w, h = (int(float(v)) for v in rect.split(","))
            return x, y, w, h
    return None


def read_tone_label(rect: tuple[int, int, int, int]) -> str:
    x, y, w, h = rect
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        shot = tmp.name
    subprocess.run(["screencapture", "-x", "-R", f"{x},{y},{w},{h}", shot], check=True)
    text = subprocess.run([str(_helper()), "ocr", shot], capture_output=True, text=True).stdout
    Path(shot).unlink(missing_ok=True)
    return text


def load_tone(tone: dict, send_command) -> dict:
    """Load `tone` (an entry from find_tones) as a new FL channel and verify it."""
    use_prev = tone["no"] < _pack_size(tone["pack"])
    # Stored index i is displayed as number i+1, so ▲ from i loads tone i, ▼ from i-2 loads tone i.
    index = tone["no"] if use_prev else tone["no"] - 2
    # FL names the new channel (and its plugin window) after the file name.
    name = re.sub(r"[^A-Za-z0-9 ._'-]", "_", tone["name"]).strip()[:24] or "ZENOLOGY"
    folder = CACHE / f"zen_{tone['pack_id']}_{tone['no']}"
    folder.mkdir(parents=True, exist_ok=True)
    fst = build_fst(tone["pack_id"], index, name, folder / f"{name}.fst")

    def count() -> int | None:  # FL can be busy for seconds while a plugin instance loads
        return send_command("channels.getCount", timeout=5.0).get("count")

    before = None
    for _ in range(5):
        before = count()
        if isinstance(before, int):
            break
    if not isinstance(before, int):
        raise ZenologyError("FL is not answering")
    open_in_fl(fst)
    after = None
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(1.0)
        after = count()
        if isinstance(after, int) and after > before:
            break
    if after != before + 1:
        raise ZenologyError(f"FL did not add a channel (count {before} -> {after})")
    channel = after - 1

    return _select_on_screen(tone, channel, name, use_prev, send_command)


def _select_on_screen(tone: dict, channel: int, name: str, use_prev: bool, send_command) -> dict:
    if not _front_is_fl():
        raise ZenologyError("FL Studio is not the frontmost app; refusing to click")
    rect = None
    for attempt in range(24):  # macOS only lists windows of the visible Space; give FL a moment
        rect = _window_rect(name)
        if rect:
            break
        if attempt in (8, 16):  # FL sometimes loads the plugin without showing its window
            send_command("debug.exec", {"code": f"channels.showEditor({channel}, 0)\nresult = 1"})
            time.sleep(0.5)
            send_command("debug.exec", {"code": f"channels.showEditor({channel}, 1)\nresult = 1"})
        time.sleep(0.5)
    if rect is None:
        raise ZenologyError(f"ZENOLOGY window for channel {channel} did not open")
    dx, dy = ARROW_PREV if use_prev else ARROW_NEXT
    subprocess.run([str(_helper()), "click", str(rect[0] + dx), str(rect[1] + dy)], check=True)
    time.sleep(1.5)

    # Drop the window title: it repeats the channel/file name, not the loaded tone.
    title = f"{name} ("
    lines = [line for line in read_tone_label(rect).splitlines() if not line.startswith(title)]
    text = "\n".join(lines)
    send_command("channels.closeEditor", {"index": channel})
    # OCR may read the ▼ arrow before the number as a letter ("v 07:")
    numbers = [int(m) for m in re.findall(r"(?m)^[^\d\n]{0,4}(\d+)\s*:", text)]
    ok = tone["no"] in numbers and _norm(tone["name"]) in _norm(text)
    label = " / ".join(
        line for line in lines
        if re.match(r"^\s*\d+\s*:", line) or _norm(tone["name"]) in _norm(line)
    )
    return {
        "channel": channel,
        "tone": tone["name"],
        "pack": tone["pack"],
        "screen_label": label,
        "verified": ok,
    }


# Factory banks (PRST_A ... PRST_L) are built into the plugin and have no tone
# lists on disk. Their pack ids follow the bank letter: PRST_F Essential = 12104
# (seen in a saved state), so PRST_A = 12099 ... PRST_L = 12110.
FACTORY_BASE = 12098


def factory_pack_id(bank: str) -> int:
    letter = bank.strip().upper().removeprefix("PRST_")[:1]
    if not ("A" <= letter <= "L"):
        raise ZenologyError(f"unknown factory bank {bank!r} (expected PRST_A ... PRST_L)")
    return FACTORY_BASE + ord(letter) - ord("A") + 1


def load_factory_tone(bank: str, number: int, send_command, name: str | None = None) -> dict:
    """Load factory tone `number` (as shown in ZENOLOGY) from bank PRST_<letter>.

    There is no name list for factory banks, so only the number is verified.
    """
    tone = {"pack": f"PRST_{bank.strip().upper().removeprefix('PRST_')[:1]}",
            "pack_id": factory_pack_id(bank), "no": int(number),
            "name": name or f"Factory {int(number)}"}
    label = re.sub(r"[^A-Za-z0-9 ._'-]", "_", tone["name"]).strip()[:24] or "ZENOLOGY"
    folder = CACHE / f"zen_{tone['pack_id']}_{tone['no']}"
    folder.mkdir(parents=True, exist_ok=True)
    fst = build_fst(tone["pack_id"], tone["no"], label, folder / f"{label}.fst")
    before = send_command("channels.getCount", timeout=5.0).get("count")
    open_in_fl(fst)
    after = before
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(1.0)
        after = send_command("channels.getCount", timeout=5.0).get("count")
        if isinstance(after, int) and isinstance(before, int) and after > before:
            break
    if not (isinstance(before, int) and after == before + 1):
        raise ZenologyError(f"FL did not add a channel (count {before} -> {after})")
    result = _select_on_screen({**tone, "name": "\x00"}, after - 1, label, True, send_command)
    shown = [int(m) for m in re.findall(r"(\d+)\s*:", result["screen_label"])]
    result["tone"], result["verified"] = tone["name"], tone["no"] in shown
    return result
