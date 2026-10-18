from pathlib import Path

import pytest

from fl_studio_mcp.utils import fl_render
from fl_studio_mcp.utils.fl_render import render_pids, render_project, render_versions
from fl_studio_mcp.utils.flp_edit import channel_enabled, set_channels_enabled
from fl_studio_mcp.utils.flp_events import read_events


class FakeInstance:
    """Stands in for the hidden FL: renders one copy per poll, then exits (or not)."""

    instances: list["FakeInstance"] = []

    def __init__(self, work: Path, mode: str = "ok"):
        self.work, self.mode = work, mode
        self.launched = self.stopped = False
        self.seen: dict[str, list[bool]] = {}
        FakeInstance.instances.append(self)

    def launch(self):
        self.launched = True
        for flp in sorted(self.work.glob("*.flp")):
            self.seen[flp.stem] = channel_enabled(read_events(flp.read_bytes())[1])

    def pids(self):
        if not self.launched:
            return []
        if self.mode == "stall":
            return [4242]
        todo = [f for f in sorted(self.work.glob("*.flp")) if not f.with_suffix(".wav").exists()]
        if todo and not (self.mode == "partial" and len(todo) == 1):
            todo[0].with_suffix(".wav").write_bytes(b"RIFF" + todo[0].stem.encode())
            return [4242]
        return [4242] if self.mode == "stays_open" else []

    def stop(self):
        self.stopped = True


@pytest.fixture
def clock():
    state = {"t": 0.0}
    return state


def run(project, versions, outputs, mode, clock, **kw):
    FakeInstance.instances.clear()

    def sleep(s):
        clock["t"] += s

    return render_versions(project, versions, outputs,
                           instance_factory=lambda work: FakeInstance(work, mode),
                           sleep=sleep, clock=lambda: clock["t"], **kw)


def test_one_instance_renders_all_versions(template_flp, tmp_path, clock):
    out = {"mix": tmp_path / "out" / "mix.wav", "kick": tmp_path / "out" / "01 Kick.wav"}
    res = run(template_flp, {"mix": None, "kick": lambda evs: set_channels_enabled(evs, {0})},
              out, "ok", clock)
    inst = FakeInstance.instances[0]
    assert len(FakeInstance.instances) == 1 and inst.launched and inst.stopped
    assert inst.seen["v000"] == [True] * 5  # the mix as saved
    assert inst.seen["v001"] == [True, False, False, False, False]  # kick only
    assert res == out and all(p.exists() for p in out.values())
    assert out["kick"].read_bytes() == b"RIFFv001"
    assert not inst.work.exists()  # temp folder cleaned up


def test_finishes_when_fl_stays_open(template_flp, tmp_path, clock):
    out = {"mix": tmp_path / "mix.wav"}
    run(template_flp, {"mix": None}, out, "stays_open", clock)
    assert out["mix"].exists() and FakeInstance.instances[0].stopped


def test_stall_is_reported_and_instance_stopped(template_flp, tmp_path, clock):
    with pytest.raises(RuntimeError, match="stalled"):
        run(template_flp, {"mix": None}, {"mix": tmp_path / "mix.wav"}, "stall", clock,
            startup_grace=10)
    assert FakeInstance.instances[0].stopped
    assert not FakeInstance.instances[0].work.exists()


def test_exit_without_all_files_names_missing(template_flp, tmp_path, clock):
    with pytest.raises(RuntimeError, match=r"exited without rendering \['b'\]"):
        outs = {"a": tmp_path / "a.wav", "b": tmp_path / "b.wav"}
        run(template_flp, {"a": None, "b": None}, outs, "partial", clock)


def test_outputs_must_match_versions(template_flp, tmp_path):
    with pytest.raises(ValueError):
        render_versions(template_flp, {"a": None}, {})


def test_render_project_default_output(template_flp, monkeypatch, clock):
    def fake(project, versions, outputs, timeout=0):
        return {"render": outputs["render"]}

    monkeypatch.setattr(fl_render, "render_versions", fake)
    assert render_project(template_flp) == template_flp.with_name("Song.render.wav")


def test_render_pids_only_matches_this_folder():
    ps = (" 9297 /Applications/FL Studio 2026.app/Contents/MacOS/OsxFL\n"
          "28289 /Applications/FL Studio 2026.app/Contents/MacOS/OsxFL -R -Ewav -F/tmp/x/a/\n"
          "28934 /Applications/FL Studio 2026.app/Contents/MacOS/OsxFL -R -Ewav -F/tmp/x/b/\n"
          "  311 /usr/bin/python3 render -F/tmp/x/a/\n"
          "garbage � line\n")
    assert render_pids(ps, Path("/tmp/x/a")) == [28289]
