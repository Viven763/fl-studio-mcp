import pytest
from fl_sim import EQ_TYPES, FLSim, Param

from fl_studio_mcp.utils import fl_params
from fl_studio_mcp.utils.fl_params import (
    FL_PARAM_LIB,
    param_names,
    param_options,
    resolve_param,
    set_params,
)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(fl_params, "_NAME_CACHE", {})


def eq_sim():
    params = [Param("Band 1 level", 0.5, "db"), Param("Band 1 freq", 0.3, "hz"),
              Param("Band 1 type", 5 / 7, options=EQ_TYPES), Param("Band 2 type", 6 / 7,
                                                                    options=EQ_TYPES)]
    return FLSim({(8, 0): ("Fruity parametric EQ 2", params)})


def test_scan_groups_ranges_and_restores():
    sim = eq_sim()
    res = param_options(8, 0, 2, exec_fn=sim)
    assert res["options"] == EQ_TYPES and not res["partial"]
    assert sim.plugins.plugins[(8, 0)][1][2].value == pytest.approx(5 / 7)


def test_set_text_midpoint():
    sim = eq_sim()
    res = set_params(8, 0, [{"param": 2, "text": "high pass"}], exec_fn=sim)
    assert res["params"]["2"]["text"] == "High pass" and not res["errors"]
    assert res["params"]["2"]["value"] == pytest.approx(3 / 7, abs=0.05)


def test_text_prefix_unique_and_ambiguous():
    sim = eq_sim()
    assert set_params(8, 0, [{"param": 2, "text": "peak"}], exec_fn=sim)["params"]["2"]["text"] \
        == "Peaking"
    res = set_params(8, 0, [{"param": 2, "text": "high"}], exec_fn=sim)  # pass or shelf?
    assert res["errors"] and "Low pass" in res["errors"][0]


def test_no_match_restores_and_lists_options():
    sim = eq_sim()
    res = set_params(8, 0, [{"param": 3, "text": "banana"}], exec_fn=sim)
    assert res["params"] == {} and "options" in res["errors"][0]
    assert sim.plugins.plugins[(8, 0)][1][3].value == pytest.approx(6 / 7)


def test_target_and_value_by_name():
    sim = eq_sim()
    res = set_params(8, 0, [{"param": "band 1 level", "target": -6.0},
                            {"param": 1, "value": 0.25}], exec_fn=sim)
    assert res["params"]["0"]["text"] == "-6.0dB"
    assert res["params"]["1"]["value"] == 0.25


def test_item_errors_reported():
    res = set_params(8, 0, [{"param": 0}, {"param": "nope", "value": 1}], exec_fn=eq_sim())
    assert len(res["errors"]) == 2 and "no parameter named" in res["errors"][1]


def test_names_chunked_and_cached():
    params = [Param(f"Very long parameter name number {i:04d}") for i in range(1000)]
    sim = FLSim({(3, -1): ("ZENOLOGY", params)})
    name, names = param_names(3, -1, exec_fn=sim)
    assert name == "ZENOLOGY" and len(names) == 1000 and names[999].endswith("0999")
    first = sim.execs
    assert first > 3  # info + several chunks
    param_names(3, -1, exec_fn=sim)
    assert sim.execs == first + 1  # only the cheap info call


def test_resolve_param():
    names = ["Cutoff", "Reso", "cutoff", "Porta Time"]
    assert resolve_param(names, "porta time") == 3
    assert resolve_param(names, 1) == 1
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_param(names, "CUTOFF")
    with pytest.raises(ValueError, match="similar"):
        resolve_param(names, "porta")


def test_scan_refused_while_recording():
    sim = eq_sim()
    sim.transport.recording = True
    with pytest.raises(RuntimeError, match="recording"):
        param_options(8, 0, 2, exec_fn=sim)


def test_missing_plugin():
    with pytest.raises(ValueError, match="no plugin at insert 5 slot 1"):
        param_names(5, 1, exec_fn=FLSim())


def test_value_string_with_documented_signature():
    """Older/documented API: (param, index, slot, pickupMode, useGlobalIndex), no defaults."""
    sim = eq_sim()

    class DocumentedPlugins(type(sim.plugins)):
        def getParamValueString(self, p, i, s, pickup, g):  # noqa: N802
            return self._p(i, s)[1][p].text()

    sim.plugins.__class__ = DocumentedPlugins
    res = set_params(8, 0, [{"param": 2, "text": "Low pass"}], exec_fn=sim)
    assert res["params"]["2"]["text"] == "Low pass"


def test_lib_compiles():
    compile(FL_PARAM_LIB, "<fl>", "exec")
