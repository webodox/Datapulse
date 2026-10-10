"""Turn any supported file into a table.

Spreadsheets and JSON stay tables. A PDF that contains a table (one row of the data per line) is read back
as that table. Other PDFs, Word, email, text, HTML, images and zip archives go through the document unifier:
with an AI key, records are extracted; without one, each document is one row of its text.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from datapulse.config import Config
from datapulse.errors import DataPulseError
from datapulse.io import ARCHIVES, DOCUMENTS, INPUTS, load_dataframe
from datapulse.unifier.documents import expand_zip, is_document, is_tabular
from datapulse.unifier.pipeline import unify_files

from .base import SourceError
from .tabular_text import pdf_frame


def frame_from_path(path, label: str | None = None) -> pd.DataFrame:
    """A file on disk as a DataFrame. `label` is the name shown on document rows."""
    path = Path(path)
    label = label or path.name
    ext = path.suffix.lower()
    if ext in ARCHIVES:
        dest = Path(tempfile.mkdtemp())
        files, _skipped = expand_zip(path, dest, label)
        return _rows(files, label)
    if is_tabular(path):
        return load_dataframe(path)
    if is_document(path):
        if ext == ".pdf":
            table = _pdf_table(path, label)
            if table is not None:
                return table
        return _rows([(path, label)], label)
    raise SourceError(
        f"Unsupported file type '{path.suffix or 'unknown'}'. "
        "Use a spreadsheet (CSV, Excel, JSON, Parquet) or a document (PDF, Word, email, text, HTML, image, zip)."
    )


def frame_from_bytes(data: bytes, name: str) -> pd.DataFrame:
    """Bytes plus a file name (the extension decides how to read them)."""
    ext = Path(name).suffix.lower()
    if ext not in INPUTS:
        raise SourceError(f"Unsupported file type '{ext or 'unknown'}' for {name}")
    dest = Path(tempfile.mkdtemp())
    path = dest / f"download{ext}"
    path.write_bytes(data)
    return frame_from_path(path, name)


def _pdf_table(path, label: str) -> pd.DataFrame | None:
    return pdf_frame(path, label)


def _rows(files, label: str) -> pd.DataFrame:
    try:
        result = unify_files(files, None, Config(), source_label=label)
    except DataPulseError as exc:
        raise SourceError(str(exc)) from exc
    if not result.rows:
        raise SourceError(f"No records could be read from {label}")
    return pd.DataFrame(result.rows)
