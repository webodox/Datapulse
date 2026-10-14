"""Fetch a web page; the AI unifier extracts records from its content."""
from __future__ import annotations

import uuid
from pathlib import Path

import pandas as pd

from datapulse.errors import DataPulseError
from datapulse.sources.base import Field, Source, register
from datapulse.sources.http import fetch_bytes, require_str
from datapulse.unifier.documents import html_to_text


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


def page_frame(url: str) -> pd.DataFrame:
    """One row describing a page: its address, title and text."""
    body, headers, final = fetch_bytes(url, headers={"User-Agent": "DataPulse/0.1 (+data unifier)"})
    if not body.strip():
        raise DataPulseError("The web page was empty")
    ctype = headers.get("content-type", "").lower()
    if "html" not in ctype and not body.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
        text, title = body.decode("utf-8", errors="replace").strip(), ""
    else:
        title, text = html_to_text(body.decode("utf-8", errors="replace"))
    text = text.strip()
    if not text and not title:
        raise DataPulseError("The page has no readable text")
    return pd.DataFrame([{"url": final or url, "title": title[:300], "text": text[:50_000]}])


@register
class WebPage(Source):
    kind, label, group = "web_page", "Web page", "Files & links"
    description = "A public page. DataPulse reads its title and text into one row. Use test data to try example.com."
    requires = ("httpx",)
    has_sample = True
    fields = (Field("url", "Page URL", "https://example.com"),)

    def fetch(self, settings, max_rows):
        return page_frame(settings["url"]).head(max_rows + 1)

    def test(self, settings):
        row = page_frame(settings["url"]).iloc[0]
        title = f": {row['title']}" if row["title"] else ""
        return f"Read the page{title} ({len(str(row['text']).split()):,} words)."
