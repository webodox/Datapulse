"""The AI data unifier, map path: specs, coercion, and the mapping agent."""
import json

import pytest

from conftest import make_df
from datapulse.config import Config
from datapulse.errors import DataPulseError
from datapulse.unifier import StoredSpec, UnifySpec, apply_spec, coerce, spec_diff, to_json_schema, unify_files
from datapulse.unifier.agent import MAX_VALUE_CHARS, SAMPLE_RECORDS, propose_spec
from datapulse.unifier.heuristic import default_spec, snake_case
from datapulse.unifier.spec import Field, get_path
from fake_claude import FakeClaude, text

SECRET = "ZZ-ROW-VALUE-SECRET"

MAPPING = {
    "description": "Customer orders",
    "fields": [
        {"name": "order_id", "from": "Order #", "type": "integer", "transform": None, "format": None,
         "separator": None, "description": "Order number"},
        {"name": "amount_usd", "from": "Total", "type": "number", "transform": "parse_number", "format": None,
         "separator": None, "description": "Order total in dollars"},
        {"name": "ordered_on", "from": "Date", "type": "date", "transform": "parse_date", "format": "%m/%d/%Y",
         "separator": None, "description": "Order date"},
        {"name": "customer_email", "from": "customer.email", "type": "string", "transform": "lowercase_trim",
         "format": None, "separator": None, "description": "Buyer email"},
    ],
}
RAW = [{"Order #": "1001", "Total": "$1,200.50", "Date": "03/07/2026", "customer": {"email": " Ada@X.com "}},
       {"Order #": "1002", "Total": "(15.00)", "Date": "03/08/2026", "customer": {"email": "bob@x.com"}},
       {"Order #": "1003", "Total": "5 units", "Date": "not a date", "customer": {"email": None}}]


# ---------- spec and coercion ----------

def test_map_spec_applies_types_and_transforms_and_counts_failures():
    spec = UnifySpec.from_dict({**MAPPING, "mode": "map"})
    rows, errors = apply_spec(RAW, spec)
    assert rows[0] == {"order_id": 1001, "amount_usd": 1200.5, "ordered_on": "2026-03-07", "customer_email": "ada@x.com"}
    assert rows[1]["amount_usd"] == -15.0  # accounting negative
    assert rows[2]["amount_usd"] is None and rows[2]["ordered_on"] is None  # never silently "fixed"
    assert errors == {"amount_usd": 1, "ordered_on": 1}
    assert rows[2]["customer_email"] is None  # missing values are null, not errors


@pytest.mark.parametrize("raw,field,expected", [
    ("12.5", Field("n", "integer"), (None, False)),
    ("12", Field("n", "integer"), (12, True)),
    ("Yes", Field("b", "boolean"), (True, True)),
    ("maybe", Field("b", "boolean"), (None, False)),
    ("a; b ;c", Field("tags", "array", transform="split", separator=";"), (["a", "b", "c"], True)),
    ('{"k": 1}', Field("o", "object"), ({"k": 1}, True)),
    ("2026-09-01T10:00:00Z", Field("t", "datetime"), ("2026-09-01 10:00:00+00:00", True)),
    ("2026-09-01 10:00:00", Field("t", "datetime"), ("2026-09-01 10:00:00", True)),
    (3.0, Field("s", "string"), ("3", True)),
    ({"a": 1}, Field("s", "string"), ('{"a": 1}', True)),
    ("", Field("n", "number"), (None, True)),
])
def test_coerce(raw, field, expected):
    assert coerce(raw, field) == expected


def test_paths_resolve_exact_keys_nested_objects_and_list_items():
    rec = {"a.b": 1, "a": {"b": 2, "items": [{"sku": "X"}]}}
    assert get_path(rec, "a.b") == 1  # a CSV header containing a dot wins
    assert get_path(rec, "a.items.0.sku") == "X"
    assert get_path(rec, "a.missing") is None


@pytest.mark.parametrize("bad,needle", [
    ({"fields": [{"name": "Bad Name", "type": "string"}]}, "snake_case"),
    ({"fields": [{"name": "x", "type": "decimal"}]}, "unknown type"),
    ({"fields": [{"name": "x", "type": "string", "transform": "eval"}]}, "unknown transform"),
    ({"fields": [{"name": "x", "type": "string"}, {"name": "x", "type": "integer"}]}, "twice"),
    ({"fields": [{"name": "_document", "type": "string"}]}, "snake_case"),
    ({"fields": [{"name": "tags", "type": "array", "transform": "split"}]}, "separator"),
    ({"mode": "extract", "fields": [{"name": "x", "type": "string"}]}, "record_mode"),
    ({"fields": []}, "at least one"),
])
def test_validation_rejects_bad_specs(bad, needle):
    with pytest.raises(DataPulseError, match=needle):
        UnifySpec.from_dict(bad)


