"""What both components share: the API client's life cycle and the client name, reading a
source, the API's list of formats, and which errors stop a run instead of skipping a source."""

import mimetypes
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Self, TypeAlias
from urllib.parse import urlsplit

import httpx
from haystack import default_from_dict, logging
from haystack.dataclasses import ByteStream
from haystack.utils import Secret, deserialize_secrets_inplace

from converter_studio import (
    APIConnectionError,
    AsyncConverterStudio,
    AuthenticationError,
    ConverterStudio,
    Format,
    InsufficientCreditsError,
    MissingApiKeyError,
    PermissionDeniedError,
    RateLimitError,
)

from ._version import __version__

logger = logging.getLogger(__name__)

API_KEY_ENV = "CONVERTER_STUDIO_API_KEY"
#: Sent on every request in X-Converter-Studio-Client and in front of the User-Agent (the Python
#: client's ``client_name``), for usage statistics only.
CLIENT_NAME = f"haystack/{__version__}"
#: Seconds for each network step of a request (connecting, sending or reading a chunk).
NETWORK_TIMEOUT = 60.0
#: Seconds to wait for one conversion.
DEFAULT_TIMEOUT = 600.0
#: Conversions running at the same time in ``run_async``.
DEFAULT_CONCURRENCY = 4

SourceType: TypeAlias = str | Path | ByteStream

#: Errors every following source would meet too (no key, a wrong key, no credits, a plan that
#: does not allow it, too many requests, the API out of reach): they stop the run.
STOPPING_ERRORS: tuple[type[Exception], ...] = (
    MissingApiKeyError,
    AuthenticationError,
    PermissionDeniedError,
    InsufficientCreditsError,
    RateLimitError,
    APIConnectionError,
)

# MIME types Python's own table may not know, by the format they are.
_MIME_EXTENSIONS = {
    "image/heic": "heic",
    "image/heif": "heic",
    "image/avif": "avif",
    "image/webp": "webp",
    "image/x-adobe-dng": "dng",
    "image/x-canon-cr2": "cr2",
    "image/x-nikon-nef": "nef",
    "image/x-sony-arw": "arw",
    "image/svg+xml": "svg",
    "application/postscript": "eps",
    "text/markdown": "md",
    "application/yaml": "yaml",
    "application/x-yaml": "yaml",
    "text/yaml": "yaml",
    "application/epub+zip": "epub",
    "application/x-mobipocket-ebook": "mobi",
    "application/vnd.amazon.ebook": "azw3",
    "application/x-fictionbook+xml": "fb2",
    "application/x-7z-compressed": "7z",
    "application/vnd.rar": "rar",
    "application/x-rar-compressed": "rar",
    "text/vtt": "vtt",
    "application/x-subrip": "srt",
    "font/ttf": "ttf",
    "font/otf": "otf",
    "font/woff": "woff",
    "font/woff2": "woff2",
}
_EXTENSION_MIMES = {
    **{extension: mime for mime, extension in reversed(_MIME_EXTENSIONS.items())},
    "tar.gz": "application/gzip",
    "tgz": "application/gzip",
}


def is_url(source: object) -> bool:
    return isinstance(source, str) and source.lower().startswith(("http://", "https://"))


def extension_of(name: str | None) -> str | None:
    """A file name's extension, lower case and without the dot (``tar.gz`` counts as one)."""
    if not name:
        return None
    lower = name.lower()
    if lower.endswith(".tar.gz"):
        return "tar.gz"
    suffix = PurePosixPath(lower).suffix
    return suffix[1:] or None


def extension_for_mime(mime_type: str | None) -> str | None:
    """The extension a MIME type stands for, when it names a specific format."""
    if not mime_type:
        return None
    bare = mime_type.split(";", 1)[0].strip().lower()
    if bare in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[bare]
    guessed = mimetypes.guess_extension(bare, strict=False)
    return guessed[1:] if guessed and guessed != ".bin" else None


def mime_type_for(name: str | None) -> str | None:
    """The MIME type of a file by its name."""
    extension = extension_of(name)
    if extension in _EXTENSION_MIMES:
        return _EXTENSION_MIMES[extension]
    guessed, _ = (
        mimetypes.guess_type(f"file.{extension}", strict=False) if extension else (None, None)
    )
    return guessed


