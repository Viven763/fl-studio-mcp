"""Insert effect plugins into mixer slots of a saved project (.flp).

The scripting API can tweak effects but not load them, so effects are written
into the project file, using real plugin instances taken from FL's own
template projects as the source.

FL Studio 2026 mixer layout: every insert starts with event 236; it then lists
its 10 slots as markers (event 98 = slot index). A loaded plugin is the group
of events *preceding* its slot marker: 201 name, 212 wrapper, 155, 128, 41,
213 state. FL places the effect by the wrapper's own fields (u32 insert at
offset 0, u32 slot at offset 4), not by its position in the file, so both are
rewritten when a template instance is copied in.
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

from fl_studio_mcp.utils.backups import atomic_write, make_backup
from fl_studio_mcp.utils.fl_files import fl_install_dirs
from fl_studio_mcp.utils.flp_events import read_events, write_events

EV_INSERT, EV_SLOT, EV_PLUGIN_NAME, EV_WRAPPER, EV_STATE = 236, 98, 201, 212, 213
SLOTS = 10


def _name(val: bytes) -> str:
    return val.decode("utf-16-le", "replace").rstrip("\x00")


def _insert_starts(events) -> list[int]:
    return [i for i, (e, _) in enumerate(events) if e == EV_INSERT]


def _slot_marker(events, insert: int, slot: int) -> int:
    starts = _insert_starts(events)
    if insert >= len(starts):
        raise ValueError(f"mixer insert {insert} does not exist")
    end = starts[insert + 1] if insert + 1 < len(starts) else len(events)
    for i in range(starts[insert], end):
        e, v = events[i]
        if e == EV_SLOT and struct.unpack("<H", v)[0] == slot:
            return i
    raise ValueError(f"slot {slot} marker not found on insert {insert}")


def _plugin_group_before(events, marker: int) -> tuple[int, int] | None:
    """(start, end) of the plugin group right before a slot marker, or None if empty."""
    i = marker - 1
    while i >= 0 and events[i][0] != EV_SLOT and events[i][0] != EV_INSERT:
        if events[i][0] == EV_PLUGIN_NAME:
            return i, marker
        i -= 1
    return None


def find_effect_template(plugin: str) -> list[tuple[int, bytes]]:
    """Copy of a plugin's event group from the first FL template that uses it."""
    for install in fl_install_dirs():
        for flp in sorted((install / "Data" / "Templates").rglob("*.flp")):
            try:
                _, events = read_events(flp.read_bytes())
            except ValueError:
                continue
            first_insert = next((i for i, (e, _) in enumerate(events) if e == EV_INSERT), None)
            if first_insert is None:
                continue
            for i in range(first_insert, len(events)):
                e, v = events[i]
                if e == EV_PLUGIN_NAME and _name(v).lower() == plugin.lower():
                    j = i
                    while events[j][0] != EV_SLOT:
                        j += 1
                    group = events[i:j]
                    if any(ev == EV_STATE for ev, _ in group):
                        return list(group)
    raise ValueError(f"no FL template contains the effect '{plugin}'")


def list_slots(project: Path) -> dict[int, dict[int, str]]:
    _, events = read_events(project.read_bytes())
    out: dict[int, dict[int, str]] = {}
    for insert in range(len(_insert_starts(events))):
        for slot in range(SLOTS):
            group = _plugin_group_before(events, _slot_marker(events, insert, slot))
            if group:
                out.setdefault(insert, {})[slot] = _name(events[group[0]][1])
    return out


def add_effects(project: Path, effects: list[tuple[int, int, str]]) -> dict:
    """effects: (insert, slot, plugin name). Slots must be empty. Keeps a backup."""
    header, events = read_events(project.read_bytes())
    templates: dict[str, list] = {}
    for insert, slot, plugin in effects:
        marker = _slot_marker(events, insert, slot)
        if _plugin_group_before(events, marker):
            raise ValueError(f"insert {insert} slot {slot} is not empty")
        group = templates.setdefault(plugin, find_effect_template(plugin))
        placed = []
        for e, v in group:
            if e == EV_WRAPPER:
                v = struct.pack("<II", insert, slot) + v[8:]
            placed.append((e, v))
        events[marker:marker] = placed

    data = write_events(header, events)
    read_events(data)
    backup = make_backup(project, "fx")
    atomic_write(project, data)
    placed = list_slots(project)
    for insert, slot, plugin in effects:
        if placed.get(insert, {}).get(slot, "").lower() != plugin.lower():
            shutil.copy2(backup, project)
            raise ValueError(f"verification failed at insert {insert} slot {slot}; restored backup")
    return {"added": len(effects), "backup": str(backup)}
