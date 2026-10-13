import struct

import pytest

from fl_studio_mcp.utils import zenology
from fl_studio_mcp.utils.fl_files import channel_bounds
from fl_studio_mcp.utils.flp_events import read_events, write_events
from fl_studio_mcp.utils.zenology import (
    ADDR_NAME,
    ADDR_PACK,
    ADDR_TONE,
    FST_TEMPLATE,
    HOST_ADDRS,
    NAME_CHARS,
    NAME_STRIDE,
    STATE_TEMPLATE,
    ZenologyError,
    harvest,
    library_index,
    load_from_library,
    patch_state,
    read_tone_from_state,
    set_channel_tone,
    state_values,
)

HONKY = {"pack": "EXZ005_Studio", "pack_id": 1005, "no": 93, "name": "Honky Tenor"}
SUPER = {"pack": "EXZ008_VintKeys", "pack_id": 1008, "no": 231, "name": "SuperTnr SRX"}


def fake_state(pack_id: int, number: int, name: str, host=(0, 0, 0, 0, 0)) -> bytes:
    """The template state relabelled as another tone (enough for file-level tests)."""
    chars = name.ljust(NAME_CHARS)[:NAME_CHARS]
    updates = {ADDR_PACK: pack_id, ADDR_TONE: number - 1}
    updates.update({ADDR_NAME + NAME_STRIDE * k: ord(c) for k, c in enumerate(chars)})
    updates.update(dict(zip(HOST_ADDRS, host)))
    return patch_state(STATE_TEMPLATE.read_bytes(), updates)


def fst_with_state(path, state: bytes, name: str = "x"):
    header, events = read_events(FST_TEMPLATE.read_bytes())
    events = [(e, state if e == 213 else (name + "\0").encode("utf-16-le") if e == 203 else v)
              for e, v in events]
    path.write_bytes(write_events(header, events))
    return path


@pytest.fixture
def library(tmp_path, monkeypatch):
    if not FST_TEMPLATE.exists():
        pytest.skip("ZENOLOGY preset template not installed")
    monkeypatch.setenv("FL_USER_DATA", str(tmp_path / "FLUser"))
    return tmp_path


def test_patch_state_roundtrip_and_layout_checks():
    state = STATE_TEMPLATE.read_bytes()
    out = patch_state(state, {ADDR_TONE: 99})
    assert read_tone_from_state(out)["index"] == 99
    assert struct.unpack_from("<q", out, 233)[0] == len(out) - 241
    assert struct.unpack_from("<q", out, 221)[0] == len(out) - 241 + 12
    with pytest.raises(ZenologyError, match="lacks fields"):
        patch_state(state, {0x7FFFFFF0: 1})
    assert patch_state(state, {0x7FFFFFF0: 1}, strict=False)
    with pytest.raises(ZenologyError, match="layout"):
        patch_state(b"\0" * 8 + state, {ADDR_TONE: 1})


def test_harvest_takes_consistent_tones_only_and_resets_part(library):
    src = library / "src"
    src.mkdir()
    fst_with_state(src / "a.fst", fake_state(1005, 93, "Honky Tenor", host=(1, 1, 0, 1, 15)))
    fst_with_state(src / "b.fst", fake_state(1005, 94, "Honky Tenor"))  # mismatching name
    res = harvest([src / "a.fst", src / "b.fst"])
    assert [(a["pack"], a["number"], a["name"]) for a in res["added"]] == [
        ("EXZ005_Studio", 93, "Honky Tenor")]
    path = library_index()[(1005, 93)]
    assert path.parent.name == "EXZ005_Studio" and path.name == "Honky Tenor.fst"
    state = next(v for e, v in read_events(path.read_bytes())[1] if e == 213)
    assert set(state_values(state, HOST_ADDRS).values()) == {0}
    assert harvest([src / "a.fst"])["added"] == []  # already known


