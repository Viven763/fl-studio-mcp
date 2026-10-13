"""Cross-platform helpers for FL Studio files: locating data, opening files, channel snapshots.

A channel snapshot is an FL channel preset (.fst) cut straight out of the saved
project, so it captures whatever the plugin currently holds - including sounds
picked in plugins that expose no presets to the host (e.g. Roland ZENOLOGY).
Snapshots are plain FL files and load on any machine/OS that has the plugin.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import struct
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from fl_studio_mcp.utils.backups import in_backup_dir, is_backup_dir_name, is_backup_name
from fl_studio_mcp.utils.flp_events import read_events, write_events

EV_CH_NEW, EV_CH_TYPE, EV_CH_END, EV_INSERT = 64, 21, 51, 236
EV_VERSION, EV_PLUGIN_NAME, EV_CH_NAME, EV_WRAPPER, EV_STATE = 199, 201, 203, 212, 213
# Header of an FL channel preset (FLhd chunk: format, channel count, PPQ).
FST_HEADER = b"FLhd" + struct.pack("<IHHH", 6, 0x30, 2, 0x30)
# Every save -> edit -> reload sequence (and other multi-step FL operations) holds
# this lock: FastMCP runs tool calls concurrently in worker threads.
EDIT_LOCK = threading.RLock()


def user_data_dir() -> Path:
    """FL Studio's user data folder (Documents/Image-Line/FL Studio on both OSes)."""
    override = os.environ.get("FL_USER_DATA")
    if override:
        return Path(override)
    if platform.system() == "Windows":
        home = Path(os.environ.get("USERPROFILE", Path.home()))
        return home / "Documents" / "Image-Line" / "FL Studio"
    return Path.home() / "Documents" / "Image-Line" / "FL Studio"


def snapshot_dir() -> Path:
    return user_data_dir() / "Presets" / "Channel presets" / "MCP Snapshots"


def fl_install_dirs() -> list[Path]:
    """Factory data folders of installed FL Studio versions."""
    if platform.system() == "Darwin":
        apps = sorted(Path("/Applications").glob("FL Studio*.app"), reverse=True)
        return [a / "Contents" / "Resources" / "FL" for a in apps]
    if platform.system() == "Windows":
        root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Image-Line"
        return sorted(root.glob("FL Studio*"), reverse=True)
    return []


def open_in_fl(path: Path) -> None:
    """Hand a file to the running FL Studio, like double-clicking it in the OS."""
    system = platform.system()
    if system == "Darwin":
        apps = sorted(Path("/Applications").glob("FL Studio*.app"), reverse=True)
        cmd = ["open", "-a", str(apps[0]), str(path)] if apps else ["open", str(path)]
        subprocess.run(cmd, check=True, timeout=10)
    elif system == "Windows":
        exes = [d / "FL64.exe" for d in fl_install_dirs() if (d / "FL64.exe").exists()]
        if exes:
            subprocess.Popen([str(exes[0]), str(path)])
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]  # .fst is associated with FL
    else:
        raise RuntimeError(f"Opening files in FL Studio is not supported on {system}")


_last_project: Path | None = None


def remember_project(path: Path) -> None:
    """Remember the project FL has open, so the next save finds it without searching."""
    global _last_project
    _last_project = Path(path)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _search_roots() -> list[tuple[Path, int]]:
    """Folders to look for a freshly saved project in, with how deep to look."""
    home = Path.home()
    roots = [(Path(p), 3) for p in os.environ.get("FL_PROJECT_DIRS", "").split(os.pathsep) if p]
    roots += [
        (user_data_dir() / "Projects", 3),
        (user_data_dir(), 1),
        (home / "Downloads", 1),
        (home / "Desktop", 2),
        (home / "Documents", 3),
        (home, 2),
    ]
    return roots


def _walk(root: Path, max_depth: int) -> Iterator[Path]:
    """*.flp under `root`, skipping hidden folders, Library and Backup folders."""
    for depth in range(max_depth + 1):
        for p in root.glob("/".join(["*"] * depth + ["*.flp"])):
            folders = p.relative_to(root).parts[:-1]
            if any(f.startswith(".") or f == "Library" or is_backup_dir_name(f)
                   for f in folders):
                continue
            yield p


