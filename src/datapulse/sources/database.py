"""One "Database" source for every database: paste a connection URL and the right driver is picked from it.

PostgreSQL (and Supabase, Neon, Railway, RDS, Redshift), MySQL/MariaDB, SQL Server, MongoDB (and Atlas,
DocumentDB, Cosmos DB), and anything else with a SQLAlchemy driver (Snowflake, BigQuery, Oracle, Databricks...).
"""
from __future__ import annotations

from typing import Dict, Tuple

from .base import REGISTRY, Field, Source, SourceError, error_text, plugin, register

# URL scheme (before any "+driver") -> plug-in that reads it
SCHEMES = {
    "mongodb": "mongodb",
    "postgres": "postgres", "postgresql": "postgres", "redshift": "postgres", "timescaledb": "postgres",
    "mysql": "mysql", "mariadb": "mysql",
    "mssql": "sqlserver", "sqlserver": "sqlserver",
}


def detect(url: str) -> str:
    """The plug-in kind for a connection URL: one of SCHEMES' values, or "sql" for any other SQLAlchemy URL."""
    scheme = url.strip().split("://", 1)[0].lower() if "://" in url else ""
    if not scheme:
        raise SourceError("Paste a connection URL, like postgresql://user:password@host:5432/database")
    if scheme.split("+")[0] == "sqlite":
        raise SourceError("SQLite files can't be reached from the server; upload the data as a File instead.")
    return SCHEMES.get(scheme.split("+")[0], "sql")


def type_label(url: str) -> str:
    """What the URL points at, for display: "PostgreSQL", "MongoDB"... ("Database" if it can't be told)."""
    try:
        kind = detect(url)
    except SourceError:
        return "Database"
    return "SQL database" if kind == "sql" else REGISTRY[kind].label


@register
class Database(Source):
    kind, label, group = "database", "Database", "Databases"
    description = "Any database: PostgreSQL, Supabase, MySQL, SQL Server, MongoDB, Snowflake, BigQuery, Oracle…"
    fields = (
        Field("url", "Connection URL", "postgresql://user:password@host:5432/database", secret=True,
              help="Paste it from your database's dashboard. DataPulse works out the type from it."),
        Field("query", "Table, query or collection", "orders", multiline=True,
              help="A table name or one SELECT query. For MongoDB: a collection, or database.collection."),
    )

    def target(self, settings: Dict[str, str]) -> Tuple[Source, Dict[str, str]]:
        kind = detect(settings["url"])
        found = plugin(kind)  # explains which package is missing, if one is
        if kind == "mongodb":
            database, _, collection = settings["query"].strip().rpartition(".")
            return found, {"url": settings["url"], "database": database, "collection": collection}
        return found, {"url": settings["url"], "query": settings["query"]}

    def run(self, settings, action):
        """Run a step on the detected plug-in; driver errors are labelled with the detected type."""
        found, inner = self.target(settings)
        try:
            return action(found, inner)
        except SourceError:
            raise
        except Exception as e:
            raise SourceError(f"{type_label(settings['url'])}: {error_text(e)}") from None

    def test(self, settings):
        return f"{type_label(settings['url'])}: {self.run(settings, lambda found, inner: found.test(inner))}"

    def fetch(self, settings, max_rows):
        return self.run(settings, lambda found, inner: found.fetch(inner, max_rows))
