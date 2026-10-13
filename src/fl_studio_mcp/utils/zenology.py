"""Roland ZENOLOGY tones in FL Studio: read, load and swap them by name.

ZENOLOGY ignores MIDI program changes, exposes no host presets and keeps its
tone library encrypted. But every saved ZENOLOGY state carries the complete
tone it plays, so a tone ZENOLOGY has loaded once can be reused as a file:

- The tone library (Channel presets > MCP Snapshots > ZENOLOGY) holds one
  channel preset per tone, harvested from saved projects and presets. Loading
  a tone from it, or swapping the tone of an existing channel (a project-file
  edit), needs no screen, mouse or keyboard.
- Only a tone ZENOLOGY has never loaded anywhere needs the screen once
  (macOS): build an .fst whose "current tone" reference points next to the
  target, load it, press ZENOLOGY's tone arrow (correcting until the right
  number shows), verify the tone from the saved state, add it to the library.
"""

from __future__ import annotations

import functools
import json
import os
import plistlib
import re
import struct
import subprocess
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterable
from pathlib import Path

from fl_studio_mcp.utils.fl_files import open_in_fl, snapshot_dir, user_data_dir
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

# Layout of the ZENOLOGY wrapper payload (FL event 213), identical in every saved state
# seen: the plist starts at byte 241, preceded by int64 sizes of the state chunk
# (plist + 12) at 221 and of the plist itself at 233.
PLIST_OFFSET = 241
ADDR_PACK, ADDR_TONE = 0xFFFC008, 0xFFFC00C
# Part settings the host can change (mono, legato, ?, portamento, portamento time).
HOST_ADDRS = (0x75330, 0x75334, 0x75338, 0x7533C, 0x75340)
# Tone arrows relative to the plugin window's top-left corner (points, BASIC page).
ARROW_PREV = (100, 108)  # ▲ - previous tone number
ARROW_NEXT = (100, 129)  # ▼ - next tone number


class ZenologyError(RuntimeError):
    pass


def _norm(s: str) -> str:
    # OCR confuses "l" / "I" / "1" in tone names ("VlnSRX" read as "VInSRX")
    return re.sub(r"[^a-z0-9]", "", s.lower()).replace("1", "l").replace("i", "l")


@functools.lru_cache(maxsize=1)
def _tones() -> tuple[dict, ...]:
    return tuple(json.loads(TONES_FILE.read_text()))


def find_tones(query: str, limit: int = 20) -> list[dict]:
    tones = [dict(t) for t in _tones()]
    q = _norm(query)
    exact = [t for t in tones if _norm(t["name"]) == q]
    if exact:
        return exact[:limit]
    words = [w for w in re.split(r"\s+", query.lower()) if w]
    def hit(t: dict) -> bool:
        return all(_norm(w) in _norm(t["name"]) or _norm(w) in _norm(t["category"]) for w in words)

    return [t for t in tones if hit(t)][:limit]


def _pack_size(pack: str) -> int:
    return max(t["no"] for t in _tones() if t["pack"] == pack)


def patch_state(payload: bytes, updates: dict[int, int], strict: bool = True) -> bytes:
    """A ZENOLOGY plugin state (event 213) with some address/value pairs changed.

    Refuses states whose layout differs from the known one rather than guessing.
    With strict, every address in `updates` must exist in the state.
    """
    off = payload.find(b"bplist00")
    if off != PLIST_OFFSET:
        raise ZenologyError("unexpected ZENOLOGY state layout (plist offset)")
    size = struct.unpack_from("<q", payload, off - 8)[0]
    chunk = struct.unpack_from("<q", payload, off - 20)[0]
    if chunk != size + 12 or off + size != len(payload):
        raise ZenologyError("unexpected ZENOLOGY state layout (sizes)")
    plist = plistlib.loads(payload[off:])
    blob = bytearray(plist[ZEN_KEY])
    found = set()
    for i in range(0, len(blob) - 7, 8):
        addr = struct.unpack_from(">I", blob, i)[0]
        if addr in updates:
            struct.pack_into(">I", blob, i + 4, updates[addr])
            found.add(addr)
    if strict and found != set(updates):
        raise ZenologyError(f"ZENOLOGY state lacks fields {sorted(set(updates) - found)}")
    plist[ZEN_KEY] = bytes(blob)
    new = plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)
    header = bytearray(payload[:off])
    struct.pack_into("<q", header, off - 20, len(new) + 12)
    struct.pack_into("<q", header, off - 8, len(new))
    return bytes(header) + new


