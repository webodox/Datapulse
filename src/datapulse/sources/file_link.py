"""Download a public file URL: data files (CSV / JSON / JSONL / Parquet / Excel) or documents (PDF, Word…)."""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from datapulse.errors import DataPulseError
from datapulse.io import ARCHIVES, INPUTS
from datapulse.sources.http import fetch_bytes, require_str

SUPPORTED = tuple(e for e in INPUTS if e not in ARCHIVES)

# Content-Type → extension when the URL has no useful suffix.
_CONTENT_TYPES = {
    "text/csv": ".csv",
    "application/csv": ".csv",
    "application/json": ".json",
    "application/x-ndjson": ".jsonl",
    "application/parquet": ".parquet",
    "application/vnd.apache.parquet": ".parquet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/html": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "message/rfc822": ".eml",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


def _ext_from_disposition(header: str) -> str | None:
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', header, re.I)
    if not match:
        return None
    name = unquote(match.group(1).strip())
    suffix = Path(name).suffix.lower()
    return suffix if suffix in SUPPORTED else None


def _ext_from_url(url: str) -> str | None:
    path = unquote(urlparse(url).path)
    suffix = Path(path).suffix.lower()
    return suffix if suffix in SUPPORTED else None


def _ext_from_content_type(ctype: str) -> str | None:
    base = ctype.split(";")[0].strip().lower()
    return _CONTENT_TYPES.get(base)


class FileLinkSource:
    kind = "file_link"

    def fetch(self, config: dict, secrets: dict, dest_dir: Path) -> Path:
        url = require_str(config, "url", label="File URL")
        body, headers, final = fetch_bytes(url)
        if not body:
            raise DataPulseError("Downloaded file is empty")

        ext = (
            _ext_from_disposition(headers.get("content-disposition", ""))
            or _ext_from_url(final)
            or _ext_from_url(url)
            or _ext_from_content_type(headers.get("content-type", ""))
        )
        if not ext:
            # Peek at bytes for JSON array / NDJSON heuristics.
            sample = body.lstrip()[:1]
            if sample in (b"[", b"{"):
                ext = ".json"
            else:
                raise DataPulseError(
                    "Could not detect file type. Use a URL ending in .csv, .json, .jsonl, .parquet, .xlsx, "
                    ".pdf, .docx, .txt, .md or .html"
                )

        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{uuid.uuid4().hex}{ext}"
        path.write_bytes(body)
        return path