def test_heuristic_spec_names_types_and_identifiers():
    recs = [{"Order ID": "007", "Amount": "1.5", "When": "2026-01-01 00:00:00", "Paid": "true",
             "Customer": {"firstName": "Ada"}, "count": 3}]
    spec = default_spec(recs)
    types = {f.name: f.type for f in spec.fields}
    assert types == {"order_id": "string", "amount": "number", "when": "datetime", "paid": "boolean",
                     "customer_first_name": "string", "count": "integer"}  # "007" keeps its leading zeros
    assert snake_case("2nd Place") == "f_2nd_place"


def test_json_schema_and_diff():
    old = UnifySpec.from_dict({**MAPPING, "mode": "map"})
    new_fields = [dict(f) for f in MAPPING["fields"][:3]] + [{"name": "coupon", "type": "string", "from": "Coupon"}]
    new_fields[1]["from"] = "Grand Total"
    new = UnifySpec.from_dict({"mode": "map", "fields": new_fields})
    diff = spec_diff(old, new)
    assert diff["added"] == ["coupon"] and diff["removed"] == ["customer_email"]
    assert diff["remapped"] == [{"name": "amount_usd", "from_before": "Total", "from_now": "Grand Total"}]
    schema = to_json_schema(old)
    assert schema["properties"]["ordered_on"] == {"type": ["string", "null"], "format": "date",
                                                  "description": "Order date"}
    assert spec_diff(old, old) is None


# ---------- the mapping agent ----------

def test_propose_spec_sends_a_bounded_sample_and_self_corrects():
    records = [{"Order #": str(i), "Total": f"${i}.00", "Notes": SECRET + "x" * 500} for i in range(400)]
    bad = dict(MAPPING, fields=[{**MAPPING["fields"][0], "type": "money"}])
    good = {"description": "Orders", "fields": MAPPING["fields"][:2]}
    fake = FakeClaude([text(json.dumps(bad)), text(json.dumps(good))])
    previous = UnifySpec.from_dict({"mode": "map", "fields": [{"name": "order_id", "type": "integer", "from": "id"}]})
    spec = propose_spec(records, previous, Config(), client=fake)
    assert [f.name for f in spec.fields] == ["order_id", "amount_usd"] and spec.mode == "map"
    sent = json.loads(fake.calls[0]["messages"][0]["content"])
    assert len(sent["sample_records"]) == SAMPLE_RECORDS and sent["total_records"] == 400
    assert all(len(r["Notes"]) <= MAX_VALUE_CHARS + 1 for r in sent["sample_records"])
    assert sent["previous_mapping"]["fields"][0]["from"] == "id"
    assert "unknown type" in fake.calls[1]["messages"][-1]["content"]  # the validation error went back
    assert fake.calls[0]["output_config"]["format"]["schema"]["required"] == ["description", "fields"]


def test_propose_spec_gives_up_after_repeated_invalid_output():
    bad = text(json.dumps({"description": "x", "fields": []}))
    with pytest.raises(DataPulseError, match="valid mapping"):
        propose_spec(RAW, None, Config(), client=FakeClaude([bad, bad]))


# ---------- pipeline ----------

def _csv(tmp_path, df, name="orders.csv"):
    p = tmp_path / name
    df.to_csv(p, index=False)
    return p


def test_pipeline_reuses_the_spec_until_the_shape_changes(tmp_path):
    df = make_df(30)
    first = unify_files([(_csv(tmp_path, df), "orders.csv")], None, Config(), use_ai=False)
    assert first.origin == "heuristic" and not first.reused and len(first.rows) == 30
    prev = StoredSpec(first.spec, first.origin, first.fingerprint)
    again = unify_files([(_csv(tmp_path, make_df(30, seed=3)), "orders.csv")], prev, Config(), use_ai=False)
    assert again.reused and again.changed is None
    shifted = unify_files([(_csv(tmp_path, df.assign(coupon="X")), "orders.csv")], prev, Config(), use_ai=False)
    assert not shifted.reused and shifted.changed["added"] == ["coupon"]


def test_pipeline_rejects_mixed_uploads(tmp_path):
    (tmp_path / "notes.txt").write_text("hi")
    with pytest.raises(DataPulseError, match="separately"):
        unify_files([(_csv(tmp_path, make_df(5)), "o.csv"), (tmp_path / "notes.txt", "notes.txt")], None, Config())


def test_pipeline_falls_back_when_the_ai_is_unavailable(tmp_path, monkeypatch):
    def broken(*a, **k):
        raise DataPulseError("Could not reach the AI service")
    monkeypatch.setattr("datapulse.unifier.pipeline.propose_spec", broken)
    out = unify_files([(_csv(tmp_path, make_df(10)), "o.csv")], None, Config(), use_ai=True)
    assert out.origin == "heuristic" and "AI mapping unavailable" in out.stats["note"]