def find_project_file(window_title: str, saved_after: float) -> Path | None:
    """Find the project FL just saved.

    The scripting API doesn't expose the project path (and newer FL versions
    don't even put the file name in the title), so look for the newest .flp
    written after `saved_after`: first the project found last time, then the
    usual project folders. Backups are skipped unless nothing else matches.
    """
    if _last_project is not None and _mtime(_last_project) >= saved_after:
        return _last_project
    match = re.match(r"\s*(.+?\.flp)\b", window_title, re.IGNORECASE)
    wanted = match.group(1).lower() if match else None
    hits: list[Path] = []
    for root, depth in _search_roots():
        if root.is_dir():
            hits = [p for p in _walk(root, depth) if _mtime(p) >= saved_after]
            if hits:
                break
    if wanted:
        hits = [p for p in hits if p.name.lower() == wanted] or hits
    pool = [p for p in hits if not is_backup_name(p) and not in_backup_dir(p)] or hits
    return max(pool, key=_mtime) if pool else None


def project_warning(project: Path) -> str | None:
    """A warning when the file FL has open is a backup rather than the real project."""
    if is_backup_name(project) or in_backup_dir(project):
        return (
            f"FL has a backup file open ({project.name}), so edits go into it. Use "
            "fl_restore_backup to put it back over the real project, or "
            "fl_save_project_as to continue under a new name."
        )
    return None


def channel_bounds(events: list[tuple[int, bytes]]) -> list[tuple[int, int]]:
    """(start, end) event ranges of channel blocks, end exclusive, in channel order.

    A block starts with ChNew + ChType and ends after its terminator (event 51)
    or at the next block; the last block never runs into the mixer section.
    Lenient on purpose: suitable for reading and patching single events.
    """
    starts = [
        i for i in range(len(events) - 1)
        if events[i][0] == EV_CH_NEW and events[i + 1][0] == EV_CH_TYPE
    ]
    out = []
    for n, start in enumerate(starts):
        limit = starts[n + 1] if n + 1 < len(starts) else next(
            (i for i in range(start + 2, len(events)) if events[i][0] == EV_INSERT), len(events)
        )
        end = next((i + 1 for i in range(start + 2, limit) if events[i][0] == EV_CH_END), limit)
        out.append((start, end))
    return out


def _channel_blocks(events: list[tuple[int, bytes]]) -> dict[int, list[tuple[int, bytes]]]:
    """Map channel index -> its events after ChNew (ChType first)."""
    return {
        struct.unpack("<H", events[start][1])[0]: events[start + 1:end]
        for start, end in channel_bounds(events)
    }


def snapshot_channel(project: Path, channel: int, out_path: Path, name: str) -> Path:
    """Write channel `channel` of a saved project as a standalone .fst preset."""
    _, events = read_events(project.read_bytes())
    block = _channel_blocks(events).get(channel)
    if block is None:
        raise ValueError(f"channel {channel} not found in {project.name}")
    first = {}
    for eid, val in block:
        first.setdefault(eid, val)
    if EV_STATE not in first:
        raise ValueError(f"channel {channel} has no plugin state (sampler/empty channel?)")
    version = next((v for e, v in events if e == EV_VERSION), b"")
    preset = [(EV_VERSION, version), (28, b"\x01")]
    preset += [(eid, first[eid]) for eid in (EV_PLUGIN_NAME, EV_WRAPPER) if eid in first]
    preset += [(EV_CH_NAME, (name + "\0").encode("utf-16-le")), (EV_STATE, first[EV_STATE])]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(write_events(FST_HEADER, preset))
    return out_path


def save_project_and_locate(send_command, timeout: float = 10.0) -> Path:
    """Save the open project via FL and return the path of the written .flp."""
    started = time.time() - 1
    result = send_command("project.save")
    title = result.get("title", "")
    deadline = time.time() + timeout
    while time.time() < deadline:
        path = find_project_file(title, started)
        if path:
            time.sleep(0.5)  # let FL finish writing
            remember_project(path)
            return path
        time.sleep(0.5)
    if _last_project is not None and _last_project.exists():
        raise RuntimeError(
            f"FL did not write {_last_project.name} within {timeout:.0f} s. FL is probably "
            "showing a dialog (e.g. a Save panel, possibly on FL's own desktop): close it in "
            "FL, then retry."
        )
    raise RuntimeError(
        "Saved, but could not find the project file. Is the project saved to disk yet? "
        "Otherwise set FL_PROJECT_DIRS to its folder."
    )


