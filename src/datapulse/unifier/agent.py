"""The AI unifier for tabular and nested data: reads sample records and writes a map spec.

Unlike the triage agent, this one sees real values - up to SAMPLE_RECORDS records, each string cut to
MAX_VALUE_CHARS - because it has to understand what the fields mean. Code then applies the spec to every row.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from datapulse.ai import create, get_client, json_format, text_of
from datapulse.config import Config
from datapulse.errors import DataPulseError

from .spec import TRANSFORMS, TYPES, UnifySpec, record_paths

SAMPLE_RECORDS = 25
MAX_VALUE_CHARS = 200

SYSTEM = f"""You are the data unifier for a data platform. Records arrive from databases, spreadsheets, \
Google Sheets, CSV/Excel/Parquet files and JSON APIs, often messy. Write a mapping that turns every record \
into clean, well-typed JSON with clear field names. Code applies your mapping to every row, so you only describe it.

For each output field give:
- name: snake_case. Keep an original name that is already clear snake_case unchanged (users write rules \
against these names); only rename unclear or messy ones ("Order #" -> "order_id", "Cust Email" -> "customer_email").
- from: the exact key or dotted path in the raw record ("customer.email", "items.0.sku"). Copy keys exactly.
- type: one of {", ".join(TYPES)}. Pick the most precise type the values support.
- transform: null or one of {", ".join(TRANSFORMS)}. Use parse_number for "$1,200.50" or "12%", \
parse_date/parse_datetime with a strptime `format` for non-ISO dates such as "03/07/2026", split with a \
`separator` for delimited lists, lowercase_trim for emails and codes. Never use a transform to hide bad \
data: "5 units" is not a number.
- description: one plain-English sentence about what the field means.

Include every meaningful raw field, and nothing that is not in the data. Unpack nested objects into separate \
fields when their parts mean different things. If a previous mapping is given, the source changed shape: keep \
every existing field name and type that still applies, map moved fields to their new location, add new fields, \
and drop fields that disappeared. Also write a one-sentence description of the dataset."""

_NULLABLE = lambda t: {"type": [t, "null"]}
SPEC_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["description", "fields"],
    "properties": {
        "description": {"type": "string"},
        "fields": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "from", "type", "transform", "format", "separator", "description"],
            "properties": {
                "name": {"type": "string"}, "from": {"type": "string"},
                "type": {"type": "string", "enum": list(TYPES)},
                "transform": {"anyOf": [{"type": "string", "enum": list(TRANSFORMS)}, {"type": "null"}]},
                "format": _NULLABLE("string"), "separator": _NULLABLE("string"),
                "description": {"type": "string"}}}},
    },
}


def _truncate(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_VALUE_CHARS else value[:MAX_VALUE_CHARS] + "…"
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v) for v in value[:10]]
    return value


def sample_records(records: List[Dict[str, Any]], n: int = SAMPLE_RECORDS) -> List[Dict[str, Any]]:
    """Evenly spaced records, so the sample is not just the first rows of a sorted export."""
    if len(records) <= n:
        picked = records
    else:
        step = len(records) / n
        picked = [records[int(i * step)] for i in range(n)]
    return [_truncate(r) for r in picked]


def propose_spec(records: List[Dict[str, Any]], previous: Optional[UnifySpec], cfg: Config,
                 client=None, attempts: int = 2, source_label: str = "") -> UnifySpec:
    """Ask the AI for a map spec. Invalid output goes back to the model once before giving up."""
    client = client or get_client(cfg)
    context = {
        "source": source_label or "unknown",
        "total_records": len(records),
        "raw_field_paths": record_paths(records)[:300],
        "sample_records": sample_records(records),
        "previous_mapping": previous.to_dict() if previous else None,
    }
    messages: List[Dict[str, Any]] = [{"role": "user", "content": json.dumps(context, default=str)}]
    error = ""
    for _ in range(attempts):
        response = create(client, cfg, system=SYSTEM, output_config=json_format(SPEC_SCHEMA), messages=messages)
        try:
            draft = json.loads(text_of(response))
            return UnifySpec.from_dict({**draft, "mode": "map"})
        except (ValueError, KeyError, TypeError, DataPulseError) as e:
            error = str(e)
            messages += [{"role": "assistant", "content": response.content},
                         {"role": "user", "content": f"That mapping is invalid: {error}\nReturn a corrected mapping."}]
    raise DataPulseError(f"The AI could not produce a valid mapping ({error}).")
