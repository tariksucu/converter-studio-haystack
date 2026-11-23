from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from converter_studio import AuthenticationError, UnprocessableError
from haystack.dataclasses import ByteStream
from haystack.utils import Secret

from haystack_integrations.components.converters.converter_studio import (
    ConverterStudioFileConverter,
    __version__,
)

from .fake_api import SCAN, FakeAPI


def converter(api: FakeAPI, **kwargs: Any) -> ConverterStudioFileConverter:
    component = ConverterStudioFileConverter(**kwargs)
    component._transport = api.transport()
    component._async_transport = api.async_transport()
    return component


def test_files_to_pdf(api: FakeAPI, files: Path) -> None:
    output = converter(api, output_file_type="pdf").run(
        sources=[files / "deck.pptx", str(files / "photo.heic")]
    )["output"]
    assert [stream.data for stream in output] == [b"%PDF-1.7 slides", SCAN]
    assert all(stream.mime_type == "application/pdf" for stream in output)
    assert output[0].meta == {
        "file_path": str(files / "deck.pptx"),
        "converter_studio": {
            "source_format": "pptx",
            "output_format": "pdf",
            "file_name": "deck.pdf",
        },
    }
    assert [body["to"] for body in api.conversions] == ["pdf", "pdf"]


def test_the_target_given_to_run_wins(api: FakeAPI, files: Path) -> None:
    component = converter(api, output_file_type="pdf")
    output = component.run(sources=[files / "photo.heic"], output_file_type=".JPG")["output"]
    assert output[0].meta["converter_studio"]["file_name"] == "photo.jpg"
    assert output[0].mime_type == "image/jpeg"
    assert api.conversions[0]["to"] == "jpg"


def test_a_target_is_needed(api: FakeAPI, files: Path) -> None:
    with pytest.raises(ValueError, match="output_file_type"):
        converter(api).run(sources=[files / "deck.pptx"])
    assert api.requests == []


def test_options_and_max_credits_are_sent(api: FakeAPI, files: Path) -> None:
    component = converter(api, output_file_type="mp3", options={"bitrate": "128"}, max_credits=4)
    component.run(sources=[files / "clip.mp4"])
    assert api.conversions[0]["options"] == {"bitrate": "128"}
    assert api.conversions[0]["maxCredits"] == 4


def test_bytestreams_and_meta(api: FakeAPI) -> None:
    stream = ByteStream(data=b"heic bytes", meta={"file_name": "IMG_1.HEIC", "album": "trip"})
    output = converter(api, output_file_type="jpg").run(
        sources=[stream], meta=[{"album": "holiday", "n": 1}]
    )["output"]
    assert output[0].meta == {
        "file_name": "IMG_1.HEIC",
        "album": "holiday",
        "n": 1,
        "converter_studio": {
            "source_format": "heic",
            "output_format": "jpg",
            "file_name": "IMG_1.jpg",
        },
    }
    assert api.upload_names() == ["IMG_1.HEIC"]


def test_text_results_say_their_encoding(api: FakeAPI, files: Path) -> None:
    source = ByteStream(data=b"PK word", meta={"file_name": "a.docx", "encoding": "latin-1"})
    output = converter(api, output_file_type="txt").run(sources=[source])["output"]
    assert output[0].meta["encoding"] == "utf-8-sig"  # the result's, not the source's
    assert output[0].mime_type == "text/plain"
    windows = converter(api, output_file_type="csv", options={"csvEncoding": "windows1254"})
    assert windows.run(sources=[files / "sheet.xlsx"])["output"][0].meta["encoding"] == "cp1254"
    pdf = converter(api, output_file_type="pdf").run(sources=[source])["output"][0]
    assert "encoding" not in pdf.meta


def test_a_link(api: FakeAPI) -> None:
    url = "https://example.com/talk.mov"
    output = converter(api, output_file_type="mp4").run(sources=[url])["output"]
    assert output[0].meta["url"] == url
    assert output[0].mime_type == "video/mp4"
    assert api.conversions[0] == {"to": "mp4", "url": url}


def test_failures_are_skipped(api: FakeAPI, files: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        output = converter(api, output_file_type="pdf").run(
            sources=[
                files / "clip.mp4",
                files / "broken.docx",
                files / "nope",
                files / "letter.docx",
            ]
        )["output"]
    assert [s.meta["converter_studio"]["file_name"] for s in output] == ["letter.pdf"]
    assert "We can't convert mp4 to pdf yet." in caplog.text
    assert "Your credits were given back" in caplog.text
    assert "Could not read" in caplog.text


def test_raise_on_failure(api: FakeAPI, files: Path) -> None:
    with pytest.raises(UnprocessableError) as caught:
        converter(api, output_file_type="pdf", raise_on_failure=True).run(
            sources=[files / "clip.mp4"]
        )
    assert caught.value.code == "unsupportedConversion"


def test_a_wrong_key_stops_the_run(api: FakeAPI, files: Path) -> None:
    component = converter(api, output_file_type="pdf", api_key=Secret.from_token("cvs_live_x"))
    with pytest.raises(AuthenticationError):
        component.run(sources=[files / "deck.pptx"])


def test_every_request_names_the_integration(api: FakeAPI, files: Path) -> None:
    converter(api, output_file_type="pdf").run(sources=[files / "deck.pptx"])
    assert {r.headers["x-converter-studio-client"] for r in api.requests} == {
        f"haystack/{__version__}"
    }


@pytest.mark.anyio
async def test_run_async(api: FakeAPI, files: Path) -> None:
    component = ConverterStudioFileConverter(output_file_type="pdf", concurrency_limit=3)
    component._async_transport = api.async_transport(delay=0.02)
    sources: list[str | Path | ByteStream] = [files / "deck.pptx", files / "clip.mp4"]
    sources += [files / "letter.docx", files / "photo.heic", files / "sheet.xlsx"]
    output = (await component.run_async(sources=sources))["output"]
    names = [s.meta["converter_studio"]["file_name"] for s in output]
    assert names == ["deck.pdf", "letter.pdf", "photo.pdf", "sheet.pdf"]
    assert api.most_in_flight == 3
    await component.close_async()


def test_settings_are_checked() -> None:
    with pytest.raises(ValueError, match="options"):
        ConverterStudioFileConverter(options=["quality"])  # type: ignore[arg-type]
    assert ConverterStudioFileConverter(output_file_type=" WebP ").output_file_type == "webp"
    assert ConverterStudioFileConverter(output_file_type="").output_file_type is None
