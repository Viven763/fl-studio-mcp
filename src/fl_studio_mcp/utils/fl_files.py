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
import time
from pathlib import Path

from fl_studio_mcp.utils.flp_events import read_events, write_events

EV_CH_NEW, EV_CH_TYPE = 64, 21
EV_VERSION, EV_PLUGIN_NAME, EV_CH_NAME, EV_WRAPPER, EV_STATE = 199, 201, 203, 212, 213
# Header of an FL channel preset (FLhd chunk: format, channel count, PPQ).
FST_HEADER = b"FLhd" + struct.pack("<IHHH", 6, 0x30, 2, 0x30)


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


def find_project_file(window_title: str, saved_after: float) -> Path | None:
    """Find the project FL just saved.

    The scripting API doesn't expose the project path (and newer FL versions
    don't even put the file name in the title), so look for the newest .flp
    written after `saved_after`, preferring one whose name matches the title.
    """
    match = re.match(r"\s*(.+?\.flp)\b", window_title, re.IGNORECASE)
    wanted = match.group(1).lower() if match else None
    roots = [Path(p) for p in os.environ.get("FL_PROJECT_DIRS", "").split(os.pathsep) if p]
    roots += [user_data_dir() / "Projects", user_data_dir(), Path.home() / "Documents", Path.home()]
    hits: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for depth in range(4):  # shallow search keeps this fast
            for p in root.glob("/".join(["*"] * depth + ["*.flp"])):
                if (
                    "Backup" not in p.parts
                    and ".before-delete-" not in p.name
                    and p.stat().st_mtime >= saved_after
                ):
                    hits.append(p)
        if hits:
            break
    if wanted:
        named = [p for p in hits if p.name.lower() == wanted]
        hits = named or hits
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def _channel_blocks(events: list[tuple[int, bytes]]) -> dict[int, list[tuple[int, bytes]]]:
    """Map channel index -> its events (a channel block starts with ChNew + ChType)."""
    blocks: dict[int, list[tuple[int, bytes]]] = {}
    current: list[tuple[int, bytes]] | None = None
    for i, (eid, val) in enumerate(events):
        is_start = eid == EV_CH_NEW and i + 1 < len(events) and events[i + 1][0] == EV_CH_TYPE
        if is_start:
            current = blocks.setdefault(struct.unpack("<H", val)[0], [])
        elif current is not None:
            current.append((eid, val))
    return blocks


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
            return path
        time.sleep(0.5)
    raise RuntimeError(
        "Saved, but could not find the project file. Is the project saved to disk yet? "
        "Otherwise set FL_PROJECT_DIRS to its folder."
    )


_CAPTURE = """
wins = {"mixer": midi.widMixer, "rack": midi.widChannelRack, "playlist": midi.widPlaylist,
        "pianoroll": midi.widPianoRoll, "browser": midi.widBrowser}
result = {"playing": transport.isPlaying(), "pos": transport.getSongPos(),
          "mode": transport.getLoopMode(), "pattern": patterns.patternNumber(),
          "windows": {k: ui.getVisible(w) for k, w in wins.items()}}
"""


def _restore_code(state: dict) -> str:
    return f"""
st = {state!r}
wins = {{"mixer": midi.widMixer, "rack": midi.widChannelRack, "playlist": midi.widPlaylist,
         "pianoroll": midi.widPianoRoll, "browser": midi.widBrowser}}
if transport.getLoopMode() != st["mode"]:
    transport.setLoopMode()
patterns.jumpToPattern(st["pattern"])
for k, w in wins.items():
    if st["windows"].get(k):
        ui.showWindow(w)
    elif ui.getVisible(w):
        ui.hideWindow(w)
transport.setSongPos(st["pos"])
if st["playing"] and not transport.isPlaying():
    transport.start()
result = True
"""


def reopen_and_wait(
    send_command, project: Path, timeout: float = 20.0, expected: list[str] | None = None
) -> bool:
    """Reopen `project` in FL, wait for it, then restore what the user had going.

    Playback, song position, loop mode, current pattern and which main windows
    were open are captured before the reload and put back afterwards, so a
    file edit feels like an in-place update rather than the project closing.
    `expected` defaults to the current channel list (edits that keep channels).
    """
    state = send_command("debug.exec", {"code": _CAPTURE}).get("result")
    names = expected if expected is not None else [
        c["name"] for c in send_command("channels.getAll").get("channels", [])
    ]
    open_in_fl(project)
    deadline = time.time() + timeout
    time.sleep(1.0)
    while time.time() < deadline:
        now = [c["name"] for c in send_command("channels.getAll").get("channels", [])]
        if now == names:
            time.sleep(1.0)  # let FL finish restoring plugin states
            if state:
                send_command("debug.exec", {"code": _restore_code(state)})
            return True
        time.sleep(0.5)
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
