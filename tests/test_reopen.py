from pathlib import Path

import pytest

from fl_studio_mcp.utils import fl_files
from fl_studio_mcp.utils.flp_events import read_events


class FakeFL:
    """Minimal FL for reopen_and_wait: song position, channel names and a pending reload."""

    def __init__(self, names, reload_after_polls=None):
        self.names = list(names)
        self.pos = 1234
        self.polls = 0
        self.reload_after = reload_after_polls
        self.opened = False
        self.restored = None

    def send(self, action, params=None, timeout=2.0):
        code = (params or {}).get("code", "")
        if code.startswith("result = [transport.getSongPos(2)"):
            if self.opened:
                self.polls += 1
                if self.reload_after is not None and self.polls >= self.reload_after:
                    self.pos = 0  # a freshly loaded project starts at the beginning
            return {"result": [self.pos, self.names]}
        if "setSongPos(7, 2)" in code:
            state = {"playing": False, "pos": self.pos, "mode": 1, "pattern": 3, "windows": {}}
            self.pos = fl_files.SENTINEL_TICKS
            return {"result": state}
        if "st = " in code:
            self.restored = code
            return {"result": True}
        return {"result": None}


@pytest.fixture
def fast_clock(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(fl_files.time, "time", lambda: clock["t"])

    def sleep(s):
        clock["t"] += s

    monkeypatch.setattr(fl_files.time, "sleep", sleep)
    return clock


def test_waits_for_reload_then_restores_ticks(monkeypatch, fast_clock):
    fl = FakeFL(["Kick", "Bass"], reload_after_polls=4)
    monkeypatch.setattr(fl_files, "open_in_fl", lambda p: setattr(fl, "opened", True))
    assert fl_files.reopen_and_wait(fl.send, Path("/tmp/x.flp"))
    assert fl.polls == 4
    assert "transport.setSongPos(st[\"pos\"], 2)" in fl.restored
    assert "'pos': 1234" in fl.restored


def test_grace_when_position_never_moves(monkeypatch, fast_clock):
    fl = FakeFL(["Kick"], reload_after_polls=None)
    monkeypatch.setattr(fl_files, "open_in_fl", lambda p: setattr(fl, "opened", True))
    start = fast_clock["t"]
    assert fl_files.reopen_and_wait(fl.send, Path("/tmp/x.flp"), grace=3.0)
    assert fast_clock["t"] - start >= 3.0


def test_times_out_when_names_differ(monkeypatch, fast_clock):
    fl = FakeFL(["Kick"], reload_after_polls=1)
    monkeypatch.setattr(fl_files, "open_in_fl", lambda p: setattr(fl, "opened", True))
    assert not fl_files.reopen_and_wait(fl.send, Path("/tmp/x.flp"), timeout=5, expected=["X"])
    assert fl.restored is None


def test_channel_bounds_stop_before_mixer(template_flp):
    _, events = read_events(template_flp.read_bytes())
    bounds = fl_files.channel_bounds(events)
    assert bounds, "template has channels"
    first_insert = next(i for i, (e, _) in enumerate(events) if e == 236)
    for start, end in bounds:
        assert events[start][0] == 64 and events[start + 1][0] == 21
        assert end <= first_insert
    assert all(b[1] <= c[0] for b, c in zip(bounds, bounds[1:]))
