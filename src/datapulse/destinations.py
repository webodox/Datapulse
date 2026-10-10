"""Destinations: write a unified table into the company's own database.

The connection URL works like the "Database" source (PostgreSQL/Supabase, MySQL, SQL Server, MongoDB, any SQLAlchemy
database). Only the one named table (or collection) is ever written: "replace" swaps its contents for the new data,
"append" adds the new rows. Errors never show the password.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Tuple

import pandas as pd

from .sources.base import SourceError, _scrub, _secrets, error_text
from .sources.database import Database, type_label

MODES = ("replace", "append")
TABLE = re.compile(r"^[A-Za-z_][\w$]*(\.[A-Za-z_][\w$]*)?$")  # table or schema.table (MongoDB: database.collection)
CHUNK_ROWS = 1000


def check_settings(settings: Dict[str, str]) -> Dict[str, str]:
    if not settings.get("url"):
        raise SourceError("Paste your database's connection URL.")
    table = (settings.get("table") or "").strip()
    if not TABLE.match(table):
        raise SourceError("Name one table, like unified_orders or analytics.unified_orders (letters, numbers, _).")
    if settings.get("mode") not in MODES:
        raise SourceError("Choose replace or append.")
    return {"url": settings["url"].strip(), "table": table, "mode": settings["mode"]}


def _sql_engine(url: str):
    """A SQLAlchemy engine for a connection URL, built by the same plug-in the Database source would pick."""
    found, inner = Database().target({"url": url, "query": "unused"})
    return found.engine(inner)


def _mongo(url: str, table: str):
    found, inner = Database().target({"url": url, "query": table})
    client = found.client(inner)
    return client, found.database(client, inner)[inner["collection"]]


def _split(table: str) -> Tuple[Any, str]:
    schema, _, name = table.rpartition(".")
    return schema or None, name


def _guarded(settings: Dict[str, str], action):
    """Run a database step; errors are labelled with the database type and never contain the password."""
    label = type_label(settings["url"])
    try:
        return action()
    except SourceError as e:
        raise SourceError(_scrub(str(e), _secrets("database", settings))) from None
    except Exception as e:
        raise SourceError(f"{label}: {_scrub(error_text(e), _secrets('database', settings))}") from None


def _records(df: pd.DataFrame):
    """Rows as plain JSON-able values (NaN -> None, timestamps -> ISO text) for MongoDB."""
    return json.loads(df.to_json(orient="records", date_format="iso"))


def describe_target(settings: Dict[str, str]) -> str:
    """Connect and say what is there now, without writing anything."""
    settings = check_settings(settings)
    label = type_label(settings["url"])

    def run():
        if Database().target({"url": settings["url"], "query": settings["table"]})[0].kind == "mongodb":
            client, collection = _mongo(settings["url"], settings["table"])
            try:
                count = collection.estimated_document_count()
            finally:
                client.close()
            return f"{label}: connected. '{settings['table']}' has {count:,} documents."
        from sqlalchemy import inspect, text

        schema, name = _split(settings["table"])
        engine = _sql_engine(settings["url"])
        try:
            with engine.connect() as conn:
                if not inspect(conn).has_table(name, schema=schema):
                    return f"{label}: connected. '{settings['table']}' doesn't exist yet; the first send creates it."
                quoted = ".".join(conn.dialect.identifier_preparer.quote(p) for p in settings["table"].split("."))
                rows = conn.execute(text(f"SELECT COUNT(*) FROM {quoted}")).scalar()
        finally:
            engine.dispose()
        return f"{label}: connected. '{settings['table']}' has {rows:,} rows."
    return _guarded(settings, run)


def send(settings: Dict[str, str], df: pd.DataFrame) -> str:
    """Write df into the destination table; returns a short summary of what was written."""
    settings = check_settings(settings)
    label, table, mode = type_label(settings["url"]), settings["table"], settings["mode"]
    if df.empty:
        raise SourceError("There are no rows to send.")

    def run():
        if Database().target({"url": settings["url"], "query": table})[0].kind == "mongodb":
            client, collection = _mongo(settings["url"], table)
            try:
                if mode == "replace":
                    collection.delete_many({})
                collection.insert_many(_records(df))
            finally:
                client.close()
        else:
            schema, name = _split(table)
            engine = _sql_engine(settings["url"])
            try:
                with engine.begin() as conn:  # one transaction: all rows or none
                    df.to_sql(name, conn, schema=schema, if_exists=mode, index=False, chunksize=CHUNK_ROWS)
            finally:
                engine.dispose()
        verb = "Replaced the contents of" if mode == "replace" else "Added the rows to"
        return f"{verb} '{table}' in {label}: {len(df):,} rows."
    return _guarded(settings, run)
