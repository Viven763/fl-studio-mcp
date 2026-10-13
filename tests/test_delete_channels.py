import pytest

from fl_studio_mcp.utils.flp_edit import delete_channels
from fl_studio_mcp.utils.flp_notes import read_pattern_notes, write_parts

LIVE = ["Kick", "Upright", "Upright", "Snare", "Synth"]  # what FL shows (duplicate names)


def test_delete_by_index_with_duplicate_names(template_flp):
    write_parts(template_flp, [{"pattern": 1, "channel": 3,
                                "notes": [{"midi": 38, "time": 0, "duration": 0.25}]}])
    res = delete_channels(template_flp, indices=[2], live_names=LIVE)
    assert res["deleted"] == [{"index": 2, "name": "Upright"}]
    assert res["channels"] == ["Kick", "Upright", "Snare", "Synth"]
    assert "Song backups" in res["backup"]
    assert [n["channel"] for n in read_pattern_notes(template_flp)[1]] == [2]  # remapped 3 -> 2


def test_ambiguous_name_lists_indices(template_flp):
    before = template_flp.read_bytes()
    with pytest.raises(ValueError, match=r"'Upright': \[1, 2\]"):
        delete_channels(template_flp, names=["Upright"], live_names=LIVE)
    assert template_flp.read_bytes() == before


def test_live_names_mismatch_refused(template_flp):
    with pytest.raises(ValueError, match="different channels"):
        delete_channels(template_flp, indices=[0], live_names=["Only", "Two"])


def test_names_still_work(template_flp):
    res = delete_channels(template_flp, names=["808 Clap"])
    assert res["deleted"] == [{"index": 1, "name": "808 Clap"}]
    with pytest.raises(ValueError, match="no channel named"):
        delete_channels(template_flp, names=["808 Clap"])