@dataclass(frozen=True)
class Input:
    """A source made ready for the client."""

    #: A link (``str``), a local path, or the bytes of a ``ByteStream``.
    file: str | Path | bytes
    #: The name a ``ByteStream`` has in its metadata (``file_path``, ``file_name`` or ``name``).
    name: str | None
    #: The extension its name gives, and the one its MIME type gives (``ByteStream`` only).
    name_extension: str | None
    type_extension: str | None
    #: The metadata it brings: a ``ByteStream``'s own, ``file_path`` of a path, ``url`` of a link.
    meta: dict[str, Any]
    #: A ``ByteStream``'s ``encoding`` (Haystack's convention), for reading text here.
    encoding: str | None = None

    @property
    def is_url(self) -> bool:
        return isinstance(self.file, str)


def read_source(source: SourceType) -> Input:
    """What to send for a source: a ``ByteStream``'s bytes, a path, or a link (a ``str`` starting
    with ``http://`` or ``https://``; Converter Studio downloads the file itself)."""
    if isinstance(source, ByteStream):
        meta = dict(source.meta or {})
        raw = meta.get("file_path") or meta.get("file_name") or meta.get("name")
        name = PurePosixPath(str(raw).replace("\\", "/")).name if raw else None
        encoding = meta.get("encoding")
        return Input(
            file=source.data,
            name=name or None,
            name_extension=extension_of(name),
            type_extension=extension_for_mime(source.mime_type),
            meta=meta,
            encoding=encoding if isinstance(encoding, str) else None,
        )
    if isinstance(source, str) and is_url(source):
        last = PurePosixPath(urlsplit(source).path).name
        return Input(
            file=source,
            name=None,
            name_extension=extension_of(last),
            type_extension=None,
            meta={"url": source},
        )
    if isinstance(source, str | Path):
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"No such file: {path}")
        return Input(
            file=path,
            name=path.name,
            name_extension=extension_of(path.name),
            type_extension=None,
            meta={"file_path": str(source)},
        )
    raise ValueError(f"Unsupported source type {type(source).__name__}")


def describe(source: SourceType) -> str:
    """A source in a log line: its path or link, or a ``ByteStream``'s name (never its bytes)."""
    if isinstance(source, ByteStream):
        meta = source.meta or {}
        raw = meta.get("file_path") or meta.get("file_name") or meta.get("name")
        return f"ByteStream {raw}" if raw else f"a ByteStream of {len(source.data)} bytes"
    return str(source)


class Catalog:
    """The formats the API reads (``GET /v1/formats``, no key needed), by id and extension."""

    def __init__(self, formats: list[Format]) -> None:
        self.by_id: dict[str, Format] = {item.id: item for item in formats}
        self.by_extension: dict[str, Format] = {}
        for item in formats:
            for extension in (item.id, *item.extensions):
                self.by_extension.setdefault(extension.lower(), item)

    def get(self, format_id: str | None) -> Format | None:
        return self.by_id.get(format_id) if format_id else None

    def identify(self, item: Input) -> Format | None:
        """What a source is: by a ``ByteStream``'s MIME type first, then by its name."""
        return self.by_extension.get(item.type_extension or "") or self.by_extension.get(
            item.name_extension or ""
        )

    def upload(self, item: Input, known: Format | None) -> tuple[str | Path | bytes, str | None]:
        """What to hand to the client and, for bytes, the file name to send with them.

        A ``ByteStream`` whose name says something else than its MIME type (``slides.pptx``
        holding the PDF made from it) is sent under the name its MIME type gives (``slides.pdf``);
        one without a name is sent as ``document`` (the API also reads the format from the bytes).
        """
        if not isinstance(item.file, bytes):
            return item.file, None
        name = item.name or "document"
        by_name = self.by_extension.get(item.name_extension or "")
        if known is None or by_name is known:
            return item.file, name
        if by_name is not None and item.name_extension:
            name = name[: -(len(item.name_extension) + 1)]
        extension = known.extensions[0] if known.extensions else known.id
        return item.file, f"{name}.{extension}"


