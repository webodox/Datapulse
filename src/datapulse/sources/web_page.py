"""Fetch a web page; the AI unifier extracts records from its content."""
from __future__ import annotations

import uuid
from pathlib import Path

from datapulse.errors import DataPulseError
from datapulse.sources.http import fetch_bytes, require_str


class WebPageSource:
    kind = "web_page"

    def fetch(self, config: dict, secrets: dict, dest_dir: Path) -> Path:
        url = require_str(config, "url", label="Web page URL")
        body, headers, _ = fetch_bytes(url, headers={"User-Agent": "DataPulse/0.1 (+data unifier)"})
        if not body.strip():
            raise DataPulseError("The web page was empty")
        ctype = headers.get("content-type", "").lower()
        ext = ".pdf" if "application/pdf" in ctype else ".txt" if ctype.startswith("text/plain") else ".html"
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{uuid.uuid4().hex}{ext}"
        path.write_bytes(body)
        return path
