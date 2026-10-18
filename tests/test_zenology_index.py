import json

from fl_studio_mcp.utils import zenology as Z


def test_parse_tone_label_reads_number_and_name():
    text = "Roland / ZENOLOGY Pro\nv 068: SA Vibe SRX\nMONO\n2.2 Organ\n007: 808 Low Bass"
    assert Z.parse_tone_label(text) == [(68, "SA Vibe SRX"), (7, "808 Low Bass")]


def test_crawl_pack_steps_until_wraparound():
    names = {1: "Alpha", 2: "Beta Pad", 3: "Gamma 808"}
    state = {"n": 2}

    def click(rect, direction):
        state["n"] = state["n"] - 1 if direction == "prev" else state["n"] % 3 + 1

    def observe(rect, title):
        n = state["n"]
        return [n], f"{n:03d}: {names[n]}\nMONO"

    closed = []
    r = Z.crawl_pack({"pack": "SDZ999_Test", "pack_id": 5999, "pack_title": "Test"},
                     lambda action, params=None: closed.append(params) or {},
                     settle=0, click=click, observe=observe,
                     find_window=lambda: (0, 0, 500, 400), open_channel=lambda: 7)
    assert [(t["no"], t["name"]) for t in r["tones"]] == [(1, "Alpha"), (2, "Beta Pad"),
                                                          (3, "Gamma 808")]
    assert closed == [{"index": 7}]


def test_screen_index_is_searchable(tmp_path, monkeypatch):
    monkeypatch.setattr(Z, "screen_index_file", lambda: tmp_path / "_screen_index.json")
    Z._tones.cache_clear()
    Z.save_screen_index([{"pack": "SDZ999_Test", "pack_id": 5999, "pack_title": "Test",
                          "no": 4, "name": "Zebra Wobble", "category": "", "source": "screen"}])
    try:
        hits = Z.find_tones("zebra wobble")
        assert hits and hits[0]["pack"] == "SDZ999_Test"
        assert json.loads((tmp_path / "_screen_index.json").read_text())[0]["no"] == 4
    finally:
        Z._tones.cache_clear()


def test_find_tones_ranks_exact_then_name_then_category():
    hits = Z.find_tones("marimba", 10)
    assert hits[0]["name"].lower() == "marimba" and len(hits) > 1
