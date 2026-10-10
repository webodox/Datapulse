"""Regenerate the sample datasets: python examples/generate_samples.py"""
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).parent / "data"


def build(n: int = 500, seed: int = 42) -> pd.DataFrame:
    rs = np.random.RandomState(seed)
    return pd.DataFrame({
        "order_id": np.arange(1000, 1000 + n),
        "customer_id": [f"C{i:04d}" for i in rs.randint(0, 120, n)],
        "status": rs.choice(["pending", "shipped", "delivered", "cancelled"], n, p=[0.2, 0.3, 0.4, 0.1]),
        "amount": np.round(rs.lognormal(4, 0.5, n), 2),
        "quantity": rs.randint(1, 6, n),
        "country": rs.choice(["US", "CA", "GB", "DE"], n),
        "created_at": pd.date_range("2026-09-01", periods=n, freq="30min").strftime("%Y-%m-%d %H:%M:%S"),
    })


def drift(df: pd.DataFrame, seed: int = 7) -> pd.DataFrame:
    rs = np.random.RandomState(seed)
    d = df.iloc[:380].copy()
    d = d.drop(columns=["country"])                                  # missing column
    d["coupon_code"] = rs.choice(["SAVE10", "WELCOME"], len(d))      # added column
    d["quantity"] = d["quantity"].astype(str) + " units"             # type change
    d.loc[d.sample(frac=0.35, random_state=1).index, "customer_id"] = np.nan  # null spike
    d["amount"] = (d["amount"] * 100).round(0)                       # unit change (dollars -> cents)
    d.loc[d.sample(frac=0.08, random_state=2).index, "status"] = "refunded_pending"  # new category
    dup = d.sample(10, random_state=3).index
    d.loc[dup, "order_id"] = d["order_id"].iloc[0]                   # duplicate keys
    return d


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    base = build()
    base.to_csv(OUT / "orders.csv", index=False)
    drift(base).to_csv(OUT / "orders_drifted.csv", index=False)
    print("wrote", OUT / "orders.csv", OUT / "orders_drifted.csv")
