"""ConverterStudioDocumentConverter: files to Haystack Documents, through Converter Studio's text
and OCR conversions."""

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, get_args

from haystack import Document, component, default_to_dict, logging
from haystack.components.converters.utils import normalize_metadata
from haystack.dataclasses import ByteStream
from haystack.utils import Secret

from converter_studio import (
    AsyncConverterStudio,
    Conversion,
    ConversionFailedError,
    ConverterStudio,
    ConverterStudioError,
    Format,
    UnprocessableError,
)

from ._shared import (
    API_KEY_ENV,
    DEFAULT_CONCURRENCY,
    DEFAULT_TIMEOUT,
    STOPPING_ERRORS,
    Catalog,
    ConverterStudioComponent,
    Input,
    SourceType,
    read_source,
)

logger = logging.getLogger(__name__)

OCRMode = Literal["auto", "always", "never"]
"""When to read text with OCR (optical character recognition).

``auto``: a PDF's own text first, and OCR only when it has none (a scan); pictures are always
read with OCR. ``always``: PDFs straight to OCR, for scans (it skips the first try). ``never``:
no OCR; scanned PDFs and pictures are skipped."""

OutputFormat = Literal["text", "markdown"]
"""What the Documents' text is.

``text``: plain text. ``markdown``: Markdown (GitHub's flavour, made by Pandoc) for the formats the
API makes Markdown of (Word, OpenDocument, RTF and HTML documents), which keeps their headings,
lists and tables; every other format gives the same text as with ``text``."""

#: Formats that are text already: read here, without a request and without credits.
LOCAL_TEXT_FORMATS = frozenset({"txt", "md", "csv", "json", "xml", "yaml", "srt", "vtt", "ass"})

_File = str | Path | bytes


def text_steps(
    source: Format | None, ocr: OCRMode, output_format: OutputFormat = "text"
) -> tuple[str, ...]:
    """The conversions that make text of a format: ``("md",)`` with ``output_format="markdown"``
    where the API makes Markdown of it (Word, OpenDocument, RTF, HTML), ``("txt",)`` where the API
    makes text of it (documents, PDFs, e-books, pictures by OCR), ``("csv",)`` for spreadsheets
    (the first sheet), ``("pdf", "txt")`` for what only becomes a PDF (presentations, vector
    drawings, camera RAW photos). An unknown format is tried as text."""
    if source is None:
        return ("txt",)
    picture = source.category == "image"
    if output_format == "markdown" and "md" in source.targets:
        steps: tuple[str, ...] = ("md",)
    elif "txt" in source.targets:
        steps = ("txt",)
    elif "csv" in source.targets:
        steps = ("csv",)
    elif "pdf" in source.targets:
        steps = ("pdf", "txt")
    else:
        raise ValueError(f"Converter Studio cannot make text of {source.name} files.")
    if picture and ocr == "never":
        raise ValueError(
            f"Text in {source.name} pictures can only be read with OCR, and ocr is 'never'."
        )
    return steps


def _with_ocr(options: dict[str, Any] | None, ocr: bool) -> dict[str, Any] | None:
    if not ocr:
        return options
    return {**(options or {}), "ocr": True}


def _step_options(options: dict[str, Any] | None, index: int) -> dict[str, Any] | None:
    """The settings of a route's step: ``pageRange`` picks pages in the first step only (slides
    made into a PDF of the pages picked; that PDF is then read whole)."""
    if index == 0 or not options or "pageRange" not in options:
        return options
    rest = {name: value for name, value in options.items() if name != "pageRange"}
    return rest or None


def _decode(data: bytes, encoding: str = "utf-8-sig") -> str:
    return data.decode(encoding)


