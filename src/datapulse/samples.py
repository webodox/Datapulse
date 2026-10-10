"""Sample sources for demos: the same orders stored by three systems, each in its own format."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Callable, Dict, List, Tuple

import pandas as pd


def warehouse(df: pd.DataFrame) -> pd.DataFrame:
    """The standard: the data warehouse's own format."""
    return df


def store_system(df: pd.DataFrame) -> pd.DataFrame:
    """A point-of-sale database: other column names, amounts in cents, upper-case statuses, an extra column."""
    return pd.DataFrame({"OrderID": df.order_id + 50_000, "Customer ID": df.customer_id, "STATUS": df.status.str.upper(),
                         "amount_cents": (df.amount * 100).round(), "Quantity": df.quantity, "Country": df.country,
                         "Created_At": df.created_at, "warehouse": "W1"})


def online_shop(df: pd.DataFrame) -> pd.DataFrame:
    """A web shop: different names throughout, prices as text like "$61.31", title-case statuses."""
    return pd.DataFrame({"order_number": df.order_id + 90_000, "customer": df.customer_id, "state": df.status.str.title(),
                         "total": df.amount.map(lambda v: f"${v:,.2f}"), "qty": df.quantity, "country_code": df.country,
                         "ordered_at": df.created_at})


# (source name, file, table, shape)
SOURCES: List[Tuple[str, str, str, Callable[[pd.DataFrame], pd.DataFrame]]] = [
    ("Warehouse database", "warehouse.db", "orders", warehouse),
    ("Store system", "store_system.db", "store_orders", store_system),
    ("Online shop", "online_shop.db", "web_orders", online_shop),
]


def write_samples(orders: pd.DataFrame, folder: Path) -> List[Dict[str, str]]:
    """Write each sample source as a SQLite database; returns name + settings for each."""
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for name, file, table, shape in SOURCES:
        path = folder / file
        with closing(sqlite3.connect(path)) as db, db:
            shape(orders).to_sql(table, db, index=False, if_exists="replace")
        out.append({"name": name, "path": str(path), "query": table})
    return out
