# Changelog

All notable changes to this package are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the package uses
[semantic versioning](https://semver.org/).

## 1.1.0 - Unreleased

### Added

- `ConverterStudioDocumentConverter(output_format="markdown")`: Word (DOCX, DOC), OpenDocument
  (ODT), RTF and HTML documents become Markdown (GitHub's flavour, made by Pandoc), which keeps
  their headings, lists and tables; it costs the same as plain text. Every other format gives the
  text it gave before (a PDF's text with its page breaks, OCR text, CSV for spreadsheets). With
  this setting, `meta["converter_studio"]["content_format"]` says what each Document holds (`md`,
  `txt`, `csv`…). `OutputFormat` is exported for type hints.
- The default stays `output_format="text"`: Markdown changes the Documents' content (and so their
  IDs in a document store that already holds them), and steps written for plain text would meet
  Markdown marks. Pipelines saved by 1.0 load unchanged, and their Documents' content and metadata
  stay the same.

### Fixed

- `options={"pageRange": ...}` with a presentation (or another format that goes through a PDF)
  picked the pages twice: the slides picked became the PDF, then the same numbers were picked from
  that shorter PDF (missing pages, or a failed conversion). The page range now applies to the
  first step only.

### Documentation

- How page numbers work: a PDF's text keeps the API's page breaks (`\f`), and Haystack's
  `DocumentSplitter` gives every chunk its `page_number` from them (a test checks it).

## 1.0.0 - 2026-11-23

The first release.

- `ConverterStudioDocumentConverter`: files (paths, `ByteStream`s or links) to Haystack
  `Document`s through Converter Studio's text conversions: PDFs (OCR for scans, `ocr="auto"`,
  `"always"` or `"never"`), pictures by OCR, Word, OpenDocument, RTF and HTML documents, e-books,
  spreadsheets (CSV) and presentations (through a PDF); plain-text formats read locally.
- `ConverterStudioFileConverter`: files to another format (`output_file_type`), as `ByteStream`s
  with their MIME type and metadata.
- Both: `run_async` with `concurrency_limit`, `warm_up` / `warm_up_async` / `close` /
  `close_async`, `to_dict` / `from_dict` with the API key as a `Secret`
  (`CONVERTER_STUDIO_API_KEY`), `options` and `max_credits`, failures skipped with the API's
  message (or raised with `raise_on_failure`), and the account's errors always raised.
- Tested with Haystack 2.20, 2.29, 3.0 and 3.5.