@component
class ConverterStudioDocumentConverter(ConverterStudioComponent):
    """
    Converts files to Documents with [Converter Studio](https://converter.studio): scanned PDFs and
    pictures by OCR, PDFs with a text layer, Word, OpenDocument, RTF and HTML documents, e-books,
    spreadsheets and presentations.

    Each source is sent to the Converter Studio API, which makes plain text of it (or Markdown,
    with `output_format="markdown"`); the text becomes one `Document`. A PDF's text keeps its page
    breaks (form feeds), so Haystack's `DocumentSplitter` gives each chunk its `page_number`.
    Sources can be file paths, `ByteStream`s, or links (a `str` starting with `http://` or
    `https://`, including Google Drive, Dropbox and OneDrive share links):
    Converter Studio downloads a link itself. Plain-text formats (TXT, Markdown, CSV, JSON, XML,
    YAML, subtitles) are read locally, without a request.

    The API key is read from the `CONVERTER_STUDIO_API_KEY` environment variable by default.
    Every conversion uses the account's credits; see https://converter.studio/pricing.

    ### Usage example

    ```python
    from haystack_integrations.components.converters.converter_studio import (
        ConverterStudioDocumentConverter,
    )

    converter = ConverterStudioDocumentConverter()  # ocr="auto"
    result = converter.run(sources=["scanned-invoice.pdf", "notes.docx", "whiteboard.heic"])
    for document in result["documents"]:
        print(document.meta["file_path"], document.content[:80])
    ```
    """

    def __init__(
        self,
        *,
        api_key: Secret = Secret.from_env_var(API_KEY_ENV),
        base_url: str | None = None,
        ocr: OCRMode = "auto",
        output_format: OutputFormat = "text",
        options: dict[str, Any] | None = None,
        max_credits: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        store_full_path: bool = False,
        raise_on_failure: bool = False,
        concurrency_limit: int = DEFAULT_CONCURRENCY,
    ) -> None:
        """
        Creates a ConverterStudioDocumentConverter.

        :param api_key:
            The Converter Studio API key (create one at https://converter.studio/account#api-keys).
            Read from the `CONVERTER_STUDIO_API_KEY` environment variable by default.
        :param base_url:
            The API's address. `None` uses the `CONVERTER_STUDIO_BASE_URL` environment variable or
            `https://converter.studio/api`.
        :param ocr:
            When to read text with OCR: `"auto"` (a PDF's own text first, OCR when it has none),
            `"always"` (PDFs straight to OCR) or `"never"` (no OCR: scans and pictures are
            skipped). Pictures (JPG, PNG, HEIC, TIFF, WebP, BMP) are always read with OCR unless
            `"never"`.
        :param output_format:
            `"text"` (plain text) or `"markdown"`: Markdown for Word, OpenDocument, RTF and HTML
            documents, which keeps their headings, lists and tables (it costs the same). Other
            formats give the same text either way (a PDF's text with its page breaks, CSV for
            spreadsheets); with `"markdown"`, `meta["converter_studio"]["content_format"]` says
            which each Document holds: `"md"`, `"txt"`, `"csv"`...
        :param options:
            Conversion settings sent with every conversion, such as `{"ocrLanguage": "de"}` or
            `{"pageRange": "1-10"}` (see https://converter.studio/developers). Settings that do
            not apply to a conversion are ignored by the API. Set OCR with `ocr`, not here.
        :param max_credits:
            The most one conversion may cost and still start on its own. A dearer one is skipped
            and nothing is charged. `None` uses the API's default (20).
        :param timeout:
            Seconds to wait for each conversion.
        :param store_full_path:
            If `True`, the full path of a file is kept in the documents' `file_path` metadata;
            if `False`, only the file name.
        :param raise_on_failure:
            If `True`, a source that cannot be converted raises an error instead of being skipped
            with a warning. A missing or wrong API key, a lack of credits and an API that cannot
            be reached always raise.
        :param concurrency_limit:
            How many sources `run_async` converts at the same time.
        """
        if ocr not in get_args(OCRMode):
            raise ValueError(f"ocr must be one of {', '.join(get_args(OCRMode))}, not {ocr!r}")
        if isinstance(options, Mapping) and "ocr" in options:
            raise ValueError('Choose OCR with ocr="always" (or "auto", "never"), not in options')
        if output_format not in get_args(OutputFormat):
            raise ValueError(
                f"output_format must be one of {', '.join(get_args(OutputFormat))}, "
                f"not {output_format!r}"
            )
        # @component makes a new class from this one, so a bare super() would not find it.
        ConverterStudioComponent.__init__(
            self,
            api_key=api_key,
            base_url=base_url,
            options=options,
            max_credits=max_credits,
            timeout=timeout,
            raise_on_failure=raise_on_failure,
            concurrency_limit=concurrency_limit,
        )
        self.ocr: OCRMode = ocr
        self.output_format: OutputFormat = output_format
        self.store_full_path = store_full_path

    def to_dict(self) -> dict[str, Any]:
        """
        Serializes the component to a dictionary.

        :returns:
            Dictionary with serialized data.
        """
        return default_to_dict(
            self,
            ocr=self.ocr,
            output_format=self.output_format,
            store_full_path=self.store_full_path,
            **self._shared_init_parameters(),
        )

    @component.output_types(documents=list[Document])
    def run(
        self,
        sources: list[str | Path | ByteStream],
        meta: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> dict[str, list[Document]]:
        """
        Converts files to Documents.

        :param sources:
            File paths, `ByteStream`s or links (`http://` or `https://`) to convert.
        :param meta:
            Optional metadata to attach to the documents: a single dictionary, added to the
            metadata of all documents, or a list of dictionaries, one per source (its length must
            match the number of sources). A `ByteStream`'s own `meta` is added too.

        :returns:
            A dictionary with the following keys:
            - `documents`: The converted documents, in the order of the sources. A source that
              cannot be converted is left out, with a warning (see `raise_on_failure`).

        :raises ValueError: If `api_key` cannot be read.
        :raises converter_studio.ConverterStudioError: For a wrong API key, a lack of credits,
            too many requests or an API that cannot be reached; with `raise_on_failure`, also for
            a source that cannot be converted.
        """
        meta_list = normalize_metadata(meta, sources_count=len(sources))
        if not sources:
            return {"documents": []}
        client = self._ready_client()
        catalog = self._catalog_from(client)
        documents = []
        for source, extra in zip(sources, meta_list, strict=True):
            document = self._convert(client, catalog, source, extra)
            if document is not None:
                documents.append(document)
        return {"documents": documents}

    @component.output_types(documents=list[Document])
    async def run_async(
        self,
        sources: list[str | Path | ByteStream],
        meta: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> dict[str, list[Document]]:
        """
        Converts files to Documents, `concurrency_limit` at a time.

        This is the asynchronous version of the `run` method, with the same parameters and
        return values.

        :param sources:
            File paths, `ByteStream`s or links (`http://` or `https://`) to convert.
        :param meta:
            Optional metadata to attach to the documents: one dictionary for all, or a list with
            one per source.

        :returns:
            A dictionary with the following keys:
            - `documents`: The converted documents, in the order of the sources.
        """
        meta_list = normalize_metadata(meta, sources_count=len(sources))
        if not sources:
            return {"documents": []}
        client = await self._ready_async_client()
        catalog = await self._catalog_from_async(client)
        limit = asyncio.Semaphore(self.concurrency_limit)

        async def one(source: SourceType, extra: dict[str, Any]) -> Document | None:
            async with limit:
                return await self._convert_async(client, catalog, source, extra)

        tasks = [
            asyncio.ensure_future(one(source, extra))
            for source, extra in zip(sources, meta_list, strict=True)
        ]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return {"documents": [document for document in results if document is not None]}

    # One source

    def _convert(
        self, client: ConverterStudio, catalog: Catalog, source: SourceType, extra: dict[str, Any]
    ) -> Document | None:
        try:
            item = read_source(source)
        except (OSError, ValueError) as error:
            self._failed("Could not read {source}. Skipping it. Error: {error}", source, error)
            return None
        try:
            text, info = self._text(client, catalog, item, catalog.identify(item))
        except STOPPING_ERRORS:
            raise
        except (ConverterStudioError, OSError, ValueError) as error:
            self._failed("Could not convert {source}. Skipping it. Error: {error}", source, error)
            return None
        return Document(content=text, meta=self._meta(item, extra, info))

    async def _convert_async(
        self,
        client: AsyncConverterStudio,
        catalog: Catalog,
        source: SourceType,
        extra: dict[str, Any],
    ) -> Document | None:
        try:
            item = read_source(source)
        except (OSError, ValueError) as error:
            self._failed("Could not read {source}. Skipping it. Error: {error}", source, error)
            return None
        try:
            text, info = await self._text_async(client, catalog, item, catalog.identify(item))
        except STOPPING_ERRORS:
            raise
        except (ConverterStudioError, OSError, ValueError) as error:
            self._failed("Could not convert {source}. Skipping it. Error: {error}", source, error)
            return None
        return Document(content=text, meta=self._meta(item, extra, info))

    def _meta(self, item: Input, extra: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
        meta = {**item.meta, **extra, "converter_studio": info}
        if not self.store_full_path and (file_path := item.meta.get("file_path")):
            meta["file_path"] = Path(file_path).name
        return meta

    # Text

    def _text(
        self, client: ConverterStudio, catalog: Catalog, item: Input, source: Format | None
    ) -> tuple[str, dict[str, Any]]:
        if source is not None and source.id in LOCAL_TEXT_FORMATS:
            return self._local_text(item, source)
        steps = text_steps(source, self.ocr, self.output_format)
        file, name = catalog.upload(item, source)
        current, found, used_ocr = source, source.id if source else None, False
        try:
            for index, to in enumerate(steps):
                options = _step_options(self.options, index)
                conversion, data, ocr = self._step(client, file, name, current, to, options)
                found = found or conversion.from_format
                used_ocr = used_ocr or ocr or _read_from_picture(catalog, conversion, to)
                if index + 1 < len(steps):
                    file, name = data, conversion.output_name or f"document.{to}"
                    current = catalog.get(to)
        except UnprocessableError as error:
            # A source of unknown format: the API says what it is, and that is used instead.
            detected = _detected(catalog, error)
            if source is not None or detected is None:
                raise
            return self._text(client, catalog, item, detected)
        return _decode(data), self._info(found, used_ocr, steps[-1])

    async def _text_async(
        self, client: AsyncConverterStudio, catalog: Catalog, item: Input, source: Format | None
    ) -> tuple[str, dict[str, Any]]:
        if source is not None and source.id in LOCAL_TEXT_FORMATS:
            return self._local_text(item, source)
        steps = text_steps(source, self.ocr, self.output_format)
        file, name = catalog.upload(item, source)
        current, found, used_ocr = source, source.id if source else None, False
        try:
            for index, to in enumerate(steps):
                options = _step_options(self.options, index)
                conversion, data, ocr = await self._step_async(
                    client, file, name, current, to, options
                )
                found = found or conversion.from_format
                used_ocr = used_ocr or ocr or _read_from_picture(catalog, conversion, to)
                if index + 1 < len(steps):
                    file, name = data, conversion.output_name or f"document.{to}"
                    current = catalog.get(to)
        except UnprocessableError as error:
            detected = _detected(catalog, error)
            if source is not None or detected is None:
                raise
            return await self._text_async(client, catalog, item, detected)
        return _decode(data), self._info(found, used_ocr, steps[-1])

    def _local_text(self, item: Input, source: Format) -> tuple[str, dict[str, Any]]:
        if item.is_url:
            raise ValueError(
                f"{source.name} is text already: fetch it with Haystack's LinkContentFetcher "
                "and read it with TextFileToDocument."
            )
        data = item.file if isinstance(item.file, bytes) else Path(item.file).read_bytes()
        return _decode(data, item.encoding or "utf-8-sig"), self._info(source.id, False, source.id)

    def _info(self, source_format: str | None, ocr: bool, content_format: str) -> dict[str, Any]:
        """``meta["converter_studio"]``: what the file was, whether OCR read it and, when Markdown
        is asked for, the format of the Document's text (``md``, ``txt``, ``csv``...)."""
        info: dict[str, Any] = {"source_format": source_format, "ocr": ocr}
        if self.output_format == "markdown":
            info["content_format"] = content_format
        return info

    def _plan(self, current: Format | None, to: str) -> tuple[bool, bool]:
        """Whether this step starts with OCR, and whether it may fall back to OCR."""
        pdf_text = to == "txt" and (current is None or current.id == "pdf")
        return (pdf_text and self.ocr == "always", pdf_text and self.ocr == "auto")

    def _step(
        self,
        client: ConverterStudio,
        file: _File,
        name: str | None,
        current: Format | None,
        to: str,
        options: dict[str, Any] | None,
    ) -> tuple[Conversion, bytes, bool]:
        """One conversion and its result, and whether a PDF was read with OCR (as ``ocr`` says:
        ``auto`` asks for OCR only when the PDF turns out to have no text)."""
        ocr_first, may_retry = self._plan(current, to)
        try:
            conversion = self._convert_once(client, file, name, to, ocr_first, options)
            used_ocr = ocr_first and conversion.from_format == "pdf"
        except ConversionFailedError as failed:
            if not (may_retry and _no_text_in_pdf(failed)):
                raise
            logger.info("No text layer in {file}; reading it with OCR.", file=_label(file, name))
            conversion = self._convert_once(client, file, name, to, True, options)
            used_ocr = True
        return conversion, client.download_bytes(conversion), used_ocr

    async def _step_async(
        self,
        client: AsyncConverterStudio,
        file: _File,
        name: str | None,
        current: Format | None,
        to: str,
        options: dict[str, Any] | None,
    ) -> tuple[Conversion, bytes, bool]:
        ocr_first, may_retry = self._plan(current, to)
        try:
            conversion = await self._convert_once_async(client, file, name, to, ocr_first, options)
            used_ocr = ocr_first and conversion.from_format == "pdf"
        except ConversionFailedError as failed:
            if not (may_retry and _no_text_in_pdf(failed)):
                raise
            logger.info("No text layer in {file}; reading it with OCR.", file=_label(file, name))
            conversion = await self._convert_once_async(client, file, name, to, True, options)
            used_ocr = True
        return conversion, await client.download_bytes(conversion), used_ocr

    def _convert_once(
        self,
        client: ConverterStudio,
        file: _File,
        name: str | None,
        to: str,
        ocr: bool,
        options: dict[str, Any] | None,
    ) -> Conversion:
        return client.convert(
            file,
            to,
            file_name=name,
            options=_with_ocr(options, ocr),
            max_credits=self.max_credits,
            timeout=self.timeout,
        )

    async def _convert_once_async(
        self,
        client: AsyncConverterStudio,
        file: _File,
        name: str | None,
        to: str,
        ocr: bool,
        options: dict[str, Any] | None,
    ) -> Conversion:
        return await client.convert(
            file,
            to,
            file_name=name,
            options=_with_ocr(options, ocr),
            max_credits=self.max_credits,
            timeout=self.timeout,
        )


def _no_text_in_pdf(failed: ConversionFailedError) -> bool:
    return failed.code == "noText" and failed.conversion.from_format == "pdf"


def _read_from_picture(catalog: Catalog, conversion: Conversion, to: str) -> bool:
    """Pictures become text only by OCR."""
    source = catalog.get(conversion.from_format)
    return to == "txt" and source is not None and source.category == "image"


def _detected(catalog: Catalog, error: UnprocessableError) -> Format | None:
    """The format the API found in a file it cannot make text of directly."""
    if error.code != "unsupportedConversion":
        return None
    return catalog.get(str(error.params.get("from", "")))


def _label(file: _File, name: str | None) -> str:
    if name:
        return name
    return "a file" if isinstance(file, bytes) else str(file)
