"""MongoDB (and MongoDB Atlas, Amazon DocumentDB, Azure Cosmos DB for MongoDB): documents flattened into columns."""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any, Dict

from .base import Field, Source, SourceError, flatten, register


def plain(value: Any) -> Any:
    """BSON values as plain Python: ObjectId -> text, Decimal128 -> float, nested documents kept for flattening."""
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool, dt.datetime)):
        return value
    if isinstance(value, Decimal) or type(value).__name__ == "Decimal128":
        return float(str(value))
    return str(value)  # ObjectId, UUID, binary, timestamps...


@register
class MongoDB(Source):
    kind, label, group = "mongodb", "MongoDB", "Databases"
    description = ("Also works for MongoDB Atlas, Amazon DocumentDB and Azure Cosmos DB. Nested fields become "
                   "columns like address.city; lists are kept as text.")
    requires = ("pymongo",)
    hidden = True  # offered through the single "Database" source
    fields = (
        Field("url", "Connection URL", "mongodb+srv://user:password@cluster.mongodb.net/", secret=True),
        Field("database", "Database", "shop", required=False, help="Leave empty to use the one in the URL."),
        Field("collection", "Collection", "orders"),
        Field("filter", "Filter (optional)", '{"status": {"$ne": "test"}}', required=False, multiline=True,
              help="A MongoDB query as JSON, to pick which documents to check."),
    )

    def client(self, settings):
        from pymongo import MongoClient

        return MongoClient(settings["url"], serverSelectionTimeoutMS=10_000, connectTimeoutMS=10_000,
                           socketTimeoutMS=120_000)

    def database(self, client, settings):
        if settings.get("database"):
            return client[settings["database"]]
        try:
            return client.get_default_database()
        except Exception:
            raise SourceError("Name the database (the connection URL doesn't include one).")

    def test(self, settings):
        client = self.client(settings)
        try:
            client.admin.command("ping")
            db = self.database(client, settings)
            names = db.list_collection_names()
            if settings["collection"] not in names:
                raise SourceError(f"Connected, but database '{db.name}' has no collection '{settings['collection']}'. "
                                  f"Collections: {', '.join(sorted(names)[:10]) or 'none'}")
            count = db[settings["collection"]].estimated_document_count()
        finally:
            client.close()
        return f"Connected. '{settings['collection']}' has about {count:,} documents."

    def fetch(self, settings, max_rows):
        query: Dict[str, Any] = {}
        if settings.get("filter"):
            try:
                query = json.loads(settings["filter"])
            except ValueError as e:
                raise SourceError(f"The filter isn't valid JSON: {e}")
            if not isinstance(query, dict):
                raise SourceError("The filter must be a JSON object, like {\"status\": \"paid\"}.")
        client = self.client(settings)
        try:
            docs = list(self.database(client, settings)[settings["collection"]].find(query, limit=max_rows + 1))
        finally:
            client.close()
        if not docs:
            raise SourceError("No documents matched.")
        return flatten([plain(d) for d in docs])
