"""Shared fixtures: a real FL project file to edit, and a scriptable fake FL connection."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

TEMPLATE = Path(
    "/Applications/FL Studio 2026.app/Contents/Resources/FL/Data/Templates/Minimal/"
    "Basic with limiter/Basic with limiter.flp"
)


@pytest.fixture
def template_flp(tmp_path: Path) -> Path:
    """A copy of FL's minimal template project (FL 2026 layout), skipped if FL isn't here."""
    if not TEMPLATE.exists():
        pytest.skip("FL Studio 2026 template not installed")
    dst = tmp_path / "Song" / "Song.flp"
    dst.parent.mkdir()
    shutil.copy2(TEMPLATE, dst)
    return dst


class FakeSend:
    """Stands in for FLConnection.send_command; `handler(action, params)` makes replies."""

    def __init__(self, handler=None):
        self.calls: list[tuple[str, dict]] = []
        self.handler = handler or (lambda action, params: {"success": True, "result": None})

    def __call__(self, action, params=None, timeout=2.0):
        self.calls.append((action, params or {}))
        return self.handler(action, params or {})

    def codes(self) -> list[str]:
        return [p.get("code", "") for a, p in self.calls if a == "debug.exec"]


@pytest.fixture
def fake_send():
    return FakeSend