def _project_with_zenology(template_flp, state: bytes) -> int:
    """Put a ZENOLOGY state into the template project's last channel; returns its index."""
    header, events = read_events(template_flp.read_bytes())
    start, end = channel_bounds(events)[-1]
    idx = next(i for i in range(start, end) if events[i][0] == 213)
    events[idx] = (213, state)
    template_flp.write_bytes(write_events(header, events))
    return len(channel_bounds(events)) - 1


def _channel_state(project, channel):
    _, events = read_events(project.read_bytes())
    start, end = channel_bounds(events)[channel]
    return next(v for e, v in events[start:end] if e == 213)


def test_set_channel_tone_swaps_sound_keeps_part(library, template_flp):
    src = library / "src"
    src.mkdir()
    fst_with_state(src / "honky.fst", fake_state(1005, 93, "Honky Tenor"))
    harvest([src / "honky.fst"])
    ch = _project_with_zenology(template_flp, fake_state(1008, 231, "SuperTnr SRX",
                                                         host=(1, 1, 0, 0, 0)))
    before = template_flp.read_bytes()
    res = set_channel_tone(template_flp, ch, HONKY, rename="Honky Tenor")
    assert res["from"] == {"pack": "EXZ008_VintKeys", "number": 231, "name": "SuperTnr SRX"}
    assert res["renamed"] == "Honky Tenor" and "backups" in res["backup"]
    state = _channel_state(template_flp, ch)
    assert read_tone_from_state(state)["number"] == 93
    assert state_values(state, HOST_ADDRS) == dict(zip(HOST_ADDRS, (1, 1, 0, 0, 0)))
    from fl_studio_mcp.utils.flp_edit import channel_names

    names = channel_names(read_events(template_flp.read_bytes())[1])
    assert names[ch] == "Honky Tenor" and len(names) == 5
    assert template_flp.read_bytes() != before


def test_set_channel_tone_refuses_non_zenology_and_missing(library, template_flp):
    src = library / "src"
    src.mkdir()
    fst_with_state(src / "honky.fst", fake_state(1005, 93, "Honky Tenor"))
    harvest([src / "honky.fst"])
    before = template_flp.read_bytes()
    with pytest.raises(ZenologyError, match="no plugin state"):
        set_channel_tone(template_flp, 0, HONKY)  # a sampler channel
    with pytest.raises(ZenologyError, match="not a ZENOLOGY"):
        set_channel_tone(template_flp, 4, HONKY)  # another plugin
    monkey = dict(HONKY, no=500, name="Nope")
    with pytest.raises(ZenologyError, match="not in the tone library"):
        set_channel_tone(template_flp, 0, monkey)
    assert template_flp.read_bytes() == before


def test_load_from_library_without_screen(library, monkeypatch, fake_send):
    src = library / "src"
    src.mkdir()
    fst_with_state(src / "s.fst", fake_state(1008, 231, "SuperTnr SRX"))
    harvest([src / "s.fst"])
    rack = ["Kick", "SuperTnr SRX"]
    opened = []

    def handler(action, params):
        if action == "channels.getAll":
            return {"channels": [{"name": n} for n in rack]}
        if action == "channels.getCount":
            return {"count": len(rack)}
        return {"success": True}

    def fake_open(path, background=True):
        opened.append((path, background))
        rack.append(path.stem)

    monkeypatch.setattr(zenology, "open_in_fl", fake_open)
    monkeypatch.setattr(zenology, "CACHE", library / "cache")
    monkeypatch.setattr(zenology.time, "sleep", lambda s: None)
    send = fake_send(handler)
    res = load_from_library(SUPER, send, read_state=lambda ch: {
        "pack_id": 1008, "number": 231, "name": "SuperTnr SRX"})
    assert res["channel"] == 2 and res["channel_name"] == "SuperTnr SRX 2"
    assert res["source"] == "library" and res["clicks"] == 0 and res["verified"]
    assert opened[0][1] is True  # FL stays in the background
    names = [v for e, v in read_events(opened[0][0].read_bytes())[1] if e == 203]
    assert names == ["SuperTnr SRX 2\0".encode("utf-16-le")]
    assert ("channels.closeEditor", {"index": 2}) in send.calls
