"""Render a project to WAV with FL Studio's own command-line renderer.

A separate, background FL instance renders a copy of the saved project (in a
path without spaces, which FL's argument parser can't handle). The FL the user
works in stays open and untouched.

Switch syntax differs per OS: macOS takes "-R -Ewav" (with "/R" FL just opens
the project), Windows "/R /Ewav". The WAV is considered done once its size
stops changing; if the render instance doesn't exit by itself, only that
process is terminated.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from fl_studio_mcp.utils.fl_files import fl_install_dirs
from fl_studio_mcp.utils.flp_events import read_events, write_events

EventPatch = Callable[[list[tuple[int, bytes]]], list[tuple[int, bytes]]]


def _fl_executable() -> Path:
    system = platform.system()
    if system == "Darwin":
        apps = sorted(Path("/Applications").glob("FL Studio*.app"), reverse=True)
        if apps:
            return apps[0] / "Contents" / "MacOS" / "OsxFL"
    elif system == "Windows":
        for d in fl_install_dirs():
            if (d / "FL64.exe").exists():
                return d / "FL64.exe"
    raise RuntimeError("FL Studio executable not found")


def render_project(
    project: Path,
    timeout: float = 600.0,
    out: Path | None = None,
    patch: EventPatch | None = None,
) -> Path:
    """Render a saved `project` in the background and return the WAV.

    The copy that gets rendered can be changed first with `patch` (a function
    on the project's events, e.g. muting channels); the project itself is never
    touched. Default output: <project folder>/<name>.render.wav.
    """
    work = Path(tempfile.mkdtemp(prefix="flrender_"))
    try:
        return _render(project, work, timeout, out, patch)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render(project: Path, work: Path, timeout: float, out: Path | None,
            patch: EventPatch | None) -> Path:
    if " " in str(work):
        raise RuntimeError(f"temp folder contains spaces: {work}")
    src = work / "render.flp"
    if patch is None:
        shutil.copy2(project, src)
    else:
        header, events = read_events(project.read_bytes())
        src.write_bytes(write_events(header, patch(events)))
    flags = ["-R", "-Ewav"] if platform.system() == "Darwin" else ["/R", "/Ewav"]
    proc = subprocess.Popen([str(_fl_executable()), *flags, str(src)], cwd=work,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wav = work / "render.wav"
    last, stable_since = -1, None
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            time.sleep(1.0)
            size = wav.stat().st_size if wav.exists() else -1
            if size > 0 and size == last:
                stable_since = stable_since or time.time()
                if time.time() - stable_since >= 3:
                    break
            else:
                stable_since = None
            last = size
            if proc.poll() is not None and not wav.exists():
                raise RuntimeError("FL exited without rendering")
        else:
            raise RuntimeError("rendering timed out")
    finally:
        if proc.poll() is None:  # only the render instance, never the user's FL
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
    out = out or project.with_name(f"{project.stem}.render.wav")
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(wav), out)
    return out
