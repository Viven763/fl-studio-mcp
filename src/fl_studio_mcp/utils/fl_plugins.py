"""Every plugin FL Studio knows, from its plugin database, and loading any of them.

FL keeps one .fst per installed plugin in "Presets/Plugin database/Installed/
<Generators|Effects>/<Fruity|VST|VST3|AudioUnit|...>/<name>.fst". Opening such a
file the way the OS does (double-click) adds the instrument as a new channel,
in the background, without the screen. For effects the plugin's event group
(201 name, 212 wrapper and, for third-party plugins, 203 + 213 state) is copied
into a mixer slot of the saved project (see flp_mixer.add_effects).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from fl_studio_mcp.utils.fl_files import user_data_dir
from fl_studio_mcp.utils.flp_events import read_events

KINDS = {"Generators": "instrument", "Effects": "effect"}
SKIP_FOLDERS = {"New"}  # FL's "recently added" list duplicates real entries
EV_VERSION, EV_TEMPO_LIKE = 199, 28


def database_dir() -> Path:
    return user_data_dir() / "Presets" / "Plugin database" / "Installed"


def plugin_database() -> list[dict]:
    """[{"name", "kind": instrument|effect, "format": Fruity|VST|VST3|AudioUnit, "path"}]"""
    out = []
    root = database_dir()
    for folder, kind in KINDS.items():
        base = root / folder
        if not base.is_dir():
            continue
        for fst in sorted(base.rglob("*.fst")):
            fmt = fst.parent.relative_to(base).parts[0] if fst.parent != base else ""
            if fmt in SKIP_FOLDERS:
                continue
            out.append({"name": fst.stem, "kind": kind, "format": fmt, "path": str(fst)})
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def find_plugins(query: str = "", kind: str | None = None, limit: int = 50) -> list[dict]:
    words = [_norm(w) for w in query.split() if _norm(w)]
    hits = [
        p
        for p in plugin_database()
        if (kind is None or p["kind"] == kind)
        and all(w in _norm(p["name"] + " " + p["format"]) for w in words)
    ]
    q = _norm(query)
    hits.sort(
        key=lambda p: (
            _norm(p["name"]) != q,
            not _norm(p["name"]).startswith(q),
            p["format"] != "Fruity",
            p["name"],
        )
    )
    return hits[:limit]


def resolve_plugin(name: str, kind: str) -> dict:
    exact = [p for p in plugin_database() if p["kind"] == kind and _norm(p["name"]) == _norm(name)]
    if exact:
        return sorted(exact, key=lambda p: p["format"] != "Fruity")[0]
    near = find_plugins(name, kind, 5)
    hint = f"; did you mean {', '.join(p['name'] for p in near)}?" if near else ""
    raise ValueError(f"no installed {kind} named {name!r}{hint}")


def effect_group(plugin: dict) -> list[tuple[int, bytes]]:
    """The plugin's events as they go into a mixer slot (without the file preamble)."""
    _, events = read_events(Path(plugin["path"]).read_bytes())
    group = [(e, v) for e, v in events if e not in (EV_VERSION, EV_TEMPO_LIKE)]
    if not group or group[0][0] != 201:
        raise ValueError(f"unexpected layout in {plugin['path']}")
    return group


def instrument_file(plugin: dict, channel_name: str, folder: Path) -> Path:
    """A copy of the plugin's database .fst named after the wanted channel.

    FL names a channel opened from a preset after the file, so the copy carries
    the name; the database file itself is never touched."""
    safe = re.sub(r'[\\/:*?"<>|]', "_", channel_name).strip() or plugin["name"]
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / f"{safe}.fst"
    shutil.copy2(plugin["path"], dst)
    return dst
