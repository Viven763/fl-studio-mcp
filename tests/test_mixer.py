import pytest
from fl_sim import FLSim

from fl_studio_mcp.utils.fl_mixer import build_routing_view, mixer_setup, read_routing


def song_sim():
    sim = FLSim(channels=["Kick", "Snare", "Violin", "Pad", "Snare"], tracks=12)
    sim.channels.targets = [4, 5, 8, 7, 5]
    sim.mixer.names[8] = "Violin"
    sim.mixer.names[10] = "Reverb"
    sim.mixer.routes[(8, 10)] = 0.35
    return sim


def test_build_routing_view_filters_defaults():
    view = build_routing_view(read_routing(exec_fn=song_sim()))
    tracks = {i["track"] for i in view["inserts"]}
    assert tracks == {0, 4, 5, 7, 8, 10}
    violin = next(i for i in view["inserts"] if i["track"] == 8)
    assert {"to": 10, "to_name": "Reverb", "level": 0.35} in violin["routes"]
    assert violin["channels"] == ["Violin"]
    reverb = next(i for i in view["inserts"] if i["track"] == 10)
    assert reverb["inputs"] == [{"from": 8, "from_name": "Violin", "level": 0.35}]
    assert view["channels"][2] == {"channel": 2, "name": "Violin", "insert": 8}


def test_build_routing_view_single_track():
    view = build_routing_view(read_routing(exec_fn=song_sim()), track=10)
    assert [i["track"] for i in view["inserts"]] == [10] and "channels" not in view


def test_mixer_setup_routes_sends_volumes():
    sim = song_sim()
    res = mixer_setup(
        routes=[{"channel": "Pad", "insert": 9}, {"channel": 4, "insert": 6}],
        names={"9": "Pad bus"},
        sends=[{"from": 7, "to": 10, "level": 0.3}, {"from": 8, "to": 10, "remove": True},
               {"from": 9, "to": 11, "exclusive": True}],
        volumes_db={"8": -3.0, "9": 30.0},
        exec_fn=sim,
    )
    assert sim.channels.targets[3] == 9 and sim.channels.targets[4] == 6
    assert sim.mixer.names[9] == "Pad bus"
    assert sim.mixer.routes[(7, 10)] == 0.3 and (8, 10) not in sim.mixer.routes
    assert (9, 0) not in sim.mixer.routes and (9, 11) in sim.mixer.routes
    assert sim.mixer.after_routing == 1
    assert res["volumes_db"]["8"] == pytest.approx(-3.0, abs=0.05)
    assert any("insert 9: asked 30.0 dB" in w for w in res["warnings"])
    assert {"from": 8, "to": 10, "active": False, "level": None} in res["sends"]


def test_mixer_setup_warns_on_silent_insert():
    sim = song_sim()
    res = mixer_setup(None, None, [{"from": 8, "to": 0, "remove": True},
                                   {"from": 8, "to": 10, "remove": True}], None, exec_fn=sim)
    assert any("insert 8 now routes nowhere" in w for w in res["warnings"])


def test_duplicate_channel_name_errors():
    with pytest.raises(ValueError, match="2 channels"):
        mixer_setup([{"channel": "Snare", "insert": 3}], None, None, None, exec_fn=song_sim())


def test_bad_send_rejected():
    with pytest.raises(ValueError, match="needs from/to"):
        mixer_setup(None, None, [{"from": 1, "level": 0.5}], None, exec_fn=song_sim())
