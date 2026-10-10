"""Download a publicly shared Google Drive file."""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from datapulse.errors import DataPulseError
from datapulse.sources.file_link import SUPPORTED
from datapulse.sources.google_sheets import GoogleSheetsSource, parse_sheet_url
from datapulse.sources.http import fetch_bytes, require_str

_FILE_ID = re.compile(r"/file/d/([a-zA-Z0-9_-]+)")
_CONFIRM = re.compile(r"confirm=([0-9A-Za-z_-]+)")


def parse_drive_file_id(url: str) -> str:
    url = url.strip()
    match = _FILE_ID.search(url)
    if match:
        return match.group(1)
    qs = parse_qs(urlparse(url).query)
    if "id" in qs and qs["id"][0]:
        return qs["id"][0]
    raise DataPulseError(
        "Not a Google Drive file URL. Paste a link like "
        "https://drive.google.com/file/d/…/view or …/open?id=…"
    )


def _download_url(file_id: str, confirm: str | None = None) -> str:
    base = f"https://drive.google.com/uc?export=download&id={file_id}"
    return f"{base}&confirm={confirm}" if confirm else base


def _ext_from_disposition(header: str) -> str | None:
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', header, re.I)
    if not match:
        return None
    suffix = Path(match.group(1).strip().strip('"')).suffix.lower()
    return suffix if suffix in SUPPORTED else None


class GoogleDriveSource:
    kind = "google_drive"

    def fetch(self, config: dict, secrets: dict, dest_dir: Path) -> Path:
        url = require_str(config, "url", label="Google Drive URL")
        # A Sheets link pasted into the Drive form should still work.
        try:
            parse_sheet_url(url)
        except DataPulseError:
            pass
        else:
            return GoogleSheetsSource().fetch({"url": url}, secrets, dest_dir)

        file_id = parse_drive_file_id(url)
        body, headers, _ = fetch_bytes(_download_url(file_id))

        # Large files get an interstitial HTML page with a confirm token.
        if b"confirm=" in body[:8000] and b"<html" in body[:500].lower():
            token_match = _CONFIRM.search(body.decode("utf-8", errors="ignore"))
            if not token_match:
                raise DataPulseError(
                    "Google Drive asked for a download confirmation we could not parse. "
                    "Make sure the file is shared as 'Anyone with the link can view'."
                )
            body, headers, _ = fetch_bytes(_download_url(file_id, token_match.group(1)))

        if b"<html" in body[:500].lower() and (
            b"accounts.google.com" in body[:2000].lower() or b"Sign in" in body[:2000]
        ):
            raise DataPulseError(
                "This Google Drive file is not publicly readable. "
                "Share it as 'Anyone with the link can view', then try again."
            )
        if not body:
            raise DataPulseError("Downloaded Drive file is empty")

        ext = _ext_from_disposition(headers.get("content-disposition", "")) or ".csv"
        # Prefer an extension the loader understands; fall back to .bin → reject.
        if ext not in SUPPORTED:
            # Heuristic: JSON-looking content.
            sample = body.lstrip()[:1]
            if sample in (b"[", b"{"):
                ext = ".json"
            else:
                raise DataPulseError(
                    f"Unsupported Drive file type '{ext}'. "
                    "Share a data file (CSV, JSON, Parquet, Excel) or a document (PDF, Word, text, image)."
                )

        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{uuid.uuid4().hex}{ext}"
        path.write_bytes(body)
        return path
