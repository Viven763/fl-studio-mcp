import pytest

from fl_studio_mcp.tools.transport import SET_SONG_POS
from fl_studio_mcp.utils.fl_exec import build_code


class FakeTransport:
    def __init__(self):
        self.calls = []

    def setSongPos(self, value, mode=-1):  # noqa: N802 - FL API name
        self.calls.append((value, mode))

    def getSongPosHint(self):  # noqa: N802
        return "9:01:00"


class FakeGeneral:
    def getRecPPQ(self):  # noqa: N802
        return 96


def run(unit, pos, mode=None):
    transport = FakeTransport()
    scope = {"transport": transport, "general": FakeGeneral()}
    exec(build_code(SET_SONG_POS, {"unit": unit, "pos": pos, "mode": mode}), scope)
    return transport.calls, scope["result"]


@pytest.mark.parametrize(
    "unit,pos,expected",
    [
        ("seconds", 12.345, (12345, 0)),
        ("ms", 500, (500, 0)),
        ("ticks", 768, (768, 2)),
        ("bars", 9, (8 * 4 * 96, 2)),
        ("fraction", 0.5, (0.5, -1)),
    ],
)
def test_unit_conversions(unit, pos, expected):
    calls, hint = run(unit, pos)
    assert calls == [expected]
    assert hint == "9:01:00"


def test_raw_mode_passthrough():
    assert run("seconds", 3, mode=1)[0] == [(3, 1)]
    assert run("seconds", 0.25, mode=-1)[0] == [(0.25, -1)]
