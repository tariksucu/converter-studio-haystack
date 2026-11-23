# Changelog

All notable changes to this package are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the package uses
[semantic versioning](https://semver.org/).

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
