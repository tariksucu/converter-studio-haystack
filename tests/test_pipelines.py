"""The components inside real Haystack pipelines, and their serialization."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from haystack import Pipeline
from haystack.components.converters import TextFileToDocument
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.writers import DocumentWriter
from haystack.core.serialization import component_from_dict, component_to_dict
from haystack.document_stores.in_memory import InMemoryDocumentStore
from haystack.utils import Secret

from haystack_integrations.components.converters.converter_studio import (
    ConverterStudioDocumentConverter,
    ConverterStudioFileConverter,
)

from .fake_api import FakeAPI

DOCUMENT_TYPE = (
    "haystack_integrations.components.converters.converter_studio.document_converter."
    "ConverterStudioDocumentConverter"
)
FILE_TYPE = (
    "haystack_integrations.components.converters.converter_studio.file_converter."
    "ConverterStudioFileConverter"
)
ENV_KEY = {"type": "env_var", "env_vars": ["CONVERTER_STUDIO_API_KEY"], "strict": True}


def test_document_converter_to_dict() -> None:
    component = ConverterStudioDocumentConverter()
    assert component_to_dict(component, "converter") == {
        "type": DOCUMENT_TYPE,
        "init_parameters": {
            "api_key": ENV_KEY,
            "base_url": None,
            "ocr": "auto",
            "output_format": "text",
            "options": None,
            "max_credits": None,
            "timeout": 600.0,
            "store_full_path": False,
            "raise_on_failure": False,
            "concurrency_limit": 4,
        },
    }


def test_document_converter_round_trip() -> None:
    component = ConverterStudioDocumentConverter(
        api_key=Secret.from_env_var(["MY_KEY", "CONVERTER_STUDIO_API_KEY"], strict=False),
        base_url="http://localhost:8080/api",
        ocr="always",
        output_format="markdown",
        options={"ocrLanguage": "de"},
        max_credits=5,
        timeout=120.0,
        store_full_path=True,
        raise_on_failure=True,
        concurrency_limit=2,
    )
    data = component_to_dict(component, "converter")
    kept = copy.deepcopy(data)
    again = component_from_dict(ConverterStudioDocumentConverter, data, "converter")
    assert data == kept  # from_dict leaves its input alone
    assert isinstance(again, ConverterStudioDocumentConverter)
    assert again.api_key == component.api_key
    assert component_to_dict(again, "converter") == kept


def test_a_pipeline_saved_by_1_0_loads_with_plain_text() -> None:
    data = {
        "type": DOCUMENT_TYPE,
        "init_parameters": {
            "api_key": ENV_KEY,
            "base_url": None,
            "ocr": "auto",
            "options": None,
            "max_credits": None,
            "timeout": 600.0,
            "store_full_path": False,
            "raise_on_failure": False,
            "concurrency_limit": 4,
        },
    }
    component = component_from_dict(ConverterStudioDocumentConverter, data, "converter")
    assert isinstance(component, ConverterStudioDocumentConverter)
    assert component.output_format == "text"


def test_file_converter_round_trip() -> None:
    component = ConverterStudioFileConverter(output_file_type="pdf", options={"pdfA": True})
    data = component_to_dict(component, "to_pdf")
    assert data["type"] == FILE_TYPE
    assert data["init_parameters"]["output_file_type"] == "pdf"
    assert data["init_parameters"]["api_key"] == ENV_KEY
    again = component_from_dict(ConverterStudioFileConverter, data, "to_pdf")
    assert component_to_dict(again, "to_pdf") == data


def test_a_token_is_never_serialized() -> None:
    component = ConverterStudioDocumentConverter(api_key=Secret.from_token("cvs_live_secret"))
    with pytest.raises(ValueError, match="Cannot serialize token-based secret"):
        component.to_dict()


def test_an_indexing_pipeline(api: FakeAPI, files: Path) -> None:
    store = InMemoryDocumentStore()
    converter = ConverterStudioDocumentConverter()
    converter._transport = api.transport()
    pipeline = Pipeline()
    pipeline.add_component("converter", converter)
    pipeline.add_component("splitter", DocumentSplitter(split_by="page", split_length=1))
    pipeline.add_component("writer", DocumentWriter(document_store=store))
    pipeline.connect("converter", "splitter")
    pipeline.connect("splitter", "writer")
    result = pipeline.run(
        {"converter": {"sources": [files / "report.pdf", files / "scan.pdf", files / "deck.pptx"]}}
    )
    assert result["writer"]["documents_written"] == store.count_documents()
    contents = sorted(d.content or "" for d in store.filter_documents())
    assert "Page one\n\f" in contents
    assert "Text read by OCR\n" in contents
    assert "Slide two\n\f" in contents
    ocr = store.filter_documents(
        filters={"field": "meta.file_path", "operator": "==", "value": "scan.pdf"}
    )
    assert ocr[0].meta["converter_studio"]["ocr"] is True


def test_page_breaks_give_each_chunk_its_page_number(api: FakeAPI, files: Path) -> None:
    # The PDF's text keeps the API's page breaks (form feeds), in Markdown mode too: Haystack's
    # DocumentSplitter numbers the pages of each chunk from them. Split by page, the way that
    # numbers pages alike in every Haystack version (split by line counts them differently in
    # 2.20-2.29).
    converter = ConverterStudioDocumentConverter(output_format="markdown")
    converter._transport = api.transport()
    pipeline = Pipeline()
    pipeline.add_component("converter", converter)
    pipeline.add_component("splitter", DocumentSplitter(split_by="page", split_length=1))
    pipeline.connect("converter", "splitter")
    result = pipeline.run({"converter": {"sources": [files / "report.pdf"]}})
    chunks = result["splitter"]["documents"]
    assert [(c.content.strip(), c.meta["page_number"]) for c in chunks if c.content.strip()] == [
        ("Page one", 1),
        ("Page two", 2),
    ]
    assert chunks[0].meta["converter_studio"]["content_format"] == "txt"


def test_the_file_converter_feeds_haystack_converters(api: FakeAPI, files: Path) -> None:
    to_text = ConverterStudioFileConverter(output_file_type="txt")
    to_text._transport = api.transport()
    pipeline = Pipeline()
    pipeline.add_component("to_text", to_text)
    pipeline.add_component("text", TextFileToDocument())
    pipeline.connect("to_text.output", "text.sources")
    result = pipeline.run({"to_text": {"sources": [files / "letter.docx"]}})
    documents = result["text"]["documents"]
    assert documents[0].content == "Word text: café\n"
    assert documents[0].meta["file_path"] == "letter.docx"


def test_the_file_converter_feeds_the_document_converter(api: FakeAPI, files: Path) -> None:
    to_pdf = ConverterStudioFileConverter(output_file_type="pdf")
    to_pdf._transport = api.transport()
    text = ConverterStudioDocumentConverter()
    text._transport = api.transport()
    pipeline = Pipeline()
    pipeline.add_component("to_pdf", to_pdf)
    pipeline.add_component("text", text)
    pipeline.connect("to_pdf.output", "text.sources")
    result = pipeline.run({"to_pdf": {"sources": [files / "photo.heic"]}})
    document = result["text"]["documents"][0]
    assert document.content == "Text read by OCR\n"
    assert document.meta["file_path"] == "photo.heic"
    assert document.meta["converter_studio"] == {"source_format": "pdf", "ocr": True}
    assert api.upload_names() == ["photo.heic", "photo.pdf", "photo.pdf"]


def test_yaml_round_trip() -> None:
    pipeline = Pipeline()
    pipeline.add_component("to_pdf", ConverterStudioFileConverter(output_file_type="pdf"))
    pipeline.add_component("text", ConverterStudioDocumentConverter(ocr="always"))
    pipeline.connect("to_pdf.output", "text.sources")
    yaml = pipeline.dumps()
    assert "cvs_live" not in yaml
    loaded = Pipeline.loads(yaml)
    assert loaded.dumps() == yaml
    text = loaded.get_component("text")
    assert isinstance(text, ConverterStudioDocumentConverter)
    assert text.ocr == "always"


@pytest.mark.anyio
async def test_an_async_pipeline(api: FakeAPI, files: Path) -> None:
    from haystack.version import __version__ as haystack_version

    converter = ConverterStudioDocumentConverter()
    converter._async_transport = api.async_transport()
    if haystack_version.startswith("2."):
        from haystack import AsyncPipeline  # type: ignore[attr-defined]

        pipeline = AsyncPipeline()
    else:
        pipeline = Pipeline()
    pipeline.add_component("converter", converter)
    result = await pipeline.run_async({"converter": {"sources": [files / "scan.pdf"]}})
    assert result["converter"]["documents"][0].content == "Text read by OCR\n"
    await converter.close_async()
