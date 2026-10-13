"""Build the ZENOLOGY tone index (name -> pack id + tone number) from installed sound-list PDFs.

Usage: python scripts/build_zenology_index.py [out.json]
Requires `pdftotext` (poppler: `brew install poppler` / poppler for Windows).
"""
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

CANDIDATES = [
    os.environ.get("ZENOLOGY_DATA", ""),
    "/Library/Application Support/Roland Cloud/ZENOLOGY",  # macOS
    r"C:\ProgramData\Roland Cloud\ZENOLOGY",  # Windows
    r"C:\Program Files\Roland Cloud\ZENOLOGY",
]
ROOT = next((Path(c) for c in CANDIDATES if c and Path(c).is_dir()), None)
if ROOT is None:
    sys.exit("ZENOLOGY data folder not found; set ZENOLOGY_DATA to the folder with EXZ*/SDZ* packs")
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path(__file__).resolve().parent.parent / "src" / "fl_studio_mcp" / "data"
    / "zenology_tones.json"
)

def is_int(s):
    return s.isdigit()

tones = []
for pack in sorted(ROOT.iterdir()):
    files = [f for f in pack.iterdir() if f.suffix in ('.exz', '.sdz')]
    if not files:
        continue
    head = files[0].read_bytes()[:64]
    pack_id = struct.unpack('<H', head[16:18])[0]
    title = head[32:64].decode('ascii', 'replace').strip()
    seen = set()
    for pdf in sorted(pack.glob('*.pdf')):
        cmd = ['pdftotext', '-layout', str(pdf), '-']
        text = subprocess.run(cmd, capture_output=True, text=True).stdout
        # Only the tone list is selectable; later sections list drum-kit
        # instruments and waveforms, whose names/numbers would mislead.
        cut = re.search(r"Drum Kit (Tone )?List|Drum Kit Name|Wave(form)? List|Multisample", text)
        if cut:
            text = text[:cut.start()]
        for line in text.splitlines():
            tok = re.split(r'\s{2,}|\s*\|\s*', line.strip())
            i = 0
            while i + 2 < len(tok):
                no, name, cat = tok[i], tok[i + 1], tok[i + 2]
                # Orchestra/World PDFs also list single drum instruments in these
                # categories; they are not selectable tones (loading one by number
                # selects an unrelated tone).
                drum_part = cat in ('Kick', 'Snare', 'Hi-Hat', 'Cymbal', 'Ride', 'Tom')
                if (is_int(no) and not is_int(name) and not is_int(cat) and 0 < int(no) < 2000
                        and not drum_part):
                    key = (int(no), name)
                    if key not in seen:
                        seen.add(key)
                        tones.append({'pack': pack.name, 'pack_id': pack_id, 'pack_title': title,
                                      'no': int(no), 'name': name, 'category': cat})
                    i += 3
                    k = 0
                    while i + k < len(tok) and is_int(tok[i + k]):
                        k += 1
                    if k >= 3:  # MSB/LSB/PC columns (any extra int is the next tone number)
                        i += 3
                else:
                    i += 1
OUT.write_text(json.dumps(tones, ensure_ascii=False, indent=0))
print(len(tones), 'tones from', len({t["pack"] for t in tones}), 'packs')
