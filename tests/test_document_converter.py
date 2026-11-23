from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from converter_studio import (
    APIConnectionError,
    AuthenticationError,
    ConversionFailedError,
    InsufficientCreditsError,
)
from converter_studio import __version__ as python_client_version
from haystack.dataclasses import ByteStream
from haystack.utils import Secret

from haystack_integrations.components.converters.converter_studio import (
    ConverterStudioDocumentConverter,
    __version__,
)

from .fake_api import BROKEN, KEY, SCAN, TEXT_PDF, FakeAPI, error_response


def converter(api: FakeAPI, **kwargs: Any) -> ConverterStudioDocumentConverter:
    component = ConverterStudioDocumentConverter(**kwargs)
    component._transport = api.transport()
    component._async_transport = api.async_transport()
    return component


def options_sent(api: FakeAPI) -> list[dict[str, Any] | None]:
    return [body.get("options") for body in api.conversions]


# Text from each kind of file


def test_a_pdf_with_text(api: FakeAPI, files: Path) -> None:
    documents = converter(api).run(sources=[files / "report.pdf"])["documents"]
    assert len(documents) == 1
    assert documents[0].content == "Page one\n\fPage two\n\f"
    assert documents[0].meta == {
        "file_path": "report.pdf",
        "converter_studio": {"source_format": "pdf", "ocr": False},
    }
    assert [body["to"] for body in api.conversions] == ["txt"]
    assert options_sent(api) == [None]


def test_store_full_path(api: FakeAPI, files: Path) -> None:
    documents = converter(api, store_full_path=True).run(sources=[str(files / "report.pdf")])
    assert documents["documents"][0].meta["file_path"] == str(files / "report.pdf")


def test_a_scan_is_read_with_ocr_when_it_has_no_text(api: FakeAPI, files: Path) -> None:
    documents = converter(api).run(sources=[files / "scan.pdf"])["documents"]
    assert documents[0].content == "Text read by OCR\n"
    assert documents[0].meta["converter_studio"] == {"source_format": "pdf", "ocr": True}
    # The first try finds no text (and costs nothing); the file is sent again for OCR.
    assert options_sent(api) == [None, {"ocr": True}]
    assert len(api.paths("POST", "/v1/uploads")) == 2


def test_ocr_always_goes_straight_to_ocr(api: FakeAPI, files: Path) -> None:
    documents = converter(api, ocr="always").run(sources=[files / "scan.pdf"])["documents"]
    assert documents[0].meta["converter_studio"]["ocr"] is True
    assert options_sent(api) == [{"ocr": True}]


def test_ocr_always_does_not_touch_other_documents(api: FakeAPI, files: Path) -> None:
    documents = converter(api, ocr="always").run(sources=[files / "letter.docx"])["documents"]
    assert documents[0].meta["converter_studio"] == {"source_format": "docx", "ocr": False}
    assert options_sent(api) == [None]


