import struct

import pytest

from fl_studio_mcp.utils import zenology
from fl_studio_mcp.utils.zenology import (
    STATE_TEMPLATE,
    ZenologyError,
    _numbers,
    _select_on_screen,
    describe_tone,
    pick_window,
    plan_clicks,
    read_tone_from_state,
    shown_number,
    title_matches,
    unique_channel_name,
)


def test_template_state_reads_supertnr():
    info = read_tone_from_state(STATE_TEMPLATE.read_bytes())
    assert info == {"pack_id": 1008, "index": 230, "number": 231, "name": "SuperTnr SRX"}
    full = describe_tone(info)
    assert full["pack"] == "EXZ008_VintKeys" and full["consistent"] is True


def test_size_field_ignores_trailing_bytes():
    data = STATE_TEMPLATE.read_bytes() + b"\x00garbage after the plist"
    assert read_tone_from_state(data)["name"] == "SuperTnr SRX"


def test_non_zenology_raises():
    with pytest.raises(ZenologyError):
        read_tone_from_state(b"\x00" * 64)


def test_title_matches_exact():
    assert title_matches("Upright (Master)", "Upright")
    assert not title_matches("Upright Bass (Master)", "Upright")
    assert title_matches("Upright Bass", "Upright Bass")


def test_unique_channel_name_and_plan_clicks():
    assert unique_channel_name("Upright", ["Kick"]) == "Upright"
    assert unique_channel_name("Upright", ["Upright", "Upright 2"]) == "Upright 3"
    assert plan_clicks(8, 9) == ("next", 1) and plan_clicks(10, 9) == ("prev", 1)


TONE = {"pack": "SDZ027_NuSunset", "pack_id": 5027, "no": 9, "name": "Upright"}


class Screen:
    """ZENOLOGY on screen: the first arrow press overshoots by one (as seen in the wild)."""

    def __init__(self, start=10):
        self.number = start
        self.clicks = []

    def click(self, rect, direction):
        self.clicks.append(direction)
        step = -1 if direction == "prev" else 1
        self.number += step * (2 if len(self.clicks) == 1 else 1)

    def observe(self, rect, title):
        names = {8: "Light Padding", 9: "Upright", 10: "Ambient Noise"}
        text = f"EQ 1:\n{self.number:02d}:\n{names.get(self.number, '?')}"
        return [1, self.number], text


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(zenology.time, "sleep", lambda s: None)


def test_select_loop_corrects_and_closes(fake_send, quick):
    screen, send = Screen(), fake_send(lambda a, p: {"success": True})
    res = _select_on_screen(TONE, 12, "Upright", True, send, click=screen.click,
                            observe=screen.observe, find_window=lambda: (0, 0, 500, 400))
    assert screen.clicks == ["prev", "next"] and res["verified"] and res["verified_by"] == "ocr"
    assert ("channels.closeEditor", {"index": 12}) in send.calls


def test_state_check_overrides_screen(fake_send, quick):
    screen, send = Screen(), fake_send(lambda a, p: {"success": True})
    wrong = {"pack_id": 5027, "number": 8, "name": "Light Padding"}
    res = _select_on_screen(TONE, 12, "Upright", True, send, read_state=lambda ch: wrong,
                            click=screen.click, observe=screen.observe,
                            find_window=lambda: (0, 0, 500, 400))
    assert not res["verified"] and res["verified_by"] == "state" and res["loaded"] == wrong


def test_window_closed_on_error(fake_send, quick):
    send = fake_send(lambda a, p: {"success": True})

    def boom(rect, direction):
        raise ZenologyError("click failed")

    with pytest.raises(ZenologyError):
        _select_on_screen(TONE, 3, "Upright", True, send, click=boom,
                          observe=lambda r, t: ([], ""), find_window=lambda: (0, 0, 1, 1))
    assert ("channels.closeEditor", {"index": 3}) in send.calls


def test_unreadable_screen_falls_back_to_state(fake_send, quick):
    send = fake_send(lambda a, p: {"success": True})

    def blind(rect, title):
        raise ZenologyError("screencapture failed")

    res = _select_on_screen(TONE, 4, "Upright", True, send,
                            read_state=lambda ch: {"pack_id": 5027, "number": 9, "name": "Upright"},
                            click=lambda r, d: None, observe=blind,
                            find_window=lambda: (0, 0, 1, 1))
    assert res["verified"] and res["verified_by"] == "state" and res["clicks"] == 1


def test_names_are_16_chars_at_stride_4():
    blob = b"".join(struct.pack(">II", 0x1130 + 4 * k, ord(c)) for k, c in enumerate("Marcato Vc"))
    blob += struct.pack(">II", 0xFFFC008, 1007) + struct.pack(">II", 0xFFFC00C, 73)
    import plistlib

    plist = plistlib.dumps({"jp.co.roland.ZENOLOGY.data": blob}, fmt=plistlib.FMT_BINARY)
    payload = b"\x00" * 8 + struct.pack("<q", len(plist)) + plist
    assert read_tone_from_state(payload) == {"pack_id": 1007, "index": 73, "number": 74,
                                             "name": "Marcato Vc"}


def test_pick_window_exact_loose_and_never_longer_names():
    wins = [("Upright Bass (Master)", (1, 1, 1, 1)), ("Upright - ZENOLOGY", (2, 2, 2, 2))]
    assert pick_window(wins, "Upright") == (2, 2, 2, 2)
    assert pick_window(wins + [("Upright (Insert 3)", (3, 3, 3, 3))], "Upright") == (3, 3, 3, 3)
    assert pick_window([("Upright Bass (Master)", (1, 1, 1, 1))], "Upright") is None
    assert pick_window([("Sad S.VlnSRX 2", (4, 4, 4, 4))], "Sad S.VlnSRX") is None


def test_numbers_and_shown_number():
    text = "v 067:\nSad S.VlnSRX\nEQ 1:\nPart 12: x"
    assert _numbers(text) == [67, 1]
    assert shown_number([1, 67], 67) == 67
    assert shown_number([1, 66], 67) == 66
    assert shown_number([], 67) is None


def test_far_off_reading_stops_clicking(fake_send, quick):
    clicks = []
    res = _select_on_screen(
        TONE, 5, "Upright", True, fake_send(lambda a, p: {"success": True}),
        read_state=lambda ch: {"pack_id": 5027, "number": 9, "name": "Upright"},
        click=lambda r, d: clicks.append(d), observe=lambda r, t: ([40], "40:"),
        find_window=lambda: (0, 0, 1, 1),
    )
    assert clicks == ["prev"] and res["verified"] and res["verified_by"] == "state"
