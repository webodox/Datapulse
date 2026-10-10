import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from datapulse.baseline import build_baseline
from datapulse.config import Config, parse_config


def make_df(n: int = 500, seed: int = 0) -> pd.DataFrame:
    rs = np.random.RandomState(seed)
    return pd.DataFrame({
        "order_id": np.arange(1000, 1000 + n),
        "customer_id": [f"C{i:04d}" for i in rs.randint(0, 120, n)],
        "status": rs.choice(["pending", "shipped", "delivered", "cancelled"], n, p=[0.2, 0.3, 0.4, 0.1]),
        "amount": np.round(rs.lognormal(4, 0.5, n), 2),
        "quantity": rs.randint(1, 6, n),
        "country": rs.choice(["US", "CA", "GB", "DE"], n),
    })


CONFIG_RAW = {
    "version": 1,
    "checks": {
        "null_rate": {"max_increase": 0.02},
        "duplicates": {"columns": ["order_id"]},
    },
    "consumers": {
        "revenue_dashboard": {"type": "dashboard", "columns": ["amount", "country"]},
        "churn_model": {"type": "model", "columns": ["customer_id", "amount", "quantity"], "owner": "ds"},
    },
}


@pytest.fixture
def cfg() -> Config:
    return parse_config(CONFIG_RAW)


@pytest.fixture
def df() -> pd.DataFrame:
    return make_df()


@pytest.fixture
def baseline(tmp_path, df, cfg):
    p = tmp_path / "orders.csv"
    df.to_csv(p, index=False)
    return build_baseline(df, p, 1, cfg.max_categories)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def shop_shape(df: pd.DataFrame) -> pd.DataFrame:
    """The same orders as another system stores them: other names, cents, capitals, an extra column."""
    return pd.DataFrame({"OrderID": df.order_id + 50_000, "Customer ID": df.customer_id, "STATUS": df.status.str.upper(),
                         "amount_cents": (df.amount * 100).round(), "Quantity": df.quantity, "Country": df.country,
                         "Created_At": df.created_at, "warehouse": "W1"})


@pytest.fixture
def sqlite_source(tmp_path, monkeypatch):
    """The example orders in shop_shape, as a SQLite data source."""
    monkeypatch.setenv("DATAPULSE_SQLITE_DIR", str(tmp_path))
    path = tmp_path / "shop.db"
    with sqlite3.connect(path) as db:
        shop_shape(pd.read_csv(Path(__file__).resolve().parents[1] / "examples/data/orders.csv")).to_sql(
            "store_orders", db, index=False)
    return {"path": str(path), "query": "store_orders"}
