import numpy as np
import pytest
import soundfile as sf

from fl_studio_mcp.tools import samples as sample_tools
from fl_studio_mcp.utils.flp_edit import (
    EV_SAMPLE_PATH,
    _blocks,
    add_sampler_channels,
    channel_names,
    set_channel_sample,
)
from fl_studio_mcp.utils.flp_events import read_events


@pytest.fixture
def wavs(tmp_path):
    out = []
    for name in ("Kick", "Clap"):
        path = tmp_path / f"{name}.wav"
        sf.write(path, np.zeros((441, 2)), 44100)
        out.append(path)
    return out


def sample_of(project, channel):
    _, events = read_events(project.read_bytes())
    start, end = _blocks(events)[channel]
    raw = next(v for e, v in events[start:end] if e == EV_SAMPLE_PATH)
    return raw.decode("utf-16-le").rstrip("\x00")


def test_add_sampler_channels(template_flp, wavs):
    res = add_sampler_channels(template_flp, [("My Kick", str(wavs[0])), ("My Clap", str(wavs[1]))])
    assert res["channels"][-2:] == ["My Kick", "My Clap"]
    assert sample_of(template_flp, 5) == str(wavs[0]) and sample_of(template_flp, 6) == str(wavs[1])
    assert "backups" in res["backup"]


def test_set_channel_sample(template_flp, wavs):
    res = set_channel_sample(template_flp, 0, str(wavs[1]), name="New Clap")
    assert res["channels"][0] == "New Clap" and sample_of(template_flp, 0) == str(wavs[1])
    assert res["previous"].endswith(".wav") or res["previous"]
    names = channel_names(read_events(template_flp.read_bytes())[1])
    assert len(names) == 5


def test_set_channel_sample_refuses(template_flp, wavs, tmp_path):
    before = template_flp.read_bytes()
    with pytest.raises(ValueError, match="not a Sampler"):
        set_channel_sample(template_flp, 4, str(wavs[0]))  # a plugin channel
    with pytest.raises(ValueError, match="not found"):
        set_channel_sample(template_flp, 0, str(tmp_path / "missing.wav"))
    assert template_flp.read_bytes() == before


def test_find_samples(tmp_path, monkeypatch, wavs):
    (tmp_path / "Pack" / "Hyper").mkdir(parents=True)
    (tmp_path / "Pack" / "Hyper" / "Hyper Clap.wv").write_bytes(b"x")
    (tmp_path / "Pack" / "notes.txt").write_bytes(b"x")
    monkeypatch.setattr(sample_tools, "sample_roots", lambda: [tmp_path / "Pack"])
    assert [p.split("/")[-1] for p in sample_tools.find_samples("hyper clap")] == ["Hyper Clap.wv"]
    assert sample_tools.find_samples("snare") == []
