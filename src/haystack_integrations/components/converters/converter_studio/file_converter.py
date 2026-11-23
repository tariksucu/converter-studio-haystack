"""ConverterStudioFileConverter: files to other formats, as ByteStreams."""

import asyncio
from pathlib import Path
from typing import Any

from haystack import component, default_to_dict
from haystack.components.converters.utils import normalize_metadata
from haystack.dataclasses import ByteStream
from haystack.utils import Secret

from converter_studio import AsyncConverterStudio, Conversion, ConverterStudio, ConverterStudioError

from ._shared import (
    API_KEY_ENV,
    DEFAULT_CONCURRENCY,
    DEFAULT_TIMEOUT,
    STOPPING_ERRORS,
    Catalog,
    ConverterStudioComponent,
    Input,
    SourceType,
    mime_type_for,
    read_source,
)


@component
class ConverterStudioFileConverter(ConverterStudioComponent):
    """
    Converts files to another format with [Converter Studio](https://converter.studio) and returns
    them as `ByteStream`s: Office files, e-books and pictures to PDF, PDFs to Word or JPG, HEIC
    and camera RAW photos to JPG or WebP, videos to MP4 or MP3, and the other conversions of
    Converter Studio's 67 formats.

    Sources can be file paths, `ByteStream`s, or links (a `str` starting with `http://` or
    `https://`), which Converter Studio downloads itself. The results connect to Haystack's own
    converters (`PyPDFToDocument`, `DOCXToDocument`), to audio transcribers, or anything else that
    takes `ByteStream`s.

    ### Usage example

    ```python
    from haystack_integrations.components.converters.converter_studio import (
        ConverterStudioFileConverter,
    )

    converter = ConverterStudioFileConverter(output_file_type="pdf")
    result = converter.run(sources=["slides.pptx", "scan.heic"])
    for stream in result["output"]:
        print(stream.meta["converter_studio"]["file_name"], stream.mime_type, len(stream.data))
    ```
    """

    def __init__(
        self,
        *,
        output_file_type: str | None = None,
        api_key: Secret = Secret.from_env_var(API_KEY_ENV),
        base_url: str | None = None,
        options: dict[str, Any] | None = None,
        max_credits: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        raise_on_failure: bool = False,
        concurrency_limit: int = DEFAULT_CONCURRENCY,
    ) -> None:
        """
        Creates a ConverterStudioFileConverter.

        :param output_file_type:
            The format to convert to (`"pdf"`, `"jpg"`, `"mp3"`, `"docx"`...). It can also be
            given to `run`, which takes precedence. What each format converts to:
            https://converter.studio/developers.
        :param api_key:
            The Converter Studio API key (create one at https://converter.studio/account#api-keys).
            Read from the `CONVERTER_STUDIO_API_KEY` environment variable by default.
        :param base_url:
            The API's address. `None` uses the `CONVERTER_STUDIO_BASE_URL` environment variable or
            `https://converter.studio/api`.
        :param options:
            Conversion settings sent with every conversion, such as `{"quality": "high"}`,
            `{"height": "720"}` or `{"ocr": True}` for a scanned PDF to Word (see
            https://converter.studio/developers). Settings that do not apply are ignored.
        :param max_credits:
            The most one conversion may cost and still start on its own. A dearer one is skipped
            and nothing is charged. `None` uses the API's default (20).
        :param timeout:
            Seconds to wait for each conversion.
        :param raise_on_failure:
            If `True`, a source that cannot be converted raises an error instead of being skipped
            with a warning. A missing or wrong API key, a lack of credits and an API that cannot
            be reached always raise.
        :param concurrency_limit:
            How many sources `run_async` converts at the same time.
        """
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
        self.output_file_type = _target(output_file_type)

    def to_dict(self) -> dict[str, Any]:
        """
        Serializes the component to a dictionary.

        :returns:
            Dictionary with serialized data.
        """
        return default_to_dict(
            self, output_file_type=self.output_file_type, **self._shared_init_parameters()
        )

    @component.output_types(output=list[ByteStream])
    def run(
        self,
        sources: list[str | Path | ByteStream],
        output_file_type: str | None = None,
        meta: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> dict[str, list[ByteStream]]:
        """
        Converts files to `output_file_type`.

        :param sources:
            File paths, `ByteStream`s or links (`http://` or `https://`) to convert.
        :param output_file_type:
            The format to convert to; overrides the one given at initialization.
        :param meta:
            Optional metadata to attach to the results: one dictionary for all, or a list with one
            per source. A `ByteStream`'s own `meta` is kept, and a path's `file_path`.

        :returns:
            A dictionary with the following keys:
            - `output`: The converted files as `ByteStream`s, in the order of the sources, with
              their MIME type and, in `meta["converter_studio"]`, `source_format`,
              `output_format` and `file_name` (the result's name, such as `slides.pdf`). A source
              that cannot be converted is left out, with a warning (see `raise_on_failure`).

        :raises ValueError: If no `output_file_type` is given, or `api_key` cannot be read.
        :raises converter_studio.ConverterStudioError: For a wrong API key, a lack of credits,
            too many requests or an API that cannot be reached; with `raise_on_failure`, also for
            a source that cannot be converted.
        """
        to = self._target_for(output_file_type)
        meta_list = normalize_metadata(meta, sources_count=len(sources))
        if not sources:
            return {"output": []}
        client = self._ready_client()
        catalog = self._catalog_from(client)
        output = []
        for source, extra in zip(sources, meta_list, strict=True):
            stream = self._convert(client, catalog, source, extra, to)
            if stream is not None:
                output.append(stream)
        return {"output": output}

    @component.output_types(output=list[ByteStream])
    async def run_async(
        self,
        sources: list[str | Path | ByteStream],
        output_file_type: str | None = None,
        meta: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> dict[str, list[ByteStream]]:
        """
        Converts files to `output_file_type`, `concurrency_limit` at a time.

        This is the asynchronous version of the `run` method, with the same parameters and
        return values.

        :param sources:
            File paths, `ByteStream`s or links (`http://` or `https://`) to convert.
        :param output_file_type:
            The format to convert to; overrides the one given at initialization.
        :param meta:
            Optional metadata to attach to the results.

        :returns:
            A dictionary with the following keys:
            - `output`: The converted files as `ByteStream`s, in the order of the sources.
        """
        to = self._target_for(output_file_type)
        meta_list = normalize_metadata(meta, sources_count=len(sources))
        if not sources:
            return {"output": []}
        client = await self._ready_async_client()
        catalog = await self._catalog_from_async(client)
        limit = asyncio.Semaphore(self.concurrency_limit)

        async def one(source: SourceType, extra: dict[str, Any]) -> ByteStream | None:
            async with limit:
                return await self._convert_async(client, catalog, source, extra, to)

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
        return {"output": [stream for stream in results if stream is not None]}

    def _target_for(self, output_file_type: str | None) -> str:
        to = _target(output_file_type) or self.output_file_type
        if not to:
            raise ValueError("output_file_type must be given at initialization or to run()")
        return to

    def _convert(
        self,
        client: ConverterStudio,
        catalog: Catalog,
        source: SourceType,
        extra: dict[str, Any],
        to: str,
    ) -> ByteStream | None:
        try:
            item = read_source(source)
        except (OSError, ValueError) as error:
            self._failed("Could not read {source}. Skipping it. Error: {error}", source, error)
            return None
        try:
            file, name = catalog.upload(item, catalog.identify(item))
            conversion = client.convert(
                file,
                to,
                file_name=name,
                options=self.options,
                max_credits=self.max_credits,
                timeout=self.timeout,
            )
            data = client.download_bytes(conversion)
        except STOPPING_ERRORS:
            raise
        except (ConverterStudioError, OSError, ValueError) as error:
            self._failed("Could not convert {source}. Skipping it. Error: {error}", source, error)
            return None
        return _stream(item, extra, conversion, data, self.options)

    async def _convert_async(
        self,
        client: AsyncConverterStudio,
        catalog: Catalog,
        source: SourceType,
        extra: dict[str, Any],
        to: str,
    ) -> ByteStream | None:
        try:
            item = read_source(source)
        except (OSError, ValueError) as error:
            self._failed("Could not read {source}. Skipping it. Error: {error}", source, error)
            return None
        try:
            file, name = catalog.upload(item, catalog.identify(item))
            conversion = await client.convert(
                file,
                to,
                file_name=name,
                options=self.options,
                max_credits=self.max_credits,
                timeout=self.timeout,
            )
            data = await client.download_bytes(conversion)
        except STOPPING_ERRORS:
            raise
        except (ConverterStudioError, OSError, ValueError) as error:
            self._failed("Could not convert {source}. Skipping it. Error: {error}", source, error)
            return None
        return _stream(item, extra, conversion, data, self.options)


def _target(output_file_type: str | None) -> str | None:
    if output_file_type is None:
        return None
    to = output_file_type.strip().lower().lstrip(".")
    return to or None


def _stream(
    item: Input,
    extra: dict[str, Any],
    conversion: Conversion,
    data: bytes,
    options: dict[str, Any] | None,
) -> ByteStream:
    name = conversion.output_name
    to = conversion.to_format or ""
    info = {"source_format": conversion.from_format, "output_format": to, "file_name": name}
    # A source's `encoding` says how the source was written, not the result.
    meta = {key: value for key, value in item.meta.items() if key != "encoding"}
    if to in TEXT_OUTPUTS:
        # Converter Studio writes text as UTF-8, some of it with a byte order mark: Haystack's
        # text converters read a ByteStream's `encoding` (older ones decode plain utf-8).
        windows = to == "csv" and (options or {}).get("csvEncoding") == "windows1254"
        meta["encoding"] = "cp1254" if windows else "utf-8-sig"
    return ByteStream(
        data=data,
        mime_type=mime_type_for(name) or mime_type_for(f"file.{to}"),
        meta={**meta, **extra, "converter_studio": info},
    )


#: Results that are text, which Haystack's text converters can read.
TEXT_OUTPUTS = frozenset({"txt", "md", "csv", "html", "json", "xml", "yaml", "srt", "vtt", "ass"})
