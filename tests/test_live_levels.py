import pytest

from fl_studio_mcp.utils.live_levels import (
    PREPARE_CODE,
    RESTORE_CODE,
    SNAP_CODE,
    STATE_CODE,
    measure_levels,
)


class FakeFL:
    """A pad rings out from before start_bar; the bass plays inside the window."""

    def __init__(self, tail_snaps=5, fail_after_start=False):
        self.clock = 0.0
        self.pos = 999
        self.playing = False
        self.pad = 0.5
        self.tail = tail_snaps
        self.fail = fail_after_start
        self.restored = None

    def now(self):
        return self.clock

    def sleep(self, s):
        self.clock += s
        if self.playing:
            self.pos += int(s * 96 * 2)  # 120 bpm, ppq 96

    def __call__(self, code, *, timeout=10.0, args=None):
        if code == STATE_CODE:
            return {"playing": False, "pos": 999, "mode": 1}
        if code == PREPARE_CODE:
            self.pos = (args["bar"] - 1) * 4 * 96
            return {"ppq": 96, "bpm": 120.0, "target": self.pos, "names": ["Master", "Pad", "Bass"]}
        if code == SNAP_CODE:
            peaks = {}
            if self.tail > 0:  # release tail of what played before
                self.tail -= 1
                peaks["1"] = self.pad
            if self.playing:
                if self.fail:
                    raise RuntimeError("FL went away")
                peaks["2"] = 0.4
            return [self.pos, peaks]
        if code.startswith("transport.start()"):
            self.playing = True
            return 1
        if code == RESTORE_CODE:
            self.restored = args
            return 1
        raise AssertionError(code)


def run(fl, **kw):
    return measure_levels(fl, 5, 2.0, clock=fl.now, sleep=fl.sleep, **kw)


def test_ignores_preroll_tail():
    fl = FakeFL(tail_snaps=5)
    res = run(fl)
    assert res["settled"] and [t["name"] for t in res["tracks"]] == ["Bass"]
    assert fl.restored == {"playing": False, "pos": 999, "mode": 1}


def test_settle_timeout_reports_floor():
    fl = FakeFL(tail_snaps=1000)
    res = run(fl, settle_s=0.5)
    assert not res["settled"] and res["floor"] == {"1": 0.5}
    assert "warning" in res


def test_state_restored_on_error():
    fl = FakeFL(tail_snaps=0, fail_after_start=True)
    with pytest.raises(RuntimeError):
        run(fl)
    assert fl.restored is not None