def test_ocr_never_skips_a_scan(
    api: FakeAPI, files: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result = converter(api, ocr="never").run(sources=[files / "scan.pdf"])
    assert result["documents"] == []
    assert options_sent(api) == [None]
    assert "couldn't find any text" in caplog.text
    assert "scan.pdf" in caplog.text


def test_ocr_never_skips_pictures_without_a_request(api: FakeAPI, files: Path) -> None:
    result = converter(api, ocr="never").run(sources=[files / "photo.heic"])
    assert result["documents"] == []
    assert api.conversions == []


def test_a_picture_is_read_with_ocr(api: FakeAPI, files: Path) -> None:
    documents = converter(api).run(sources=[files / "photo.heic"])["documents"]
    assert documents[0].content == "Text in a picture\n"
    assert documents[0].meta["converter_studio"] == {"source_format": "heic", "ocr": True}


def test_word_text_loses_its_byte_order_mark(api: FakeAPI, files: Path) -> None:
    documents = converter(api).run(sources=[files / "letter.docx"])["documents"]
    assert documents[0].content == "Word text: café\n"


def test_a_presentation_becomes_a_pdf_then_text(api: FakeAPI, files: Path) -> None:
    documents = converter(api).run(sources=[files / "deck.pptx"])["documents"]
    assert documents[0].content == "Slide one\n\fSlide two\n\f"
    assert documents[0].meta["converter_studio"] == {"source_format": "pptx", "ocr": False}
    assert [body["to"] for body in api.conversions] == ["pdf", "txt"]
    assert api.upload_names() == ["deck.pptx", "deck.pdf"]


def test_a_camera_raw_photo_goes_through_a_pdf_and_ocr(api: FakeAPI, files: Path) -> None:
    documents = converter(api).run(sources=[files / "raw.cr2"])["documents"]
    assert documents[0].content == "Text read by OCR\n"
    assert documents[0].meta["converter_studio"] == {"source_format": "cr2", "ocr": True}
    assert [body["to"] for body in api.conversions] == ["pdf", "txt", "txt"]


def test_a_spreadsheet_becomes_csv(api: FakeAPI, files: Path) -> None:
    documents = converter(api, options={"sheet": 2}).run(sources=[files / "sheet.xlsx"])
    assert documents["documents"][0].content == "name,age\nAda,36\n"
    assert api.conversions[0]["to"] == "csv"
    assert api.conversions[0]["options"] == {"sheet": 2}


def test_text_formats_are_read_here(api: FakeAPI, files: Path) -> None:
    documents = converter(api).run(sources=[files / "notes.txt", files / "data.json"])["documents"]
    assert [d.content for d in documents] == ["Plain notes, déjà vu\n", '{"a": 1}']
    assert documents[0].meta["converter_studio"] == {"source_format": "txt", "ocr": False}
    assert api.conversions == []


def test_a_text_bytestream_uses_its_encoding(api: FakeAPI) -> None:
    stream = ByteStream(
        data="Grüße".encode("latin-1"), meta={"file_name": "a.txt", "encoding": "latin-1"}
    )
    documents = converter(api).run(sources=[stream])["documents"]
    assert documents[0].content == "Grüße"


def test_what_has_no_text_is_skipped_without_a_request(
    api: FakeAPI, files: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result = converter(api).run(sources=[files / "clip.mp4", files / "report.pdf"])
    assert len(result["documents"]) == 1
    assert [body["to"] for body in api.conversions] == ["txt"]
    assert "cannot make text of MP4 files" in caplog.text


# Sources and metadata


def test_bytestreams_keep_their_meta(api: FakeAPI) -> None:
    stream = ByteStream(data=TEXT_PDF, meta={"file_path": "/in/box/report.pdf", "lang": "en"})
    documents = converter(api).run(sources=[stream], meta={"project": "x"})["documents"]
    assert documents[0].meta == {
        "file_path": "report.pdf",
        "lang": "en",
        "project": "x",
        "converter_studio": {"source_format": "pdf", "ocr": False},
    }
    assert api.upload_names() == ["report.pdf"]


def test_a_mime_type_wins_over_a_misleading_name(api: FakeAPI) -> None:
    # What ConverterStudioFileConverter hands on: the PDF made from slides.pptx.
    stream = ByteStream(data=SCAN, meta={"file_path": "slides.pptx"}, mime_type="application/pdf")
    documents = converter(api).run(sources=[stream])["documents"]
    assert documents[0].meta["converter_studio"] == {"source_format": "pdf", "ocr": True}
    assert api.upload_names() == ["slides.pdf", "slides.pdf"]


def test_a_bytestream_without_a_name_or_type(api: FakeAPI) -> None:
    documents = converter(api).run(sources=[ByteStream(data=SCAN)])["documents"]
    assert documents[0].content == "Text read by OCR\n"
    assert documents[0].meta["converter_studio"] == {"source_format": "pdf", "ocr": True}
    assert api.upload_names() == ["document", "document"]


def test_an_unknown_file_is_routed_by_what_the_api_finds(api: FakeAPI) -> None:
    # A name that says nothing: the first try (text) is refused, and the API says it is a pptx.
    api.push(
        "/api/v1/conversions",
        error_response(
            422, "unsupportedConversion", "We can't convert pptx to txt yet.", **{"from": "pptx"}
        ),
    )
    stream = ByteStream(data=b"PK slides", meta={"file_name": "deck.bin"})
    documents = converter(api).run(sources=[stream])["documents"]
    assert documents[0].content == "Slide one\n\fSlide two\n\f"
    assert documents[0].meta["converter_studio"] == {"source_format": "pptx", "ocr": False}
    assert api.upload_names() == ["deck.bin", "deck.bin.pptx", "deck.bin.pdf"]


def test_an_unknown_file_the_api_cannot_read_is_skipped(
    api: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result = converter(api).run(sources=[ByteStream(data=b"??", meta={"name": "x.bin"})])
    assert result["documents"] == []
    assert "We can't convert this file to txt yet." in caplog.text
    assert "ByteStream x.bin" in caplog.text


def test_a_link_is_fetched_by_the_api(api: FakeAPI) -> None:
    url = "https://files.example.com/share/report.pdf"
    api.links[url] = TEXT_PDF
    documents = converter(api).run(sources=[url])["documents"]
    assert documents[0].meta == {
        "url": url,
        "converter_studio": {"source_format": "pdf", "ocr": False},
    }
    assert api.conversions[0]["url"] == url
    assert api.paths("POST", "/v1/uploads") == []


def test_a_text_link_is_not_downloaded_here(api: FakeAPI, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        result = converter(api).run(sources=["https://example.com/notes.txt"])
    assert result["documents"] == []
    assert "LinkContentFetcher" in caplog.text


def test_meta_per_source(api: FakeAPI, files: Path) -> None:
    documents = converter(api).run(
        sources=[files / "report.pdf", files / "notes.txt"], meta=[{"n": 1}, {"n": 2}]
    )["documents"]
    assert [d.meta["n"] for d in documents] == [1, 2]
    with pytest.raises(ValueError, match="length of the metadata list"):
        converter(api).run(sources=[files / "report.pdf"], meta=[{}, {}])


def test_a_missing_file_is_skipped(
    api: FakeAPI, files: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result = converter(api).run(sources=[files / "nope.pdf", files / "report.pdf"])
    assert len(result["documents"]) == 1
    assert "Could not read" in caplog.text


def test_no_sources_no_requests(api: FakeAPI) -> None:
    assert converter(api).run(sources=[]) == {"documents": []}
    assert api.requests == []


# Failures


def test_a_failed_conversion_is_skipped_with_the_api_message(
    api: FakeAPI, files: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result = converter(api).run(sources=[files / "broken.docx", files / "letter.docx"])
    assert [d.meta["file_path"] for d in result["documents"]] == ["letter.docx"]
    assert "Could not convert" in caplog.text
    assert "Your credits were given back" in caplog.text


def test_raise_on_failure(api: FakeAPI, files: Path) -> None:
    with pytest.raises(ConversionFailedError) as caught:
        converter(api, raise_on_failure=True).run(sources=[files / "broken.docx"])
    assert caught.value.code == "conversionFailed"
    with pytest.raises(FileNotFoundError):
        converter(api, raise_on_failure=True).run(sources=[files / "nope.pdf"])


def test_a_price_over_max_credits_is_skipped(
    api: FakeAPI, files: Path, caplog: pytest.LogCaptureFixture
) -> None:
    api.credits = 5
    with caplog.at_level(logging.WARNING):
        result = converter(api, max_credits=2).run(sources=[files / "letter.docx"])
    assert result["documents"] == []
    assert api.conversions[0]["maxCredits"] == 2
    assert "costs 5 credits" in caplog.text


def test_a_wrong_key_stops_the_run(api: FakeAPI, files: Path) -> None:
    component = converter(api, api_key=Secret.from_token("cvs_live_wrong"))
    with pytest.raises(AuthenticationError) as caught:
        component.run(sources=[files / "report.pdf", files / "letter.docx"])
    assert caught.value.code == "invalidApiKey"
    assert len(api.paths("POST", "/v1/uploads")) == 1


def test_no_credits_stops_the_run(api: FakeAPI, files: Path) -> None:
    api.balance = 0
    with pytest.raises(InsufficientCreditsError):
        converter(api).run(sources=[files / "report.pdf", files / "letter.docx"])
    assert len(api.conversions) == 1


def test_an_api_out_of_reach_stops_the_run(files: Path) -> None:
    import httpx

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    component = ConverterStudioDocumentConverter()
    component._transport = httpx.MockTransport(down)
    component.warm_up()
    assert component._client is not None
    component._client._sleep = lambda _: None
    with pytest.raises(APIConnectionError, match=r"api\.test"):
        component.run(sources=[files / "report.pdf"])


def test_no_key_raises_at_warm_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONVERTER_STUDIO_API_KEY")
    component = ConverterStudioDocumentConverter()  # creating it needs no key
    with pytest.raises(ValueError, match="CONVERTER_STUDIO_API_KEY"):
        component.warm_up()


# Requests


def test_every_request_names_the_integration(api: FakeAPI, files: Path) -> None:
    converter(api).run(sources=[files / "scan.pdf", files / "deck.pptx"])
    assert len(api.requests) > 8
    for request in api.requests:
        assert request.headers["x-converter-studio-client"] == f"haystack/{__version__}"
        agent = request.headers["user-agent"]
        assert agent.startswith(
            f"haystack/{__version__} converter-studio-python/{python_client_version} Python/"
        )
        assert agent.count("haystack") == 1
    with_key = [r for r in api.requests if "authorization" in r.headers]
    assert all(str(r.url).startswith("https://api.test/api/v1/") for r in with_key)
    assert all(r.headers["authorization"] == f"Bearer {KEY}" for r in with_key)
    creations = api.paths("POST", "/v1/conversions")
    keys = [r.headers["idempotency-key"] for r in creations]
    assert len(set(keys)) == len(keys)


def test_the_format_list_is_read_once(api: FakeAPI, files: Path) -> None:
    component = converter(api)
    component.run(sources=[files / "report.pdf"])
    component.run(sources=[files / "letter.docx"])
    assert len(api.paths("GET", "/v1/formats")) == 1


def test_warm_up_close_and_again(api: FakeAPI, files: Path) -> None:
    component = converter(api)
    component.warm_up()
    client = component._client
    component.warm_up()
    assert component._client is client
    component.close()
    assert component._client is None
    component.close()
    assert len(component.run(sources=[files / "report.pdf"])["documents"]) == 1


def test_settings_are_checked() -> None:
    with pytest.raises(ValueError, match="ocr must be one of"):
        ConverterStudioDocumentConverter(ocr="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r'ocr="always"'):
        ConverterStudioDocumentConverter(options={"ocr": True})
    with pytest.raises(ValueError, match="max_credits"):
        ConverterStudioDocumentConverter(max_credits=-1)
    with pytest.raises(ValueError, match="timeout"):
        ConverterStudioDocumentConverter(timeout=0)
    with pytest.raises(ValueError, match="concurrency_limit"):
        ConverterStudioDocumentConverter(concurrency_limit=0)


# Async


@pytest.mark.anyio
async def test_run_async_keeps_the_order_and_the_limit(api: FakeAPI, files: Path) -> None:
    component = ConverterStudioDocumentConverter(concurrency_limit=2)
    component._async_transport = api.async_transport(delay=0.02)
    names_in = ["letter.docx", "report.pdf", "photo.heic", "notes.txt", "deck.pptx", "clip.mp4"]
    sources: list[str | Path | ByteStream] = [files / name for name in [*names_in, "scan.pdf"]]
    result = await component.run_async(sources=sources)
    names = [d.meta["file_path"] for d in result["documents"]]
    assert names == [
        "letter.docx",
        "report.pdf",
        "photo.heic",
        "notes.txt",
        "deck.pptx",
        "scan.pdf",
    ]
    assert api.most_in_flight == 2
    await component.close_async()
    assert component._async_client is None


@pytest.mark.anyio
async def test_run_async_stops_on_a_wrong_key(api: FakeAPI, files: Path) -> None:
    component = ConverterStudioDocumentConverter(api_key=Secret.from_token("cvs_live_wrong"))
    component._async_transport = api.async_transport()
    with pytest.raises(AuthenticationError):
        await component.run_async(sources=[files / "report.pdf", files / "letter.docx"])
    await component.close_async()


@pytest.mark.anyio
async def test_run_async_skips_failures(api: FakeAPI, files: Path) -> None:
    component = ConverterStudioDocumentConverter()
    component._async_transport = api.async_transport()
    result = await component.run_async(
        sources=[files / "broken.docx", ByteStream(data=SCAN)], meta={"batch": 7}
    )
    assert len(result["documents"]) == 1
    assert result["documents"][0].meta == {
        "batch": 7,
        "converter_studio": {"source_format": "pdf", "ocr": True},
    }
    assert result["documents"][0].content == "Text read by OCR\n"
    await component.close_async()


def test_the_request_bodies(api: FakeAPI, files: Path) -> None:
    converter(api, options={"ocrLanguage": "de"}, max_credits=3).run(sources=[files / "scan.pdf"])
    bodies = [json.loads(r.content) for r in api.paths("POST", "/v1/conversions")]
    assert [b["options"] for b in bodies] == [
        {"ocrLanguage": "de"},
        {"ocrLanguage": "de", "ocr": True},
    ]
    assert all(b["maxCredits"] == 3 for b in bodies)
    assert BROKEN not in [r.content for r in api.requests]
