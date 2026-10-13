import pytest

from fl_studio_mcp.tools.transport import SET_SONG_POS
from fl_studio_mcp.utils.fl_exec import build_code


class FakeTransport:
    def __init__(self):
        self.calls = []

    def setSongPos(self, value, mode=-1):  # noqa: N802 - FL API name
        self.calls.append((value, mode))

    def getSongLength(self, mode):  # noqa: N802
        return 3840  # 10 bars at PPQ 96


class FakeGeneral:
    def getRecPPQ(self):  # noqa: N802
        return 96


class FakeMixer:
    def getCurrentTempo(self):  # noqa: N802
        return 120000  # 120 BPM


def run(unit, pos, mode=None):
    transport = FakeTransport()
    scope = {"transport": transport, "general": FakeGeneral(), "mixer": FakeMixer()}
    exec(build_code(SET_SONG_POS, {"unit": unit, "pos": pos, "mode": mode}), scope)
    return transport.calls, scope["result"]


@pytest.mark.parametrize(
    "unit,pos,ticks,hint",
    [
        ("seconds", 12.345, 2370, "7:03:18"),
        ("ms", 500, 96, "1:05:00"),
        ("ticks", 768, 768, "3:01:00"),
        ("bars", 9, 3072, "9:01:00"),
        ("fraction", 0.5, 1920, "6:01:00"),
    ],
)
def test_unit_conversions(unit, pos, ticks, hint):
    calls, got = run(unit, pos)
    assert calls == [(ticks, 2)]
    assert got == hint


def test_raw_mode_maps_to_units():
    assert run("bars", 3, mode=1)[0] == [(576, 2)]  # 3 s at 120 BPM
    assert run("bars", 0.25, mode=-1)[0] == [(960, 2)]
    assert run("bars", 100, mode=2)[0] == [(100, 2)]