def state_values(payload: bytes, addrs: Iterable[int]) -> dict[int, int]:
    """Values of the given addresses in a ZENOLOGY state (missing ones left out)."""
    plist = state_plist(payload)
    blob = plist.get(ZEN_KEY) if plist else None
    if not blob:
        raise ZenologyError("not a ZENOLOGY plugin state")
    wanted = set(addrs)
    out = {}
    for i in range(0, len(blob) - 7, 8):
        addr = struct.unpack_from(">I", blob, i)[0]
        if addr in wanted:
            out[addr] = struct.unpack_from(">I", blob, i + 4)[0]
    return out


def build_fst(pack_id: int, tone_index: int, channel_name: str, out_path: Path) -> Path:
    payload = patch_state(STATE_TEMPLATE.read_bytes(), {ADDR_PACK: pack_id, ADDR_TONE: tone_index})
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


def _run(cmd: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    """Run a helper command; failures become ZenologyError (with stderr) instead of crashing."""
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip()
        hint = " (grant Screen Recording to the app running the MCP server)" if (
            cmd and cmd[0] == "screencapture") else ""
        raise ZenologyError(f"{cmd[0]} failed{hint}: {detail or e}") from e
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        raise ZenologyError(f"{cmd[0]} failed: {e}") from e


def _helper() -> Path:
    exe = CACHE / "flhelper"
    if not exe.exists() or exe.stat().st_mtime < HELPER_SRC.stat().st_mtime:
        CACHE.mkdir(parents=True, exist_ok=True)
        _run(["swiftc", "-O", str(HELPER_SRC), "-o", str(exe)], timeout=180)
    return exe


def _osa(script: str) -> str:
    """Run AppleScript; failures (e.g. missing Accessibility/Automation permission) raise."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=15
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise ZenologyError(f"osascript failed: {e}") from e
    if proc.returncode != 0:
        raise ZenologyError(
            "osascript failed (Accessibility/Automation permission for the app running "
            f"this server?): {proc.stderr.strip() or proc.returncode}"
        )
    return proc.stdout.strip()


FL_BUNDLE_ID = "com.image-line.flstudio"


def _activate_fl() -> None:
    """Bring FL to the front, switching to its desktop (Space) if it lives on another one.

    AppleScript's `activate` switches desktops; `open -a` (LaunchServices) only
    makes FL the active app and leaves its windows on their own desktop.
    """
    try:
        _osa(f'tell application id "{FL_BUNDLE_ID}" to activate')
        return
    except ZenologyError:
        pass
    apps = sorted(Path("/Applications").glob("FL Studio*.app"), reverse=True)
    cmd = ["open", "-a", str(apps[0])] if apps else ["open", "-b", FL_BUNDLE_ID]
    try:
        subprocess.run(cmd, capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _front_is_fl() -> bool:
    _activate_fl()
    time.sleep(1.2)  # the switch to FL's fullscreen desktop is animated
    events = 'tell application "System Events" to '
    if _osa(events + "get name of first process whose frontmost is true") == "OsxFL":
        return True
    _osa(events + 'tell process "OsxFL" to set frontmost to true')
    time.sleep(1.0)
    return _osa(events + "get name of first process whose frontmost is true") == "OsxFL"


def title_matches(window_title: str, channel_name: str) -> bool:
    """A plugin window is titled "<channel name> (...)"; match that name exactly."""
    return window_title == channel_name or window_title.startswith(channel_name + " (")


def pick_window(windows: list[tuple[str, tuple]], channel_name: str) -> tuple | None:
    """The rect of `channel_name`'s plugin window among (title, rect) pairs.

    Exact titles ("Name" / "Name (Insert)") win; otherwise a title that continues
    after the name with a separator ("Name - ZENOLOGY") - but never one where the
    name continues as a longer word ("Upright" must not pick "Upright Bass").
    """
    exact = [rect for title, rect in windows if title_matches(title, channel_name)]
    if exact:
        return exact[0]
    loose = [
        rect for title, rect in windows
        if title.startswith(channel_name)
        and re.match(r"\s*([(\[\-–—|:]|$)", title[len(channel_name):])
    ]
    return loose[0] if len(loose) == 1 else None


Window = tuple[str, tuple[int, int, int, int], bool]  # title, (x, y, w, h), on screen


def clean_title(title: str) -> str:
    """A window title without FL's invisible glyph markers and doubled spaces.

    FL decorates plugin window titles with private-use characters, e.g.
    "Upright\\ue5a0 (Master)\\ue5ff"; they are dropped before matching names.
    """
    kept = "".join(ch for ch in title if unicodedata.category(ch) not in ("Co", "Cf", "Cc"))
    return " ".join(kept.split())


def parse_windows(text: str) -> list[Window]:
    """Parse `flhelper windows` output: "title|x,y,w,h|onscreen|layer" per line."""
    found = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        try:
            x, y, w, h = (int(float(v)) for v in parts[1].split(","))
        except ValueError:
            continue
        found.append((clean_title(parts[0]), (x, y, w, h), parts[2].strip() == "1"))
    return found


def _windows() -> list[Window]:
    """FL's windows from the WindowServer list (every desktop), else from System Events.

    System Events only reports windows on the visible desktop, so it sees nothing
    while FL runs fullscreen on its own Space; the WindowServer list always does.
    """
    try:
        found = parse_windows(_run([str(_helper()), "windows", "FL Studio"]).stdout)
        if found:
            return found
    except ZenologyError:
        pass
    return [(clean_title(title), rect, True) for title, rect in _windows_applescript()]


def _windows_applescript() -> list[tuple[str, tuple[int, int, int, int]]]:
    """(title, (x, y, w, h)) of FL's windows on the visible Space."""
    out = _osa(
        'tell application "System Events" to tell process "OsxFL"\n'
        "set r to {}\n"
        "repeat with w in windows\n"
        "try\n"  # one odd window (no position/size) must not hide all the others
        "set end of r to (name of w) & \"|\" & (item 1 of (position of w as list)) & \",\" & "
        "(item 2 of (position of w as list)) & \",\" & (item 1 of (size of w as list)) & \",\" & "
        "(item 2 of (size of w as list))\n"
        "end try\n"
        "end repeat\n"
        "set AppleScript's text item delimiters to \"\\n\"\n"
        "return r as text\n"
        "end tell"
    )
    found = []
    for line in out.splitlines():
        title, _, rect = line.partition("|")
        try:
            x, y, w, h = (int(float(v)) for v in rect.split(","))
        except ValueError:
            continue
        found.append((title, (x, y, w, h)))
    return found


def read_tone_label(rect: tuple[int, int, int, int]) -> str:
    x, y, w, h = rect
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        shot = tmp.name
    try:
        _run(["screencapture", "-x", "-R", f"{x},{y},{w},{h}", shot])
        return _run([str(_helper()), "ocr", shot]).stdout
    finally:
        Path(shot).unlink(missing_ok=True)


def unique_channel_name(base: str, existing: list[str]) -> str:
    """`base`, or "base 2", "base 3"... so the new plugin window's title is unique."""
    name, n = base, 2
    while name in existing:
        name = f"{base} {n}"
        n += 1
    return name


def plan_clicks(current: int, target: int) -> tuple[str, int]:
    """Which tone arrow to press and how often to get from `current` to `target`."""
    return ("next", target - current) if target >= current else ("prev", current - target)


# ---------------------------------------------------------------- reading a channel's tone

ZEN_KEY = "jp.co.roland.ZENOLOGY.data"
ADDR_NAME, NAME_CHARS, NAME_STRIDE = 0x1130, 16, 4


def state_plist(payload: bytes) -> dict | None:
    """The plist inside a plugin state event (213), or None when there is none."""
    off = payload.find(b"bplist00")
    if off < 0:
        return None
    size = struct.unpack_from("<q", payload, off - 8)[0] if off >= 8 else 0
    end = off + size if 0 < size <= len(payload) - off else len(payload)
    try:
        return plistlib.loads(payload[off:end])
    except Exception:  # noqa: BLE001 - not a plist we understand
        return None


def read_tone_from_state(payload: bytes) -> dict:
    """{"pack_id", "index", "number", "name"} of the tone a ZENOLOGY state holds."""
    plist = state_plist(payload)
    blob = plist.get(ZEN_KEY) if plist else None
    if not blob:
        raise ZenologyError("not a ZENOLOGY plugin state")
    pairs = {
        struct.unpack(">I", blob[i:i + 4])[0]: struct.unpack(">I", blob[i + 4:i + 8])[0]
        for i in range(0, len(blob) - 7, 8)
    }
    chars = [pairs.get(ADDR_NAME + NAME_STRIDE * k, 32) for k in range(NAME_CHARS)]
    name = "".join(chr(c) if 32 <= c < 127 else " " for c in chars).strip()
    index = pairs.get(ADDR_TONE)
    return {
        "pack_id": pairs.get(ADDR_PACK),
        "index": index,
        "number": index + 1 if index is not None else None,
        "name": name,
    }


def describe_tone(info: dict) -> dict:
    """Add the pack folder, listed name and category from the tone index."""
    hit = next(
        (t for t in _tones() if t["pack_id"] == info["pack_id"] and t["no"] == info["number"]),
        None,
    )
    out = dict(info)
    if hit:
        out.update(pack=hit["pack"], listed_name=hit["name"], category=hit["category"],
                   consistent=_norm(hit["name"]) == _norm(info["name"]))
    return out


def channel_tone(project: Path, channel: int) -> dict:
    """The ZENOLOGY tone of channel `channel` in a saved project."""
    from fl_studio_mcp.utils.fl_files import _channel_blocks

    _, events = read_events(project.read_bytes())
    block = _channel_blocks(events).get(channel)
    if block is None:
        raise ZenologyError(f"channel {channel} not found in {project.name}")
    state = next((v for e, v in block if e == 213), None)
    if state is None:
        raise ZenologyError(f"channel {channel} has no plugin state")
    return describe_tone(read_tone_from_state(state))


# ---------------------------------------------------------------- tone library (no screen)


def library_dir() -> Path:
    """One channel preset per tone; FL's browser shows it under Channel presets."""
    return snapshot_dir() / "ZENOLOGY"


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "ZENOLOGY"


def _preset_state(path: Path) -> bytes | None:
    try:
        _, events = read_events(path.read_bytes())
    except (OSError, ValueError):
        return None
    return next((v for e, v in events if e == 213), None)


def library_index() -> dict[tuple[int, int], Path]:
    """(pack id, tone number) -> preset file, for every tone in the library."""
    out: dict[tuple[int, int], Path] = {}
    root = library_dir()
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*.fst")):
        state = _preset_state(path)
        if not state:
            continue
        try:
            info = read_tone_from_state(state)
        except ZenologyError:
            continue
        out.setdefault((info["pack_id"], info["number"]), path)
    return out


def harvest_sources() -> list[Path]:
    """Project files and channel presets that may hold ZENOLOGY tones."""
    home = Path.home()
    roots = [(user_data_dir(), 6), (home / "Downloads", 2), (home / "Desktop", 2)]
    roots += [(Path(p), 4) for p in os.environ.get("FL_PROJECT_DIRS", "").split(os.pathsep) if p]
    found: dict[Path, None] = {}
    for root, depth in roots:
        if not root.is_dir():
            continue
        for d in range(depth + 1):
            for pattern in ("*.flp", "*.fst"):
                for p in root.glob("/".join(["*"] * d + [pattern])):
                    found.setdefault(p, None)
    return list(found)


def harvest(paths: Iterable[Path]) -> dict:
    """Copy every ZENOLOGY tone found in these files into the library.

    Only tones whose stored name matches the tone index for their pack and
    number are taken (a freshly built .fst still holds the template's sound).
    Library copies have the host's part settings (mono/legato/portamento) off.
    """
    from fl_studio_mcp.utils.fl_files import channel_bounds, preset_from_block

    root = library_dir()
    index = library_index()
    added = []
    for path in paths:
        if root in path.parents:
            continue
        try:
            _, events = read_events(path.read_bytes())
        except (OSError, ValueError):
            continue
        version = next((v for e, v in events if e == 199), b"")
        if path.suffix.lower() == ".flp":
            blocks = [events[s + 1:e] for s, e in channel_bounds(events)]
        else:
            blocks = [events]
        for block in blocks:
            state = next((v for e, v in block if e == 213), None)
            if not state or b"bplist00" not in state:
                continue
            try:
                info = describe_tone(read_tone_from_state(state))
                key = (info["pack_id"], info["number"])
                if not info.get("consistent") or key in index:
                    continue
                clean = patch_state(state, dict.fromkeys(HOST_ADDRS, 0), strict=False)
            except ZenologyError:
                continue
            name = info["listed_name"]
            out = root / info["pack"] / f"{_safe(name)}.fst"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(preset_from_block(version, block, name, clean))
            index[key] = out
            added.append({"pack": info["pack"], "number": info["number"], "name": name,
                          "from": path.name})
    return {"added": added, "library": len(index)}


def library_tone(tone: dict, rescan: bool = True) -> Path | None:
    """The library preset of `tone`; rescans the user's files once when it's missing."""
    key = (tone["pack_id"], tone["no"])
    path = library_index().get(key)
    if path is None and rescan:
        harvest(harvest_sources())
        path = library_index().get(key)
    return path


def load_from_library(tone: dict, send_command, read_state: Callable | None = None) -> dict:
    """Add `tone` as a new channel from its library preset (no screen, mouse or keyboard)."""
    path = library_tone(tone)
    if path is None:
        raise ZenologyError(f"{tone['name']} is not in the tone library")
    existing = [c["name"] for c in send_command("channels.getAll").get("channels", [])]
    name = unique_channel_name(_safe(tone["name"])[:24], existing)
    src = path
    if name != path.stem:  # FL names the channel after the file
        header, events = read_events(path.read_bytes())
        events = [(e, (name + "\0").encode("utf-16-le") if e == 203 else v) for e, v in events]
        src = CACHE / "library-load" / f"{name}.fst"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(write_events(header, events))
    channel = _open_fst(src, send_command)
    send_command("channels.closeEditor", {"index": channel})
    result = {"channel": channel, "channel_name": name, "tone": tone["name"],
              "pack": tone["pack"], "source": "library", "clicks": 0,
              "verified": False, "verified_by": None}
    if read_state is not None:
        try:
            loaded = read_state(channel)
        except Exception:  # noqa: BLE001 - the preset itself was verified when harvested
            loaded = None
        if loaded is not None:
            result["loaded"] = {k: loaded.get(k) for k in ("pack_id", "number", "name")}
            result["verified_by"] = "state"
            result["verified"] = (loaded.get("pack_id"), loaded.get("number")) == (
                tone["pack_id"], tone["no"])
    return result


def set_channel_tone(project: Path, channel: int, tone: dict, rename: str | None = None,
                     keep_part: bool = True) -> dict:
    """Swap the tone of an existing ZENOLOGY channel in a saved project (file edit).

    The channel keeps its index, notes, routing and effects; its part settings
    (mono/legato/portamento) are kept unless keep_part is False. Writes a backup.
    """
    from fl_studio_mcp.utils.backups import atomic_write, make_backup
    from fl_studio_mcp.utils.fl_files import channel_bounds

    path = library_tone(tone)
    if path is None:
        raise ZenologyError(f"{tone['name']} is not in the tone library")
    new_state = _preset_state(path)
    header, events = read_events(project.read_bytes())
    bounds = channel_bounds(events)
    if not 0 <= channel < len(bounds):
        raise ZenologyError(f"channel {channel} does not exist")
    start, end = bounds[channel]
    idx = next((i for i in range(start, end) if events[i][0] == 213), None)
    if idx is None or not new_state:
        raise ZenologyError(f"channel {channel} has no plugin state")
    old_state = events[idx][1]
    before = describe_tone(read_tone_from_state(old_state))  # refuses non-ZENOLOGY channels
    if keep_part:
        new_state = patch_state(new_state, state_values(old_state, HOST_ADDRS), strict=False)
    events[idx] = (213, new_state)
    renamed = False
    if rename:
        name_at = next((i for i in range(start, end) if events[i][0] == 203), None)
        if name_at is not None:
            events[name_at] = (203, (rename + "\0").encode("utf-16-le"))
            renamed = True
    data = write_events(header, events)
    _, check = read_events(data)
    cb = channel_bounds(check)
    got = read_tone_from_state(next(v for e, v in check[cb[channel][0]:cb[channel][1]] if e == 213))
    if len(cb) != len(bounds) or (got["pack_id"], got["number"]) != (tone["pack_id"], tone["no"]):
        raise ZenologyError("verification failed; project left untouched")
    backup = make_backup(project, "tone")
    atomic_write(project, data)
    return {
        "channel": channel,
        "from": {k: before.get(k) for k in ("pack", "number", "name")},
        "to": {"pack": tone["pack"], "number": tone["no"], "name": tone["name"]},
        "renamed": rename if renamed else None,
        "backup": str(backup),
    }


# ---------------------------------------------------------------- loading tones on screen


def _numbers(text: str) -> list[int]:
    # OCR may read the arrow before the number as a letter ("v 07:")
    return [int(m) for m in re.findall(r"(?m)^[^\d\n]{0,4}(\d+)\s*:", text)]


def _click_arrow(rect: tuple[int, int, int, int], direction: str) -> None:
    dx, dy = ARROW_PREV if direction == "prev" else ARROW_NEXT
    _run([str(_helper()), "click", str(rect[0] + dx), str(rect[1] + dy)])


def _observe_screen(rect: tuple[int, int, int, int], title: str) -> tuple[list[int], str]:
    """Tone numbers ("067:") readable in the plugin window, and the window's text."""
    lines = [ln for ln in read_tone_label(rect).splitlines() if not ln.startswith(title)]
    text = "\n".join(lines)
    return _numbers(text), text


def shown_number(numbers: list[int], target: int) -> int | None:
    """Which OCR'd number is the tone number: the target if visible, else the closest."""
    if not numbers:
        return None
    return target if target in numbers else min(numbers, key=lambda n: abs(n - target))


def load_tone(tone: dict, send_command, read_state: Callable | None = None) -> dict:
    """Load `tone` (an entry from find_tones) as a new FL channel and verify it.

    `read_state(channel)` (optional) returns the tone FL saved for the channel
    (see channel_tone); it is the most reliable check.
    """
    use_prev = tone["no"] < _pack_size(tone["pack"])
    # Stored index i is displayed as number i+1, so ▲ from i loads tone i, ▼ from i-2 loads tone i.
    index = tone["no"] if use_prev else tone["no"] - 2
    existing = [c["name"] for c in send_command("channels.getAll").get("channels", [])]
    base = re.sub(r"[^A-Za-z0-9 ._'-]", "_", tone["name"]).strip()[:24] or "ZENOLOGY"
    # FL names the new channel (and its plugin window) after the file name.
    name = unique_channel_name(base, existing)
    folder = CACHE / f"zen_{tone['pack_id']}_{tone['no']}"
    folder.mkdir(parents=True, exist_ok=True)
    fst = build_fst(tone["pack_id"], index, name, folder / f"{name}.fst")
    channel = _open_fst(fst, send_command)
    return _select_on_screen(tone, channel, name, use_prev, send_command, read_state)


def _open_fst(fst: Path, send_command) -> int:
    """Open a channel preset in FL and return the index of the channel it added."""
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
    return after - 1


def _find_window(name: str, channel: int, send_command) -> tuple[int, int, int, int]:
    """The plugin window's rect once it is on screen (clicks go to screen coordinates)."""
    seen: list[Window] = []
    for attempt in range(30):
        seen = _windows()
        rect = pick_window([(t, r) for t, r, on in seen if on], name)
        if rect:
            return rect
        exists = pick_window([(t, r) for t, r, _on in seen], name) is not None
        if exists and attempt % 6 == 5:
            _activate_fl()  # it is there, but on FL's desktop: switch to it again
        if not exists and attempt in (8, 16):  # FL sometimes loads the plugin without its window
            for show in (0, 1):
                code = f"channels.showEditor({channel}, {show}, True)\nresult = 1"
                send_command("debug.exec", {"code": code})
                time.sleep(0.5)
        time.sleep(0.5)
    shown = [t for t, _r, on in seen if on and t][:10]
    hidden = [t for t, _r, on in seen if not on and t][:10]
    raise ZenologyError(
        f"ZENOLOGY window for channel {channel} ({name!r}) is not on screen. FL windows on "
        f"screen: {shown}; on another desktop: {hidden}. Show FL's desktop and retry."
    )


def _select_on_screen(
    tone: dict,
    channel: int,
    name: str,
    use_prev: bool,
    send_command,
    read_state: Callable | None = None,
    click: Callable | None = None,
    observe: Callable | None = None,
    find_window: Callable | None = None,
    max_clicks: int = 5,
    settle: float = 1.2,
) -> dict:
    """Press the tone arrow, then correct until the shown number is the target.

    The window is always closed again. With `read_state`, the result is checked
    against the tone FL actually saved for the channel (verified_by "state");
    otherwise against what is on screen (verified_by "ocr").
    """
    click = click or _click_arrow
    observe = observe or _observe_screen
    find_window = find_window or (lambda: _find_window(name, channel, send_command))
    if click is _click_arrow and not _front_is_fl():
        raise ZenologyError("FL Studio is not the frontmost app; refusing to click")
    target = int(tone["no"])
    clicks, shown, label = 0, None, ""
    try:
        rect = find_window()
        if rect is None:
            raise ZenologyError(f"ZENOLOGY window for channel {channel} did not open")
        click(rect, "prev" if use_prev else "next")
        clicks = 1
        while True:
            time.sleep(settle)
            try:
                numbers, label = observe(rect, f"{name} (")
                shown = shown_number(numbers, target)
            except ZenologyError:
                shown = None  # can't read the screen: rely on the saved state below
            if shown is None or shown == target or clicks >= max_clicks:
                break
            if abs(shown - target) > max_clicks - clicks:
                break  # too far off to be a near miss: don't click around blindly
            direction, _n = plan_clicks(shown, target)
            click(rect, direction)  # one step at a time, then look again
            clicks += 1
    finally:
        send_command("channels.closeEditor", {"index": channel})

    loaded, verified_by, ok = None, None, False
    if read_state is not None:
        try:
            loaded = read_state(channel)
        except Exception:  # noqa: BLE001 - fall back to the screen
            loaded = None
    if loaded is not None:
        verified_by = "state"
        ok = loaded.get("number") == target and loaded.get("pack_id") == tone["pack_id"]
    elif shown is not None:
        verified_by = "ocr"
        wanted = _norm(tone["name"])
        ok = shown == target and (not wanted or wanted in _norm(label))
    result = {
        "channel": channel,
        "channel_name": name,
        "tone": tone["name"],
        "pack": tone["pack"],
        "verified": ok,
        "verified_by": verified_by,
        "clicks": clicks,
        "screen_label": " / ".join(ln for ln in label.splitlines() if ln.strip()),
    }
    if loaded is not None:
        result["loaded"] = {k: loaded.get(k) for k in ("pack_id", "number", "name")}
    return result


# Factory banks (PRST_A ... PRST_L) are built into the plugin and have no tone
# lists on disk. Their pack ids follow the bank letter: PRST_F Essential = 12104
# (seen in a saved state), so PRST_A = 12099 ... PRST_L = 12110.
FACTORY_BASE = 12098


def factory_pack_id(bank: str) -> int:
    letter = bank.strip().upper().removeprefix("PRST_")[:1]
    if not ("A" <= letter <= "L"):
        raise ZenologyError(f"unknown factory bank {bank!r} (expected PRST_A ... PRST_L)")
    return FACTORY_BASE + ord(letter) - ord("A") + 1


def load_factory_tone(bank: str, number: int, send_command, name: str | None = None,
                      read_state: Callable | None = None) -> dict:
    """Load factory tone `number` (as shown in ZENOLOGY) from bank PRST_<letter>.

    There is no name list for factory banks, so only the number (and pack) is verified.
    """
    tone = {"pack": f"PRST_{bank.strip().upper().removeprefix('PRST_')[:1]}",
            "pack_id": factory_pack_id(bank), "no": int(number), "name": ""}
    existing = [c["name"] for c in send_command("channels.getAll").get("channels", [])]
    base = re.sub(r"[^A-Za-z0-9 ._'-]", "_", name or f"Factory {int(number)}").strip()[:24]
    label = unique_channel_name(base or "ZENOLOGY", existing)
    folder = CACHE / f"zen_{tone['pack_id']}_{tone['no']}"
    folder.mkdir(parents=True, exist_ok=True)
    fst = build_fst(tone["pack_id"], tone["no"], label, folder / f"{label}.fst")
    channel = _open_fst(fst, send_command)
    result = _select_on_screen(tone, channel, label, True, send_command, read_state)
    result["tone"] = name or f"Factory {int(number)}"
    return result
