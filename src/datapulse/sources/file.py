"""An uploaded file: CSV, Excel, JSON or Parquet, or a document the AI reads at upload. Edit the source to replace it."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .base import Field, Source, SourceError, register

STORED = re.compile(r"^[0-9a-f]{32}/[^/\\]+$")  # "<upload id>/<original file name>"


def files_dir() -> Path:
    """Where uploaded source files are kept; the server sets DATAPULSE_SOURCE_FILES."""
    folder = os.getenv("DATAPULSE_SOURCE_FILES")
    if not folder:
        raise SourceError("File sources are not set up on this server (DATAPULSE_SOURCE_FILES).")
    return Path(folder).resolve()


def stored_path(value: str) -> Path:
    root = files_dir()
    path = (root / value).resolve()
    if not STORED.match(value) or root not in path.parents or not path.is_file():
        raise SourceError("The uploaded file is missing; upload it again.")
    return path


@register
class File(Source):
    kind, label, group = "file", "File", "Files & links"
    description = "Upload a spreadsheet or data file (CSV, Excel, JSON, Parquet), or a PDF, Word file or image for the AI to read."
    fields = (Field("file", "File", "CSV, Excel, JSON, Parquet, PDF, Word, text or image · up to 25 MB", input="file"),)

    def test(self, settings):
        df = self.fetch(settings, 0)
        return f"Read the file: {len(df):,} rows, {len(df.columns)} columns."

    def fetch(self, settings, max_rows):
        from .connected import read_any

        path = stored_path(settings["file"])
        return read_any(path, path.name)
