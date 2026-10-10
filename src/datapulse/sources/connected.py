"""The unifier's connectors (REST APIs, web pages, Google Drive) and document reading, as menu plug-ins.

Downloaded files become tables here: data files (CSV, Excel, JSON, Parquet) are read directly, JSON records keep
their nesting as flat columns, and documents (PDF, Word, text, email, HTML, images) go through the AI unifier's
extract path, which pulls structured records out of them (one record of text per document without an API key).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from ..config import Config
from ..io import JSON_LINES, load_dataframe
from .base import Field, Source, SourceError, flatten, register


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


@register
class API(Source):
    kind, label, group = "api", "API", "Apps"
    description = "Any JSON API: paste the URL. Nested fields become columns; works with Shopify, Stripe, HubSpot…"
    fields = (
        Field("url", "API URL", "https://api.example.com/v1/orders"),
        Field("records_path", "Where the rows are", "data.items", required=False,
              help="Optional: the path to the list of records in the response. Found automatically if empty."),
        Field("auth", "Authorization header", "Bearer sk_live_…", secret=True, required=False,
              help="Optional: sent as the Authorization header. Stays on the server."),
    )

    def fetch(self, settings, max_rows):
        from .rest import RestSource

        secrets = {"headers": {"Authorization": settings["auth"]}} if settings.get("auth") else {}
        return download(RestSource(), {"url": settings["url"], "records_path": settings.get("records_path")},
                        secrets, "the API response")

    def test(self, settings):
        df = self.fetch(settings, 0)
        return f"Reached the API: {len(df):,} rows, {len(df.columns)} columns."


@register
class WebPage(Source):
    kind, label, group = "web_page", "Web page", "Apps"
    description = "A public web page or online PDF. The AI reads it on every pull and extracts the records it holds."
    fields = (Field("url", "Page URL", "https://example.com/prices"),)

    def fetch(self, settings, max_rows):
        from .web_page import WebPageSource

        return download(WebPageSource(), {"url": settings["url"]}, {}, settings["url"])

    def test(self, settings):
        df = self.fetch(settings, 0)
        return f"Read the page: {len(df):,} record(s), {len(df.columns)} columns."
