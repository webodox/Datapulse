"""Read a table that was written into a document as text (a PDF of a spreadsheet, for example)."""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path

import pandas as pd

from datapulse.io import normalize_frame

_PAGE = re.compile(r"^\[page \d+\]$")
# Comma first: that is how a spreadsheet saved as a PDF of its rows is written.
_DELIMITERS = (",", "\t", "|", ";")


def document_frame(path, label: str) -> pd.DataFrame | None:
    """The spreadsheet inside a PDF, Word, text or HTML file, or None when it is prose, a scan, or a picture."""
    from datapulse.unifier.documents import full_text

    # Not read_documents: its text cap is sized for an AI prompt and would cut the table's rows off.
    text = full_text(Path(path), label)
    return None if text is None else table_from_text(text, label)


def table_from_text(text: str, label: str = "document") -> pd.DataFrame | None:
    """A DataFrame when `text` is a header plus rows in one delimiter. None for prose."""
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not _PAGE.match(line)]
    if len(lines) < 2:
        return None
    delimiter = _delimiter(lines)
    if delimiter is None:
        return None
    rows = list(csv.reader(lines, delimiter=delimiter))
    width = len(rows[0])
    if width < 2 or any(len(row) != width for row in rows):
        return None
    header = [cell.strip() for cell in rows[0]]
    if len(set(header)) != len(header) or any(not cell or len(cell) > 60 or len(cell.split()) > 4 for cell in header):
        return None
    # The same inference a CSV upload uses, so types match the spreadsheet the table was copied from.
    frame = pd.read_csv(io.StringIO("\n".join(lines)), sep=delimiter)
    if list(frame.columns) != header:
        return None
    return normalize_frame(frame, label)


def _delimiter(lines: list[str]) -> str | None:
    for delimiter in _DELIMITERS:
        widths = [len(row) for row in csv.reader(lines, delimiter=delimiter)]
        if widths and min(widths) >= 2 and len(set(widths)) == 1:
            return delimiter
    return None
