import asyncio

import pytest

from fl_studio_mcp.tools.transport import SET_SONG_POS
from fl_studio_mcp.utils import fl_files, fl_mixer, live_levels
from fl_studio_mcp.utils.fl_params import FL_PARAM_LIB


def test_all_tools_register_with_descriptions():
    from fl_studio_mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    names = [t.name for t in tools]
    assert len(names) == len(set(names)) >= 100
    missing = [t.name for t in tools if not (t.description or "").strip()]
    assert not missing
    for new in ("fl_transform_notes", "fl_render_stems", "fl_get_mixer_routing",
                "fl_find_plugin_params", "fl_set_plugin_params", "fl_save_project_as",
                "fl_open_project", "fl_restore_backup", "fl_zenology_current_tone"):
        assert new in names


@pytest.mark.parametrize("code", [
    FL_PARAM_LIB, fl_mixer.ROUTING_CODE, fl_mixer.SETUP_CODE, SET_SONG_POS,
    fl_files._CAPTURE, fl_files._PROBE, fl_files._restore_code(
        {"playing": False, "pos": 0, "mode": 1, "pattern": 1, "windows": {}}),
    live_levels.STATE_CODE, live_levels.PREPARE_CODE, live_levels.SNAP_CODE,
    live_levels.RESTORE_CODE,
])
def test_fl_side_snippets_compile(code):
    compile(code, "<fl>", "exec")
