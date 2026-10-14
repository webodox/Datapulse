"""Data-source plug-ins and the unify engine."""
import json
import os
from pathlib import Path

import pytest

from datapulse import sources
from datapulse.baseline import build_baseline
from datapulse.config import load_config
from datapulse.io import load_dataframe
from datapulse.sources.link import download_url, read_bytes
from datapulse.sources.mongodb import MongoDB
from datapulse.sources.sql import select_sql
from datapulse.unify import SOURCE_COLUMN, apply_recipe, combine, name_matches, propose_mapping, validate_recipe
from conftest import shop_shape
from fake_claude import FakeClaude, text, tool_calls

EX = Path(__file__).resolve().parents[1] / "examples"
GOOD = load_dataframe(EX / "data/orders.csv")


# ---------- the plug-in contract ----------

def test_catalog_lists_every_plugin_with_its_form():
    kinds = {c["kind"]: c for c in sources.catalog()}
    visible = [c["kind"] for c in sources.catalog() if not c["hidden"]]
    assert visible == ["database", "file", "google_drive", "link", "rest", "web_page"]
    assert kinds["file"]["has_sample"] and kinds["link"]["has_sample"] and kinds["rest"]["has_sample"]
    assert kinds["web_page"]["has_sample"] and not kinds["database"]["has_sample"]
    assert {"postgres", "mysql", "sqlserver", "sql", "sqlite", "mongodb", "link"} <= set(kinds)
    assert [c["group"] for c in sources.catalog()][0] == "Databases"
    url = next(f for f in kinds["postgres"]["fields"] if f["name"] == "url")
    assert url["secret"] and url["placeholder"].startswith("postgresql://")


def test_settings_validation_keeps_saved_secrets_and_never_shows_them():
    saved = sources.clean_settings("postgres", {"url": "postgresql://u:pw@h/db", "query": "orders"})
    edited = sources.clean_settings("postgres", {"url": "", "query": "sales"}, previous=saved)
    assert edited == {"url": "postgresql://u:pw@h/db", "query": "sales"}
    assert sources.public_settings("postgres", edited) == {"url": "", "query": "sales", "saved_secrets": ["url"]}
    with pytest.raises(sources.SourceError, match="Connection URL is required"):
        sources.clean_settings("postgres", {"query": "orders"})
    with pytest.raises(sources.SourceError, match="Unknown setting"):
        sources.clean_settings("postgres", {"url": "x", "query": "y", "host": "z"})


def test_connection_errors_never_contain_the_password():
    with pytest.raises(sources.SourceError) as err:
        sources.try_connection("postgres", {"url": "postgresql://ada:Sup3r-Secret@127.0.0.1:1/db", "query": "t"})
    assert "Sup3r-Secret" not in str(err.value) and str(err.value).startswith("PostgreSQL:")
    with pytest.raises(sources.SourceError, match="start with postgresql://"):
        sources.try_connection("postgres", {"url": "mysql://a:b@h/db", "query": "t"})


def test_queries_are_select_only():
    quote = lambda s: f'"{s}"'  # noqa: E731
    assert select_sql("public.orders;", quote) == 'SELECT * FROM "public"."orders"'
    assert select_sql("  SELECT * FROM t WHERE x = 1 ", quote) == "SELECT * FROM t WHERE x = 1"
    for bad in ["DELETE FROM t", "SELECT 1; DROP TABLE t", "/* hi */ UPDATE t SET x = 1", "orders o; drop"]:
        with pytest.raises(sources.SourceError):
            select_sql(bad, quote)


def test_sqlite_source_reads_a_table_and_stays_in_its_folder(sqlite_source, tmp_path):
    assert "8 column(s)" in sources.try_connection("sqlite", sqlite_source)
    df = sources.fetch_source("sqlite", {**sqlite_source, "query": "SELECT * FROM store_orders WHERE Country = 'US'"})
    assert len(df) == (GOOD.country == "US").sum() and "amount_cents" in df.columns
    outside = tmp_path.parent / "elsewhere.db"
    with pytest.raises(sources.SourceError, match="No SQLite file"):
        sources.fetch_source("sqlite", {"path": str(outside), "query": "t"})


def test_row_limit(sqlite_source, monkeypatch):
    monkeypatch.setenv("DATAPULSE_MAX_SOURCE_ROWS", "100")
    with pytest.raises(sources.SourceError, match="more than 100 rows"):
        sources.fetch_source("sqlite", sqlite_source, "Shop")


def test_mongodb_documents_are_flattened(monkeypatch):
    mongomock = pytest.importorskip("mongomock")
    client = mongomock.MongoClient()
    client.shop.orders.insert_many([
        {"order_id": 1, "customer": {"id": "C1", "address": {"country": "US"}}, "items": [1, 2], "status": "paid"},
        {"order_id": 2, "customer": {"id": "C2", "address": {"country": "DE"}}, "items": [], "status": "test"},
    ])
    monkeypatch.setattr(MongoDB, "client", lambda self, settings: client)
    monkeypatch.setattr(client, "close", lambda: None)
    settings = {"url": "mongodb://x", "database": "shop", "collection": "orders", "filter": '{"status": {"$ne": "test"}}'}
    assert "about 2 documents" in sources.try_connection("mongodb", settings)
    df = sources.fetch_source("mongodb", settings)
    assert len(df) == 1 and df.loc[0, "customer.address.country"] == "US"
    assert isinstance(df.loc[0, "_id"], str) and df.loc[0, "items"] == "[1, 2]"
    with pytest.raises(sources.SourceError, match="no collection 'nope'"):
        sources.try_connection("mongodb", {**settings, "collection": "nope"})


