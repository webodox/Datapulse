"""SQL databases through SQLAlchemy: one plug-in class, one entry per database in the menu.

Queries are read-only by construction: a table name becomes `SELECT * FROM <table>`, anything else must be a
single SELECT/WITH statement, and it runs in a transaction that is always rolled back (and is marked read-only
where the database supports it). Use a database user with read-only permissions anyway.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Tuple

import pandas as pd

from .base import Field, Source, SourceError, register

IDENTIFIER = re.compile(r"^[A-Za-z_][\w$]*(\.[A-Za-z_][\w$]*){0,2}$")  # table, schema.table, db.schema.table
QUERY_FIELD = Field("query", "Table or SQL query", "orders  or  SELECT * FROM orders WHERE created_at > now() - interval '1 day'",
                    multiline=True, help="A table name, or one SELECT query.")


def select_sql(query: str, quote) -> str:
    q = query.strip().rstrip(";").strip()
    if IDENTIFIER.match(q):
        return "SELECT * FROM " + ".".join(quote(part) for part in q.split("."))
    body = re.sub(r"--[^\n]*|/\*.*?\*/", " ", q, flags=re.S).strip()
    if not re.match(r"(?i)^(select|with)\b", body):
        raise SourceError("Only a table name or a SELECT query can be used; DataPulse never changes your data.")
    if ";" in body:
        raise SourceError("Use a single SELECT query (no ';').")
    return q


class SQLDatabase(Source):
    group = "Databases"
    hidden = True  # offered through the single "Database" source, which picks one of these from the URL
    requires: Tuple[str, ...] = ("sqlalchemy",)
    schemes: Tuple[str, ...] = ()   # URL schemes this entry accepts (empty: any except SQLite)
    driver = ""                     # SQLAlchemy driver used when the URL doesn't name one
    connect_args: Dict[str, Any] = {}
    fields = (Field("url", "Connection URL", "", secret=True), QUERY_FIELD)

    def url(self, settings: Dict[str, str]):
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError

        try:
            url = make_url(settings["url"])
        except ArgumentError:
            raise SourceError(f"That doesn't look like a connection URL. Example: {self.fields[0].placeholder}")
        backend, _, driver = url.drivername.partition("+")
        backend = "postgresql" if backend == "postgres" else backend  # Heroku/Railway style
        if backend == "sqlite":
            raise SourceError("Use the SQLite file source for SQLite databases.")
        if self.schemes and backend not in self.schemes:
            raise SourceError(f"{self.label} URLs start with {self.schemes[0]}://")
        if not driver and self.driver:
            return url.set(drivername=self.driver)
        return url.set(drivername=backend + (f"+{driver}" if driver else ""))

    def engine(self, settings: Dict[str, str]):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        return create_engine(self.url(settings), poolclass=NullPool, connect_args=dict(self.connect_args))

    def begin(self, conn) -> None:
        """Hook to make the transaction read-only where the database supports it."""

    def run(self, settings, rows: int):
        """Run the source's query read-only; returns (rows, column names)."""
        from sqlalchemy import text

        engine = self.engine(settings)
        try:
            with engine.connect() as conn:
                sql = select_sql(settings["query"], conn.dialect.identifier_preparer.quote)
                trans = conn.begin()
                try:
                    self.begin(conn)
                    result = conn.execute(text(sql))
                    return result.fetchmany(rows), list(result.keys())
                finally:
                    trans.rollback()
        finally:
            engine.dispose()

    def test(self, settings):
        _, columns = self.run(settings, 1)
        shown = ", ".join(columns[:8]) + ("…" if len(columns) > 8 else "")
        return f"Connected, and the query works: {len(columns)} column(s) ({shown})."

    def fetch(self, settings, max_rows):
        rows, columns = self.run(settings, max_rows + 1)
        return pd.DataFrame.from_records([tuple(r) for r in rows], columns=columns)


@register
class Postgres(SQLDatabase):
    kind, label = "postgres", "PostgreSQL"
    description = "Also works for Supabase, Neon, Railway, Amazon RDS/Aurora, Redshift and TimescaleDB."
    requires = ("sqlalchemy", "psycopg")
    schemes, driver = ("postgresql", "redshift"), "postgresql+psycopg"
    connect_args = {"connect_timeout": 10, "options": "-c statement_timeout=120000"}
    fields = (Field("url", "Connection URL", "postgresql://user:password@host:5432/database", secret=True), QUERY_FIELD)

    def begin(self, conn):
        from sqlalchemy import text
        conn.execute(text("SET TRANSACTION READ ONLY"))


@register
class MySQL(SQLDatabase):
    kind, label = "mysql", "MySQL / MariaDB"
    description = "Also works for PlanetScale, Amazon RDS/Aurora MySQL and Google Cloud SQL."
    requires = ("sqlalchemy", "pymysql")
    schemes, driver = ("mysql", "mariadb"), "mysql+pymysql"
    connect_args = {"connect_timeout": 10, "read_timeout": 120}
    fields = (Field("url", "Connection URL", "mysql://user:password@host:3306/database", secret=True), QUERY_FIELD)


@register
class SQLServer(SQLDatabase):
    kind, label = "sqlserver", "Microsoft SQL Server"
    description = "Also works for Azure SQL Database."
    requires = ("sqlalchemy", "pymssql")
    schemes, driver = ("mssql",), "mssql+pymssql"
    connect_args = {"login_timeout": 10, "timeout": 120}
    fields = (Field("url", "Connection URL", "mssql://user:password@host:1433/database", secret=True), QUERY_FIELD)


@register
class OtherSQL(SQLDatabase):
    kind, label = "sql", "Other SQL database"
    description = ("Snowflake, BigQuery, Oracle, Databricks, DuckDB… any database with a SQLAlchemy driver "
                   "installed on the server. The URL names the driver.")
    fields = (Field("url", "Connection URL", "snowflake://user:password@account/database/schema", secret=True,
                    help="The driver's SQLAlchemy URL."), QUERY_FIELD)


@register
class SQLite(SQLDatabase):
    kind, label = "sqlite", "SQLite file"
    description = "A .sqlite/.db file on the server, inside a folder listed in DATAPULSE_SQLITE_DIR."
    fields = (Field("path", "File path", "/data/shared/sales.db"), QUERY_FIELD)

    def url(self, settings):
        from sqlalchemy.engine import URL

        roots = [Path(d).resolve() for d in os.getenv("DATAPULSE_SQLITE_DIR", "").split(os.pathsep) if d]
        if not roots:
            raise SourceError("SQLite files are off. Set DATAPULSE_SQLITE_DIR on the server to the folder they live in.")
        path = Path(settings["path"]).expanduser().resolve()
        if not any(root in path.parents for root in roots) or not path.is_file():
            raise SourceError(f"No SQLite file at that path inside {', '.join(map(str, roots))}.")
        return URL.create("sqlite", database=f"file:{path}?mode=ro", query={"uri": "true"})