# Before a reload the song position is parked on this tick; FL starts a freshly
# loaded project at the beginning, so a different position means the reload is done.
SENTINEL_TICKS = 7
_WINDOWS = """wins = {"mixer": midi.widMixer, "rack": midi.widChannelRack,
        "playlist": midi.widPlaylist, "pianoroll": midi.widPianoRoll, "browser": midi.widBrowser}
"""
_CAPTURE = _WINDOWS + f"""
result = {{"playing": transport.isPlaying(), "pos": transport.getSongPos(2),
          "mode": transport.getLoopMode(), "pattern": patterns.patternNumber(),
          "windows": {{k: ui.getVisible(w) for k, w in wins.items()}}}}
transport.stop()
transport.setSongPos({SENTINEL_TICKS}, 2)
"""
_PROBE = (
    "result = [transport.getSongPos(2), "
    "[channels.getChannelName(i, True) for i in range(channels.channelCount(1))]]"
)


def _restore_code(state: dict) -> str:
    return _WINDOWS + f"""
st = {state!r}
if transport.getLoopMode() != st["mode"]:
    transport.setLoopMode()
patterns.jumpToPattern(st["pattern"])
for k, w in wins.items():
    if st["windows"].get(k):
        ui.showWindow(w)
    elif ui.getVisible(w):
        ui.hideWindow(w)
transport.setSongPos(st["pos"], 2)
if st["playing"] and not transport.isPlaying():
    transport.start()
result = True
"""


def _exec_quiet(send_command, code: str, timeout: float = 2.0):
    """debug.exec that treats errors and timeouts (FL busy loading) as None."""
    try:
        reply = send_command("debug.exec", {"code": code}, timeout=timeout)
    except Exception:  # noqa: BLE001 - transport errors just mean "not answering yet"
        return None
    return reply.get("result") if isinstance(reply, dict) else None


def reopen_and_wait(
    send_command,
    project: Path,
    timeout: float = 60.0,
    expected: list[str] | None = None,
    restore: bool = True,
    grace: float = 8.0,
    check_names: bool = True,
    expected_count: int | None = None,
) -> bool:
    """Open `project` in FL (usually the one already open), wait until it has really
    loaded, then restore what the user had going.

    Playback, song position (in ticks), loop mode, current pattern and which
    main windows were open are captured before and put back afterwards (unless
    `restore` is False, e.g. when switching to another project), so a file edit
    feels like an in-place update. Loading is detected by the parked song
    position changing, plus the channel list being `expected` (default:
    unchanged) or `expected_count` long; if the position never moves, the
    channel check alone is trusted after `grace` seconds.
    """
    if check_names and expected is None:
        probe = _exec_quiet(send_command, _PROBE)
        expected = probe[1] if probe else None
    if not check_names:
        expected = None
    state = _exec_quiet(send_command, _CAPTURE, timeout=5.0)
    open_in_fl(project)
    started = time.time()
    while time.time() - started < timeout:
        time.sleep(0.5)
        probe = _exec_quiet(send_command, _PROBE)
        if not probe:
            continue
        pos, names = probe
        if expected is not None and names != expected:
            continue
        if expected_count is not None and len(names) != expected_count:
            continue
        if pos != SENTINEL_TICKS or time.time() - started >= grace:
            time.sleep(1.0)  # let FL finish restoring plugin states
            if restore and state:
                _exec_quiet(send_command, _restore_code(state), timeout=5.0)
            remember_project(project)
            return True
    return False


DEFAULT_TEMPLATE = "Minimal/Basic with limiter/Basic with limiter.flp"


def create_project(name: str, template: str = DEFAULT_TEMPLATE) -> Path:
    """Create Projects/<name>/<name>.flp from an FL template (never overwrites)."""
    safe = re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "Untitled"
    src = next((d / "Data" / "Templates" / template for d in fl_install_dirs()
                if (d / "Data" / "Templates" / template).exists()), None)
    if src is None:
        raise RuntimeError(f"FL template not found: {template}")
    folder = user_data_dir() / "Projects" / safe
    target = folder / f"{safe}.flp"
    n = 2
    while target.exists():
        folder = user_data_dir() / "Projects" / f"{safe} {n}"
        target = folder / f"{safe} {n}.flp"
        n += 1
    folder.mkdir(parents=True)
    shutil.copy2(src, target)
    return target
