"""Reading downloaded or uploaded files into tables, shared by the File and Link sources.

Files become tables here: data files (CSV, Excel, JSON, Parquet) are read directly, JSON records keep
their nesting as flat columns, and documents (PDF, Word, text, email, HTML, images) go through the AI unifier's
extract path, which pulls structured records out of them (one record of text per document without an API key).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from ..config import Config
from ..io import JSON_LINES, load_dataframe
from .base import SourceError, flatten


def read_any(path: Path, label: str) -> pd.DataFrame:
    """A downloaded or uploaded file as a table."""
    from ..unifier import unify_files
    from ..unifier.documents import is_document, is_tabular
    from ..unifier.records import read_raw_records

    ext = path.suffix.lower()
    if ext in (".json", *JSON_LINES):
        return flatten(read_raw_records(path, label))  # API-style records, nesting kept as address.city columns
    if is_tabular(path):
        return load_dataframe(path)
    if is_document(path):
        result = unify_files([(path, label)], None, Config(), source_label=label)
        if not result.rows:
            raise SourceError(f"No records could be extracted from {label}.")
        return pd.DataFrame(result.rows)
    raise SourceError(f"{label}: unsupported file type '{ext or 'unknown'}'.")


def download(connector, config: dict, secrets: dict, label: str) -> pd.DataFrame:
    """Run one of the unifier's connectors into a temporary folder and read what it saved."""
    with tempfile.TemporaryDirectory(prefix="datapulse-") as folder:
        return read_any(connector.fetch(config, secrets, Path(folder)), label)