class ConverterStudioComponent:
    """The settings, the client's life cycle and the error rule both components share.

    Not a component itself. The API client is made in :meth:`warm_up` (the API key is read
    there, not in ``__init__``) and released in :meth:`close`; ``run`` warms up on its own when
    it has not been.
    """

    def __init__(
        self,
        *,
        api_key: Secret,
        base_url: str | None,
        options: Mapping[str, Any] | None,
        max_credits: int | None,
        timeout: float,
        raise_on_failure: bool,
        concurrency_limit: int,
    ) -> None:
        if options is not None and not isinstance(options, Mapping):
            raise ValueError("options must be a dictionary of the API's conversion settings")
        if max_credits is not None and (
            isinstance(max_credits, bool) or not isinstance(max_credits, int) or max_credits < 0
        ):
            raise ValueError("max_credits must be a whole number, 0 or more, or None")
        if timeout <= 0:
            raise ValueError("timeout must be more than 0 seconds")
        if isinstance(concurrency_limit, bool) or concurrency_limit < 1:
            raise ValueError("concurrency_limit must be 1 or more")
        self.api_key = api_key
        self.base_url = base_url
        self.options = dict(options) if options else None
        self.max_credits = max_credits
        self.timeout = timeout
        self.raise_on_failure = raise_on_failure
        self.concurrency_limit = concurrency_limit
        self._client: ConverterStudio | None = None
        self._http: httpx.Client | None = None
        self._async_client: AsyncConverterStudio | None = None
        self._async_http: httpx.AsyncClient | None = None
        self._catalog: Catalog | None = None
        # Tests replace the network here.
        self._transport: httpx.BaseTransport | None = None
        self._async_transport: httpx.AsyncBaseTransport | None = None

    # Life cycle

    def warm_up(self) -> None:
        """Makes the API client and reads the API key (raises when ``api_key`` cannot be read).

        Calling it again does nothing until :meth:`close`.
        """
        if self._client is not None:
            return
        api_key = self._resolve_api_key()
        http = httpx.Client(
            timeout=NETWORK_TIMEOUT, follow_redirects=False, transport=self._transport
        )
        try:
            self._client = ConverterStudio(
                api_key=api_key, base_url=self.base_url, http_client=http, client_name=CLIENT_NAME
            )
        except BaseException:
            http.close()
            raise
        self._http = http

    async def warm_up_async(self) -> None:
        """Makes the asynchronous API client on the running event loop; see :meth:`warm_up`."""
        if self._async_client is not None:
            return
        api_key = self._resolve_api_key()
        http = httpx.AsyncClient(
            timeout=NETWORK_TIMEOUT, follow_redirects=False, transport=self._async_transport
        )
        try:
            self._async_client = AsyncConverterStudio(
                api_key=api_key, base_url=self.base_url, http_client=http, client_name=CLIENT_NAME
            )
        except BaseException:
            await http.aclose()
            raise
        self._async_http = http

    def close(self) -> None:
        """Closes the synchronous client's connections."""
        if self._http is not None:
            self._http.close()
        self._client = None
        self._http = None

    async def close_async(self) -> None:
        """Closes the asynchronous client's connections."""
        if self._async_http is not None:
            await self._async_http.aclose()
        self._async_client = None
        self._async_http = None

    def _resolve_api_key(self) -> str | None:
        value = self.api_key.resolve_value()
        return value if isinstance(value, str) else None

    def _ready_client(self) -> ConverterStudio:
        self.warm_up()
        if self._client is None:  # pragma: no cover (warm_up made it)
            raise RuntimeError("The Converter Studio client could not be made")
        return self._client

    async def _ready_async_client(self) -> AsyncConverterStudio:
        await self.warm_up_async()
        if self._async_client is None:  # pragma: no cover (warm_up_async made it)
            raise RuntimeError("The Converter Studio client could not be made")
        return self._async_client

    def _catalog_from(self, client: ConverterStudio) -> Catalog:
        if self._catalog is None:
            self._catalog = Catalog(client.formats())
        return self._catalog

    async def _catalog_from_async(self, client: AsyncConverterStudio) -> Catalog:
        if self._catalog is None:
            self._catalog = Catalog(await client.formats())
        return self._catalog

    # Failures

    def _failed(self, message: str, source: SourceType, error: Exception) -> None:
        """A source that could not be converted: skipped with a warning (the API's own message),
        or raised again with ``raise_on_failure``."""
        if self.raise_on_failure:
            raise error
        logger.warning(message, source=describe(source), error=str(error))

    # Serialization

    def _shared_init_parameters(self) -> dict[str, Any]:
        return {
            "api_key": self.api_key.to_dict(),
            "base_url": self.base_url,
            "options": self.options,
            "max_credits": self.max_credits,
            "timeout": self.timeout,
            "raise_on_failure": self.raise_on_failure,
            "concurrency_limit": self.concurrency_limit,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserializes the component from a dictionary.

        :param data: The dictionary to deserialize from (it is not changed).
        :returns: The deserialized component.
        """
        init_parameters = dict(data.get("init_parameters", {}))
        deserialize_secrets_inplace(init_parameters, keys=["api_key"])
        return default_from_dict(cls, {**data, "init_parameters": init_parameters})
