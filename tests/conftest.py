from __future__ import annotations

from pathlib import Path

import pytest

from .fake_api import BASE, KEY, FakeAPI


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVERTER_STUDIO_API_KEY", KEY)
    monkeypatch.setenv("CONVERTER_STUDIO_BASE_URL", BASE)


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def files(tmp_path: Path) -> Path:
    """A folder of small files the fake API knows what to do with."""
    from .fake_api import BROKEN, SCAN, TEXT_PDF

    (tmp_path / "report.pdf").write_bytes(TEXT_PDF)
    (tmp_path / "scan.pdf").write_bytes(SCAN)
    (tmp_path / "letter.docx").write_bytes(b"PK word")
    (tmp_path / "deck.pptx").write_bytes(b"PK slides")
    (tmp_path / "sheet.xlsx").write_bytes(b"PK sheet")
    (tmp_path / "photo.heic").write_bytes(b"heic bytes")
    (tmp_path / "raw.cr2").write_bytes(b"raw bytes")
    (tmp_path / "clip.mp4").write_bytes(b"video bytes")
    (tmp_path / "notes.txt").write_text("Plain notes, déjà vu\n", encoding="utf-8")
    (tmp_path / "data.json").write_text('{"a": 1}', encoding="utf-8")
    (tmp_path / "broken.docx").write_bytes(BROKEN)
    return tmp_path


@pytest.fixture
def anyio_backend() -> str:
    # Haystack runs its async pipelines on asyncio.
    return "asyncio"
