"""Audio samples: find them on disk, put them on new or existing Sampler channels.

FL's scripting API can't create channels or load samples, so channels are added
and re-pointed in the saved project file (verified, backed up) and FL reloads it
in the background.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

AUDIO_EXT = {".wav", ".wv", ".aif", ".aiff", ".flac", ".mp3", ".ogg"}


def sample_roots() -> list[Path]:
    """FL's bundled packs, FL's download folder and folders in FL_SAMPLE_DIRS."""
    from fl_studio_mcp.utils.fl_files import fl_install_dirs, user_data_dir

    roots = [Path(p) for p in os.environ.get("FL_SAMPLE_DIRS", "").split(os.pathsep) if p]
    roots += [d / "Data" / "Patches" / "Packs" for d in fl_install_dirs()]
    roots += [user_data_dir().parent / "Downloads", user_data_dir() / "Audio"]
    return [r for r in roots if r.is_dir()]


def find_samples(query: str, limit: int = 30) -> list[str]:
    words = query.lower().split()
    found: list[str] = []
    for root in sample_roots():
        for path in root.rglob("*"):
            if path.suffix.lower() not in AUDIO_EXT:
                continue
            rel = str(path.relative_to(root)).lower()
            if all(w in rel for w in words):
                found.append(str(path))
                if len(found) >= limit:
                    return found
    return found


def register_sample_tools(mcp: FastMCP) -> None:
    from fl_studio_mcp.utils.connection import get_connection
    from fl_studio_mcp.utils.fl_files import EDIT_LOCK, reopen_and_wait, save_project_and_locate

    @mcp.tool()
    def fl_find_samples(query: str, limit: int = 30) -> dict:
        """Search audio samples (FL's packs, FL downloads, FL_SAMPLE_DIRS) by path words.

        Args:
            query: Words that must all appear in the path, e.g. "hyper clap", "808 oh"
            limit: Maximum results
        """
        return {"samples": find_samples(query, limit)}

    @mcp.tool()
    def fl_add_sample_channels(samples: list[dict]) -> dict:
        """Add Sampler channels playing audio files (one reload, backup kept).

        A Sampler channel already in the project is used as the template (any
        new FL project has some), so new channels start with its settings and
        mixer insert; route them with fl_mixer_setup afterwards.

        Args:
            samples: [{"name": "Kick", "path": "/.../Power CrunchKick 10.wv"}, ...]
        Returns {"added": [{"index", "name", "path"}], "channels", "backup", "reloaded"}.
        """
        from fl_studio_mcp.utils.flp_edit import add_sampler_channels

        send = get_connection().send_command
        with EDIT_LOCK:
            try:
                pairs = [(str(s["name"]), str(Path(s["path"]).expanduser())) for s in samples]
                project = save_project_and_locate(send)
                res = add_sampler_channels(project, pairs)
                res["reloaded"] = reopen_and_wait(send, project, expected=res["channels"])
            except (RuntimeError, ValueError, KeyError, OSError) as e:
                return {"error": str(e)}
        first = len(res["channels"]) - len(pairs)
        res["added"] = [{"index": first + k, "name": n, "path": p}
                        for k, (n, p) in enumerate(pairs)]
        return res

    @mcp.tool()
    def fl_set_channel_sample(channel: int, path: str, name: str | None = None) -> dict:
        """Load another audio file into an existing Sampler channel (one reload, backup kept).

        Args:
            channel: Channel index (global) of a Sampler channel
            path: Audio file
            name: Optional new channel name
        """
        from fl_studio_mcp.utils.flp_edit import set_channel_sample

        send = get_connection().send_command
        with EDIT_LOCK:
            try:
                project = save_project_and_locate(send)
                res = set_channel_sample(project, channel, path, name)
                res["reloaded"] = reopen_and_wait(send, project, expected=res["channels"])
            except (RuntimeError, ValueError, OSError) as e:
                return {"error": str(e)}
        return res
