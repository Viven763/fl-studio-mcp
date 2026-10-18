"""Render projects to WAV with FL Studio's own command-line renderer, invisibly.

FL's scripting API has no export function, so a separate background FL
instance renders copies of the saved project; the FL the user works in is
never touched or reloaded. Every version to render (the mix, stems, versions
with channels muted) is written as a copy into one temp folder and ONE
instance renders the whole folder (-F), so FL and its plugins load once.

On macOS the instance is started through LaunchServices hidden and in the
background (open -n -g -j): no window and no change of focus; only a second
Dock icon shows while it works. Switch syntax differs per OS: macOS takes
"-R -Ewav -F<folder>" (with "/R" FL just opens the project), Windows
"/R /Ewav /F<folder>". FL's argument parser can't handle spaces, so the temp
folder has none.

The render is done when every WAV exists and the instance has exited (or the
WAVs stopped growing - FL sometimes stays open). If nothing progresses for a
while (e.g. FL shows a dialog about a missing plugin), the render instance -
and only that process - is stopped and the error names what is missing.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from fl_studio_mcp.utils.fl_files import fl_install_dirs
from fl_studio_mcp.utils.flp_events import read_events, write_events

EventPatch = Callable[[list[tuple[int, bytes]]], list[tuple[int, bytes]]]

STARTUP_GRACE = 240.0  # FL + plugins (ZENOLOGY...) loading before the first WAV appears
STALL = 120.0  # no WAV grew for this long while the instance runs: assume it is stuck
SETTLE = 3.0  # a WAV whose size didn't change for this long counts as written


def _fl_app() -> Path:
    system = platform.system()
    if system == "Darwin":
        apps = sorted(Path("/Applications").glob("FL Studio*.app"), reverse=True)
        if apps:
            return apps[0]
    elif system == "Windows":
        for d in fl_install_dirs():
            if (d / "FL64.exe").exists():
                return d / "FL64.exe"
    raise RuntimeError("FL Studio executable not found")


class _Instance:
    """The background render instance for one temp folder."""

    def __init__(self, work: Path):
        self.work = work
        self.proc: subprocess.Popen | None = None

    def launch(self) -> None:
        if platform.system() == "Darwin":
            subprocess.run(
                ["open", "-n", "-g", "-j", "-a", str(_fl_app()), "--args",
                 "-R", "-Ewav", f"-F{self.work}/"],
                check=True, capture_output=True, timeout=30,
            )
        else:
            self.proc = subprocess.Popen(
                [str(_fl_app()), "/R", "/Ewav", f"/F{self.work}\\"], cwd=self.work,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    def pids(self) -> list[int]:
        """Processes rendering this folder (never the user's FL, whose command line differs)."""
        if self.proc is not None:
            return [self.proc.pid] if self.proc.poll() is None else []
        return render_pids(_ps(), self.work)

    def stop(self) -> None:
        """Stop the render instance if it still runs (FL sometimes stays open after rendering)."""
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            return
        for sig, grace in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 2.0)):
            pids = self.pids()
            if not pids:
                return
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            deadline = time.time() + grace
            while self.pids() and time.time() < deadline:
                time.sleep(0.5)


def _ps() -> str:
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, timeout=10)
    return out.stdout.decode("utf-8", errors="replace")  # command lines can be any bytes


def render_pids(ps_output: str, work: Path) -> list[int]:
    """PIDs of FL processes whose command line renders `work` (from `ps -axo pid=,command=`)."""
    folder = str(work)
    pids = []
    for line in ps_output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and "OsxFL" in parts[1] and folder in parts[1]:
            pids.append(int(parts[0]))
    return pids


def _safe_key(n: int) -> str:
    return f"v{n:03d}"


def render_versions(
    project: Path,
    versions: dict[str, EventPatch | None],
    outputs: dict[str, Path],
    timeout: float = 1800.0,
    instance_factory: Callable[[Path], _Instance] = _Instance,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    startup_grace: float = STARTUP_GRACE,
    stall: float = STALL,
) -> dict[str, Path]:
    """Render several versions of a saved project with ONE hidden background FL instance.

    versions: name -> patch (a function on the project's events, None = as saved).
    outputs: name -> where the WAV goes. Returns name -> WAV path.
    """
    if set(outputs) != set(versions):
        raise ValueError("every version needs an output path")
    work = Path(tempfile.mkdtemp(prefix="flrender_"))
    instance = instance_factory(work)
    try:
        if " " in str(work):
            raise RuntimeError(f"temp folder contains spaces: {work}")
        header, events = read_events(project.read_bytes())
        keys = {name: _safe_key(n) for n, name in enumerate(versions)}
        for name, patch in versions.items():
            evs = patch(list(events)) if patch is not None else events
            (work / f"{keys[name]}.flp").write_bytes(write_events(header, evs))
        wavs = {name: work / f"{keys[name]}.wav" for name in versions}
        instance.launch()
        _wait(instance, wavs, timeout, sleep, clock, startup_grace, stall)
        results = {}
        for name, wav in wavs.items():
            out = outputs[name]
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(wav), out)
            results[name] = out
        return results
    finally:
        instance.stop()  # only the render instance; a no-op when it already exited
        shutil.rmtree(work, ignore_errors=True)


def _wait(instance: _Instance, wavs: dict[str, Path], timeout: float, sleep, clock,
          startup_grace: float, stall: float) -> None:
    def size(p: Path) -> int:
        try:
            return p.stat().st_size
        except OSError:
            return -1

    start = clock()
    last_sizes = {n: -1 for n in wavs}
    stable_since: dict[str, float] = {}
    last_progress = start
    seen_running = False
    while True:
        sleep(1.0)
        now = clock()
        running = bool(instance.pids())
        seen_running = seen_running or running
        for name, wav in wavs.items():
            s = size(wav)
            if s != last_sizes[name]:
                last_sizes[name] = s
                last_progress = now
                stable_since.pop(name, None)
            elif s > 0:
                stable_since.setdefault(name, now)
        written = [n for n in wavs if last_sizes[n] > 0]
        missing = [n for n in wavs if last_sizes[n] <= 0]
        settled = all(now - stable_since.get(n, now) >= SETTLE for n in wavs if n in written)
        if not missing and (not running or settled):
            return
        if not running and (seen_running or now - start > 30):
            raise RuntimeError(
                f"FL's render instance exited without rendering {missing}"
                + (f" (rendered: {written})" if written else "")
            )
        quiet = now - last_progress
        if quiet > (stall if written else startup_grace):
            raise RuntimeError(
                f"rendering stalled for {quiet:.0f} s (FL may be showing a dialog, e.g. about a "
                f"missing plugin or sample); missing {missing}"
            )
        if now - start > timeout:
            raise RuntimeError(f"rendering timed out after {timeout:.0f} s; missing {missing}")


def render_project(
    project: Path,
    timeout: float = 900.0,
    out: Path | None = None,
    patch: EventPatch | None = None,
) -> Path:
    """Render a saved `project` invisibly in the background and return the WAV.

    The copy that gets rendered can be changed first with `patch` (a function
    on the project's events, e.g. muting channels); the project itself is never
    touched. Default output: <project folder>/<name>.render.wav.
    """
    out = out or project.with_name(f"{project.stem}.render.wav")
    return render_versions(project, {"render": patch}, {"render": out}, timeout)["render"]


def safe_stem(label: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", label).strip() or "stem"