def test_link_source_handles_sheets_and_refuses_internal_addresses():
    sheet = "https://docs.google.com/spreadsheets/d/AbC-123/edit#gid=42"
    assert download_url(sheet) == "https://docs.google.com/spreadsheets/d/AbC-123/export?format=csv&gid=42"
    doc = "https://docs.google.com/document/d/DOCID99/edit"
    assert download_url(doc) == "https://docs.google.com/document/d/DOCID99/export?format=docx"
    assert len(read_bytes(b"a,b\n1,2\n", "https://x/data", "text/csv")) == 1
    assert read_bytes(b'[{"a": 1}]', "https://x/d.json", "")["a"].tolist() == [1]
    for url in ["https://127.0.0.1/x.csv", "https://169.254.169.254/latest", "http://example.com/x.csv"]:
        with pytest.raises(sources.SourceError):
            sources.fetch_source("link", {"url": url})


@pytest.mark.skipif(not os.getenv("DATAPULSE_TEST_POSTGRES_URL"), reason="needs DATAPULSE_TEST_POSTGRES_URL")
def test_real_postgres():
    from sqlalchemy import create_engine
    url = os.environ["DATAPULSE_TEST_POSTGRES_URL"]
    shop_shape(GOOD).to_sql("dp_test_orders", create_engine(url.replace("postgresql://", "postgresql+psycopg://")),
                            if_exists="replace", index=False)
    assert len(sources.fetch_source("postgres", {"url": url, "query": "dp_test_orders"})) == len(GOOD)
    with pytest.raises(sources.SourceError, match="read-only"):
        sources.fetch_source("postgres", {"url": url, "query": "WITH d AS (DELETE FROM dp_test_orders RETURNING *) SELECT * FROM d"})


# ---------- unify ----------

@pytest.fixture
def standard():
    cfg = load_config(EX / "datapulse.yml")
    return cfg, build_baseline(GOOD, EX / "data/orders.csv", 1, cfg.max_categories)


def test_names_are_matched_without_ai():
    ops = name_matches(["OrderID", "Customer ID", "amount_cents", "status"], ["order_id", "customer_id", "amount", "status"])
    assert [(o["args"]["column"], o["args"]["new_name"]) for o in ops] == [("OrderID", "order_id"), ("Customer ID", "customer_id")]


def test_recipes_replay_and_report_steps_that_no_longer_apply(standard):
    _, baseline = standard
    recipe = validate_recipe([{"operation": "rename_column", "args": {"column": "amount_cents", "new_name": "amount"}},
                              {"operation": "scale_column", "args": {"column": "amount", "factor": 0.01}}])
    df, effects, problems = apply_recipe(shop_shape(GOOD), baseline["profile"], recipe)
    assert abs(df["amount"].mean() - GOOD["amount"].mean()) < 0.01 and not problems and len(effects) == 2
    _, _, problems = apply_recipe(GOOD, baseline["profile"], recipe)  # the source changed shape
    assert problems[0].startswith("Step 1 (rename column) no longer applies")
    with pytest.raises(ValueError):
        validate_recipe([{"operation": "exec", "args": {}}])


def test_ai_mapping_uses_statistics_only(standard):
    cfg, baseline = standard
    shop = shop_shape(GOOD)
    shop.loc[:3, "STATUS"] = "ZZ-SECRET"
    summary = {"summary": "Mapped the shop's orders.", "not_fixed": [], "warnings": []}
    fake = FakeClaude([
        tool_calls(("get_column_stats", {"column": "amount_cents"}), ("get_column_stats", {"column": "amount"})),
        tool_calls(("rename_column", {"column": "amount_cents", "new_name": "amount", "reason": "same range x100"})),
        tool_calls(("scale_column", {"column": "amount", "factor": 0.01, "reason": "cents"}),
                   ("standardize_categories", {"column": "status", "reason": "capitals"}),
                   ("drop_column", {"column": "warehouse", "reason": "not in the standard"}),
                   ("fill_missing", {"column": "status", "strategy": "most_common", "reason": "not allowed here"})),
        text(json.dumps(summary)),
    ])
    proposal = propose_mapping(shop, baseline, cfg, client=fake)
    names = [o["operation"] for o in proposal["operations"]]
    assert proposal["automatic_steps"] == 6 and names[:6] == ["rename_column"] * 6
    assert names[6:] == ["rename_column", "scale_column", "standardize_categories", "drop_column"]
    assert proposal["after"]["total"] < proposal["before"]["total"]
    assert '"amount_cents"' in fake.calls[0]["messages"][0]["content"]  # both column lists are in the brief
    errors = [r for r in fake.calls[3]["messages"][-1]["content"] if r["is_error"]]
    assert "not available" in errors[0]["content"]  # repair-only tools are refused while mapping
    assert "ZZ-SECRET" not in fake.sent() and "C0102" not in fake.sent()


