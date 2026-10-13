import time
from pathlib import Path

import pytest

from fl_studio_mcp.tools.project import register_project_tools, target_path
from fl_studio_mcp.utils import fl_exec, fl_files


class ToolBox:
    """Collects tool functions the way FastMCP's decorator would."""

    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeConn:
    def __init__(self):
        self.calls = []

    def send_command(self, action, params=None, timeout=2.0):
        self.calls.append(action)
        return {"success": True}


@pytest.fixture
def box(monkeypatch, tmp_path):
    """Project tools wired to fakes: FL 'saves' into whatever file it has open."""
    state = {"open": tmp_path / "Song" / "Song.flp", "reopened": []}
    state["open"].parent.mkdir()
    state["open"].write_bytes(b"song")
    monkeypatch.setattr(fl_files, "_last_project", None)

    def save(send, timeout=10.0):
        return state["open"]

    def reopen(send, project, **kw):
        state["open"] = project
        state["reopened"].append((project, kw))
        return True

    def run(code, *, timeout=10.0, args=None, send=None):
        if "getChangedFlag" in code:
            return 0
        return ["Kick", "Bass"]

    conn = FakeConn()
    monkeypatch.setattr(fl_files, "save_project_and_locate", save)
    monkeypatch.setattr(fl_files, "reopen_and_wait", reopen)
    monkeypatch.setattr(fl_exec, "exec_in_fl", run)
    monkeypatch.setattr("fl_studio_mcp.utils.connection.get_connection", lambda: conn)
    b = ToolBox()
    register_project_tools(b)
    b.state = state
    return b


def test_target_path(tmp_path):
    cur = tmp_path / "Song" / "Song.flp"
    assert target_path(cur, "Song v2") == tmp_path / "Song" / "Song v2.flp"
    assert target_path(cur, "/abs/X.flp") == Path("/abs/X.flp")
    inside = tmp_path / "Song" / "Backup" / "Song.before-notes-20261013-190000.flp"
    assert target_path(inside, "Song") == tmp_path / "Song" / "Song.flp"


def test_save_as_copies_opens_verifies(box):
    src = box.state["open"]
    res = box.tools["fl_save_project_as"]("Song v2")
    target = src.with_name("Song v2.flp")
    assert res == {"project": str(target), "previous": str(src), "reloaded": True,
                   "verified": True}
    assert target.read_bytes() == b"song"
    assert target.stat().st_mtime < time.time() - 60  # not "new" until FL saves into it


def test_save_as_no_overwrite(box):
    box.state["open"].with_name("Taken.flp").write_bytes(b"other")
    assert "exists" in box.tools["fl_save_project_as"]("Taken")["error"]
    assert "already has open" in box.tools["fl_save_project_as"]("Song")["error"]


def test_open_refuses_backup(box, tmp_path):
    bak = tmp_path / "Song" / "Song.before-fx-20261011-195006.flp"
    bak.write_bytes(b"x")
    assert "backup" in box.tools["fl_open_project"](str(bak))["error"]
    other = tmp_path / "Other.flp"
    other.write_bytes(b"x")
    res = box.tools["fl_open_project"](str(other))
    assert res["loaded"] and box.state["open"] == other


def test_restore_backup_makes_safety_copy(box, tmp_path):
    folder = tmp_path / "Song"
    (folder / "Backup").mkdir()
    bak = folder / "Backup" / "Song.before-notes-20261013-190000.flp"
    bak.write_bytes(b"old version")
    res = box.tools["fl_restore_backup"](str(bak))
    assert (folder / "Song.flp").read_bytes() == b"old version"
    assert Path(res["safety_backup"]).read_bytes() == b"song"
    assert ".before-restore-" in res["safety_backup"] and res["loaded"]


def test_tidy_dry_run_then_move(box, tmp_path, monkeypatch):
    folder = tmp_path / "Song"
    for name in ("Song.before-fx-20261011-195006.flp", "Song.before-notes-20261011-224511.flp"):
        (folder / name).write_bytes(b"x")
    monkeypatch.setattr(fl_files, "_last_project", folder / "Song.before-notes-20261011-224511.flp")
    (folder / "Backup").mkdir(exist_ok=True)
    (folder / "Backup" / "Song.before-notes-20261014-001916.flp").write_bytes(b"x")
    (folder / "Backup" / "keep.db").write_bytes(b"x")
    dry = box.tools["fl_tidy_backups"](str(folder))
    assert dry["files"] == ["Song.before-fx-20261011-195006.flp",
                            "Song.before-notes-20261014-001916.flp"]
    assert dry["backup_folders"] == [str(folder / "Song backups")]
    assert dry["skipped"] and dry["dry_run"]
    assert (folder / "Song.before-fx-20261011-195006.flp").exists()
    done = box.tools["fl_tidy_backups"](str(folder), dry_run=False)
    moved = folder / "Song backups"
    assert (moved / "Song.before-fx-20261011-195006.flp").exists()
    assert (moved / "Song.before-notes-20261014-001916.flp").exists()
    assert (folder / "Backup" / "keep.db").exists()  # not ours: untouched
    assert not (folder / "Song.before-fx-20261011-195006.flp").exists() and not done["dry_run"]
