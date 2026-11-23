# Converter Studio for Haystack

[Haystack](https://haystack.deepset.ai) components for [Converter Studio](https://converter.studio),
a file conversion service with OCR:

- **`ConverterStudioDocumentConverter`** turns files into Haystack `Document`s: scanned PDFs and
  photos of pages (by OCR), PDFs with a text layer, Word, OpenDocument, RTF and HTML documents,
  e-books (EPUB, MOBI, AZW3, FB2), spreadsheets and presentations.
- **`ConverterStudioFileConverter`** converts files to another format and returns them as
  `ByteStream`s: Office files and e-books to PDF, PDFs to Word or JPG, HEIC and camera RAW photos
  to JPG, videos to MP4 or MP3, and the other conversions of Converter Studio's 67 formats.

The conversions run on Converter Studio's servers, so there is nothing else to install: no
Tesseract, no LibreOffice, no Poppler, no FFmpeg.

```python
from haystack_integrations.components.converters.converter_studio import (
    ConverterStudioDocumentConverter,
)

converter = ConverterStudioDocumentConverter()  # the key comes from CONVERTER_STUDIO_API_KEY
documents = converter.run(sources=["scanned-contract.pdf", "minutes.docx"])["documents"]
```

## Installation

```console
$ pip install converter-studio-haystack
```

Python 3.11 or later, Haystack 3 (`haystack-ai` 3.x; Haystack 2.20 and later work too). The
package installs the official [Converter Studio Python client](https://pypi.org/project/converter-studio/).

Create an API key on your [account page](https://converter.studio/account#api-keys) (keys start
with `cvs_live_`; keep them secret) and put it in the environment:

```console
$ export CONVERTER_STUDIO_API_KEY=cvs_live_...
```

Both components read the key with Haystack's `Secret` (`Secret.from_env_var("CONVERTER_STUDIO_API_KEY")`
by default), so it is never written into a serialized pipeline. The key is read when the
component is warmed up, not when it is created.

Every conversion uses your account's credits, the same as on the website: free accounts get
credits every day, Pro and Premium their monthly credits. Most documents cost 1 credit; reading a
picture with OCR costs 3, and a scanned PDF's OCR is priced by its number of pages. Failed
conversions cost nothing. See [pricing](https://converter.studio/pricing).

## Files to Documents

```python
from haystack_integrations.components.converters.converter_studio import (
    ConverterStudioDocumentConverter,
)

converter = ConverterStudioDocumentConverter(ocr="auto")
result = converter.run(
    sources=["invoice-scan.pdf", "handbook.epub", "whiteboard.heic", "deck.pptx"],
    meta={"project": "onboarding"},
)
for document in result["documents"]:
    print(document.meta["file_path"], document.meta["converter_studio"], document.content[:60])
```

Sources can be file paths, `ByteStream`s, or links (a `str` starting with `http://` or `https://`,
Google Drive, Dropbox and OneDrive share links included): Converter Studio downloads a link
itself, so the file never passes through your machine.

How each kind of file becomes text:

| Source                                              | What happens                                                                                                                    |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| PDF                                                 | Its text layer (pages separated by `\f`, as Haystack's own PDF converters do); a scan without text is read with OCR (see `ocr`) |
| JPG, PNG, HEIC, TIFF, WebP, BMP                     | Read with OCR (the language is found on its own, or set `options={"ocrLanguage": "de"}`)                                        |
| DOCX, DOC, ODT, RTF, HTML                           | Plain text                                                                                                                      |
| EPUB, MOBI, AZW3, FB2                               | Plain text                                                                                                                      |
| XLSX, XLS, ODS                                      | CSV text of the first sheet (`options={"sheet": 2}` for another)                                                                |
| PPTX, PPT, ODP, SVG, EPS, AI, camera RAW, GIF, AVIF | Made into a PDF, then its text (two conversions)                                                                                |
| TXT, Markdown, CSV, JSON, XML, YAML, SRT, VTT, ASS  | Read here, without a request and without credits                                                                                |
| Audio, video, archives, fonts                       | Skipped with a warning (no text to read)                                                                                        |

`ocr` says when to use OCR (optical character recognition):

- `"auto"` (default): a PDF's own text first; only a PDF without any (a scan) is read again with
  OCR. The first try costs nothing when it finds no text.
- `"always"`: PDFs go straight to OCR, for folders of scans.
- `"never"`: no OCR; scanned PDFs and pictures are skipped.

OCR reads up to 50 pages of a PDF. Its text keeps the page's lines and table columns, without page
separators.

Each `Document` carries the metadata Haystack's converters carry: a `ByteStream`'s own `meta`,
`file_path` (the file name, or the full path with `store_full_path=True`), `url` for a link, and
the `meta` you pass to `run` (a dictionary for all sources, or a list with one per source). It
also has `converter_studio`: `{"source_format": "pdf", "ocr": true}`, what the file was and
whether OCR read it.

## Files to other formats

```python
from haystack_integrations.components.converters.converter_studio import (
    ConverterStudioFileConverter,
)

converter = ConverterStudioFileConverter(output_file_type="pdf")
result = converter.run(sources=["slides.pptx", "letter.docx"])
for stream in result["output"]:
    print(stream.meta["converter_studio"]["file_name"], stream.mime_type, len(stream.data))

mp3 = converter.run(sources=["interview.mov"], output_file_type="mp3")["output"]
```

The results are `ByteStream`s with their MIME type, the source's metadata, and
`meta["converter_studio"]`: `source_format`, `output_format` and `file_name` (the result's name,
such as `slides.pdf`). They connect to anything that takes `ByteStream`s, for example a legacy
`.doc` to Haystack's `DOCXToDocument` (which needs `pip install python-docx`):

```python
from haystack import Pipeline
from haystack.components.converters import DOCXToDocument
from haystack_integrations.components.converters.converter_studio import (
    ConverterStudioFileConverter,
)

pipeline = Pipeline()
pipeline.add_component("to_docx", ConverterStudioFileConverter(output_file_type="docx"))
pipeline.add_component("docx", DOCXToDocument())
pipeline.connect("to_docx.output", "docx.sources")
result = pipeline.run({"to_docx": {"sources": ["old-report.doc"]}})
print(result["docx"]["documents"][0].content[:80])
```

A PDF made into pictures (`jpg`, `png`, `webp`, `tiff`) comes back as one picture per page, in a
ZIP when it has more than one page. `options` takes the API's conversion settings, such as
`{"quality": "high"}`, `{"height": "720"}` for a video, or `{"ocr": True}` for a scanned PDF to
Word; the full list is on the [API page](https://converter.studio/developers).

## Example: index scanned PDFs with OCR

This pipeline reads every PDF in `scans/` (scanned or not), splits the text and writes it to an
in-memory document store, then finds passages with BM25. It needs only a Converter Studio key.

```python
from pathlib import Path

from haystack import Pipeline
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.retrievers.in_memory import InMemoryBM25Retriever
from haystack.components.writers import DocumentWriter
from haystack.document_stores.in_memory import InMemoryDocumentStore
from haystack_integrations.components.converters.converter_studio import (
    ConverterStudioDocumentConverter,
)

store = InMemoryDocumentStore()
indexing = Pipeline()
indexing.add_component("converter", ConverterStudioDocumentConverter(ocr="auto"))
indexing.add_component("splitter", DocumentSplitter(split_by="word", split_length=150))
indexing.add_component("writer", DocumentWriter(document_store=store))
indexing.connect("converter", "splitter")
indexing.connect("splitter", "writer")

indexing.run({"converter": {"sources": sorted(Path("scans").glob("*.pdf"))}})
print(store.count_documents(), "passages")

retriever = InMemoryBM25Retriever(document_store=store)
for document in retriever.run(query="invoice total", top_k=3)["documents"]:
    print(
        document.meta["file_path"], document.meta["converter_studio"]["ocr"], document.content[:80]
    )
```

## Async pipelines

Both components have `run_async`, which converts `concurrency_limit` sources at a time (4 by
default), and `warm_up_async` / `close_async` for their HTTP clients:

```python
import asyncio

from haystack import Pipeline
from haystack_integrations.components.converters.converter_studio import (
    ConverterStudioDocumentConverter,
)


async def main() -> None:
    pipeline = Pipeline()
    pipeline.add_component("converter", ConverterStudioDocumentConverter(concurrency_limit=8))
    result = await pipeline.run_async({"converter": {"sources": ["a.pdf", "b.docx", "c.png"]}})
    print(len(result["converter"]["documents"]))
    await pipeline.close_async()


asyncio.run(main())
```

(On Haystack 2, use `AsyncPipeline`.)

## Settings

Both components take:

| Parameter           | Default                                           | What it does                                                                         |
| ------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `api_key`           | `Secret.from_env_var("CONVERTER_STUDIO_API_KEY")` | the API key                                                                          |
| `base_url`          | `None`                                            | the API's address (`CONVERTER_STUDIO_BASE_URL` or `https://converter.studio/api`)    |
| `options`           | `None`                                            | conversion settings sent with every conversion (ignored where they do not apply)     |
| `max_credits`       | `None` (the API's 20)                             | the most one conversion may cost and start on its own; a dearer one is skipped, free |
| `timeout`           | `600.0`                                           | seconds to wait for each conversion                                                  |
| `raise_on_failure`  | `False`                                           | raise instead of skipping a source that cannot be converted                          |
| `concurrency_limit` | `4`                                               | sources converted at the same time by `run_async`                                    |

`ConverterStudioDocumentConverter` also takes `ocr` (`"auto"`, `"always"`, `"never"`) and
`store_full_path`; `ConverterStudioFileConverter` takes `output_file_type`, which `run` can
override.

Both serialize with `to_dict` / `from_dict` and load from YAML (`Pipeline.loads`) without extra
settings: the package lives in Haystack's `haystack_integrations` namespace.

## Errors

A source that cannot be converted (a damaged file, a format that cannot become text or the format
asked for, a conversion over `max_credits`, a file over the plan's size limit) is skipped and
logged as a warning with the API's own message, the way Haystack's converters skip unreadable
files.
With `raise_on_failure=True` the error is raised instead.

Some problems would hit every following source too, so they always stop the run with an error
from the [Python client](https://pypi.org/project/converter-studio/) carrying the API's message
and code: no API key (`ValueError` at warm-up), a key that does not work
(`AuthenticationError`), not enough credits (`InsufficientCreditsError`), a plan that does not
allow it (`PermissionDeniedError`), too many requests (`RateLimitError`) and an API that cannot be
reached (`APIConnectionError`). Network errors and busy answers are retried first, and every
conversion is created with an `Idempotency-Key`, so a retry is never charged twice. Failed
conversions are not charged. The API key is never logged.

## Limits

- The largest file depends on the plan; files over 95 MB are uploaded in parts on their own.
- OCR reads up to 50 pages of a PDF; audio and video length depends on the plan.
- One file in, one file out: no merging or splitting of PDFs. A result is held in memory as a
  `ByteStream`.
- Converter Studio keeps results for 6 hours, then deletes them.

## Privacy

Your files are sent to Converter Studio (converter.studio) to be converted; a link is downloaded
by Converter Studio. Uploaded files are deleted as soon as they are converted, and results 6 hours
later. Each request also says which client sent it (`X-Converter-Studio-Client: haystack/<version>`),
for usage statistics only. See the [privacy policy](https://converter.studio/privacy) and the
[terms](https://converter.studio/terms).

## Links

- Converter Studio: <https://converter.studio>
- API documentation: <https://converter.studio/developers>
- Pricing: <https://converter.studio/pricing>
- Python client: <https://pypi.org/project/converter-studio/>
- Source code and issues: <https://github.com/tariksucu/converter-studio-haystack>
- Changelog: <https://github.com/tariksucu/converter-studio-haystack/blob/main/CHANGELOG.md>
- Support: <support@converter.studio>

MIT licensed. © Converter Studio.