def test_compare_shows_each_standard_column_with_its_source_and_rule_results(standard):
    from datapulse.compare import compare_mapping

    cfg, baseline = standard
    shop = shop_shape(GOOD)
    recipe = name_matches(list(shop.columns), list(GOOD.columns)) + [
        {"operation": "rename_column", "args": {"column": "amount_cents", "new_name": "amount"}},
        {"operation": "scale_column", "args": {"column": "amount", "factor": 0.01}},
        {"operation": "drop_column", "args": {"column": "warehouse"}},
        {"operation": "rename_column", "args": {"column": "gone", "new_name": "x"}},
    ]
    out = compare_mapping(shop, baseline, cfg, recipe)
    columns = {c["name"]: c for c in out["columns"]}
    assert list(columns) == list(GOOD.columns)
    assert columns["order_id"]["source"] == "OrderID" and columns["order_id"]["status"] == "renamed"
    assert columns["amount"]["status"] == "converted"
    assert [s["operation"] for s in columns["amount"]["steps"]] == ["rename_column", "scale_column"]
    assert columns["status"]["status"] == "flagged" and columns["status"]["issues"][0]["severity"] == "warning"
    assert columns["status"]["after"]["samples"] == shop["STATUS"].head(3).tolist()
    assert out["dropped"] == [{"name": "warehouse", "effect": "Removed the column 'warehouse'."}]
    assert out["problems"][0].startswith("Step 10 (rename column) no longer applies")
    assert out["after"]["total"] < out["before"]["total"]

    cfg.report.show_values = False
    hidden = compare_mapping(shop, baseline, cfg, [])
    assert all(c["status"] == "missing" for c in hidden["columns"]) and len(hidden["extra"]) == len(shop.columns)
    assert not any(e["after"]["samples"] for e in hidden["extra"]) and not hidden["show_values"]


def test_combine_tags_rows_with_their_source():
    out = combine({"A": GOOD.head(2), "B": GOOD.head(3).drop(columns=["country"])}, list(GOOD.columns))
    assert out[SOURCE_COLUMN].tolist() == ["A", "A", "B", "B", "B"] and out["country"].isna().sum() == 3


# ---------- one "Database" source for every database ----------

def test_database_type_is_detected_from_the_url():
    from datapulse.sources.database import detect, type_label
    cases = {"postgresql://u:p@db.supabase.co:5432/postgres": "postgres", "postgres://u:p@h/d": "postgres",
             "mysql+pymysql://u:p@h/d": "mysql", "mariadb://h/d": "mysql", "mssql://u:p@h/d": "sqlserver",
             "mongodb+srv://u:p@cluster.mongodb.net/": "mongodb", "snowflake://u:p@acct/db": "sql"}
    assert {url: detect(url) for url in cases} == cases
    assert type_label("mongodb://x") == "MongoDB" and type_label("snowflake://x") == "SQL database"
    for bad in ["just text", "sqlite:///etc/passwd"]:
        with pytest.raises(sources.SourceError):
            detect(bad)


def test_database_source_reads_mongodb_with_database_dot_collection(monkeypatch):
    mongomock = pytest.importorskip("mongomock")
    client = mongomock.MongoClient()
    client.shop.orders.insert_many([{"order_id": 1, "customer": {"country": "US"}}, {"order_id": 2, "customer": {"country": "DE"}}])
    monkeypatch.setattr(MongoDB, "client", lambda self, settings: client)
    monkeypatch.setattr(client, "close", lambda: None)
    settings = {"url": "mongodb://user:pw@host/", "query": "shop.orders"}
    assert sources.try_connection("database", settings).startswith("MongoDB: Connected")
    assert sources.fetch_source("database", settings)["customer.country"].tolist() == ["US", "DE"]
    assert sources.type_of("database", settings) == "MongoDB"


def test_database_errors_name_the_detected_type_and_hide_the_password():
    with pytest.raises(sources.SourceError) as err:
        sources.try_connection("database", {"url": "postgresql://ada:Sup3r-Secret@127.0.0.1:1/db", "query": "t"})
    assert str(err.value).startswith("PostgreSQL:") and "Sup3r-Secret" not in str(err.value)


@pytest.mark.skipif(not os.getenv("DATAPULSE_TEST_POSTGRES_URL"), reason="needs DATAPULSE_TEST_POSTGRES_URL")
def test_database_source_on_real_postgres():
    from sqlalchemy import create_engine
    url = os.environ["DATAPULSE_TEST_POSTGRES_URL"]
    shop_shape(GOOD).to_sql("dp_test_db_source", create_engine(url.replace("postgresql://", "postgresql+psycopg://")),
                            if_exists="replace", index=False)
    assert sources.try_connection("database", {"url": url, "query": "dp_test_db_source"}).startswith("PostgreSQL: Connected")
    assert len(sources.fetch_source("database", {"url": url, "query": "dp_test_db_source"})) == len(GOOD)
