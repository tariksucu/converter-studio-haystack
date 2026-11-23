from __future__ import annotations

from pathlib import Path

import pytest
from converter_studio import ConverterStudio, Format
from haystack.dataclasses import ByteStream

from haystack_integrations.components.converters.converter_studio import __version__
from haystack_integrations.components.converters.converter_studio._shared import (
    CLIENT_NAME,
    Catalog,
    describe,
    extension_for_mime,
    extension_of,
    mime_type_for,
    read_source,
)
from haystack_integrations.components.converters.converter_studio.document_converter import (
    text_steps,
)

from .fake_api import FORMATS

CATALOG = Catalog([Format.from_api(item) for item in FORMATS])


@pytest.mark.parametrize(
    ("name", "extension"),
    [
        ("report.PDF", "pdf"),
        ("backup.tar.gz", "tar.gz"),
        ("archive.tgz", "tgz"),
        ("README", None),
        ("", None),
        (None, None),
        ("dir.v2/file", None),
    ],
)
def test_extension_of(name: str | None, extension: str | None) -> None:
    assert extension_of(name) == extension


@pytest.mark.parametrize(
    ("mime", "extension"),
    [
        ("application/pdf", "pdf"),
        ("image/heic", "heic"),
        ("image/jpeg", "jpg"),
        ("text/plain; charset=utf-8", "txt"),
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"),
        ("application/octet-stream", None),
        (None, None),
    ],
)
def test_extension_for_mime(mime: str | None, extension: str | None) -> None:
    assert extension_for_mime(mime) == extension


def test_mime_type_for() -> None:
    assert mime_type_for("a.pdf") == "application/pdf"
    assert mime_type_for("a.heic") == "image/heic"
    assert mime_type_for("a.webp") == "image/webp"
    assert mime_type_for("backup.tar.gz") == "application/gzip"
    assert mime_type_for("noextension") is None
    assert mime_type_for(None) is None


def test_reading_sources(tmp_path: Path) -> None:
    path = tmp_path / "a.docx"
    path.write_bytes(b"x")
    item = read_source(str(path))
    assert (item.file, item.name_extension, item.meta) == (path, "docx", {"file_path": str(path)})
    link = read_source("HTTPS://example.com/files/Scan.PDF?dl=1")
    assert (link.file, link.name_extension, link.meta, link.is_url) == (
        "HTTPS://example.com/files/Scan.PDF?dl=1",
        "pdf",
        {"url": "HTTPS://example.com/files/Scan.PDF?dl=1"},
        True,
    )
    stream = read_source(ByteStream(data=b"1", meta={"file_path": "C:\\in\\b.xlsx"}))
    assert (stream.name, stream.name_extension) == ("b.xlsx", "xlsx")
    with pytest.raises(FileNotFoundError):
        read_source(tmp_path / "missing.pdf")
    with pytest.raises(ValueError, match="Unsupported source type"):
        read_source(42)  # type: ignore[arg-type]


def test_upload_names() -> None:
    def name(stream: ByteStream) -> str | None:
        item = read_source(stream)
        return CATALOG.upload(item, CATALOG.identify(item))[1]

    pdf = "application/pdf"
    assert (
        name(ByteStream(data=b"1", meta={"file_path": "slides.pptx"}, mime_type=pdf))
        == "slides.pdf"
    )
    assert name(ByteStream(data=b"1", meta={"file_path": "a.PDF"}, mime_type=pdf)) == "a.PDF"
    assert name(ByteStream(data=b"1", meta={"name": "a.jpeg"}, mime_type="image/jpeg")) == "a.jpeg"
    assert name(ByteStream(data=b"1", meta={"name": "notes.v2"}, mime_type=pdf)) == "notes.v2.pdf"
    assert name(ByteStream(data=b"1", mime_type=pdf)) == "document.pdf"
    assert name(ByteStream(data=b"1")) == "document"
    assert CATALOG.upload(read_source("https://x.test/a.pdf"), None) == (
        "https://x.test/a.pdf",
        None,
    )


def test_describe_never_shows_bytes() -> None:
    assert (
        describe(ByteStream(data=b"secret bytes", meta={"file_name": "a.pdf"}))
        == "ByteStream a.pdf"
    )
    assert describe(ByteStream(data=b"secret bytes")) == "a ByteStream of 12 bytes"
    assert describe(Path("x/y.pdf")) == str(Path("x/y.pdf"))


def test_text_steps() -> None:
    def steps(format_id: str | None, ocr: str = "auto") -> tuple[str, ...]:
        return text_steps(CATALOG.get(format_id), ocr)  # type: ignore[arg-type]

    assert steps("pdf") == ("txt",)
    assert steps("png") == ("txt",)
    assert steps("xlsx") == ("csv",)
    assert steps("pptx") == ("pdf", "txt")
    assert steps("cr2") == ("pdf", "txt")
    assert steps(None) == ("txt",)
    with pytest.raises(ValueError, match="cannot make text of MP4"):
        steps("mp4")
    with pytest.raises(ValueError, match="only be read with OCR"):
        steps("png", "never")
    with pytest.raises(ValueError, match="only be read with OCR"):
        steps("cr2", "never")


def test_the_client_name() -> None:
    assert f"haystack/{__version__}" == CLIENT_NAME
    # The Python client takes it (it raises ValueError on a name the header cannot carry).
    ConverterStudio(api_key="cvs_test_x", client_name=CLIENT_NAME).close()
