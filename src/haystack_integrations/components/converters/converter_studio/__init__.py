"""Haystack components for Converter Studio (https://converter.studio).

- :class:`ConverterStudioDocumentConverter`: files (scanned PDFs, pictures, Office files,
  e-books...) to Documents, with OCR where needed.
- :class:`ConverterStudioFileConverter`: files to another format (PDF, JPG, MP3...), as
  ByteStreams.
"""

from ._version import __version__
from .document_converter import ConverterStudioDocumentConverter, OCRMode
from .file_converter import ConverterStudioFileConverter

__all__ = [
    "ConverterStudioDocumentConverter",
    "ConverterStudioFileConverter",
    "OCRMode",
    "__version__",
]
