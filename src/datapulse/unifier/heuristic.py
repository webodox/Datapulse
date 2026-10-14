"""Rule-based specs, used when no AI key is configured (and as the AI's fallback)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List

from .spec import Field, UnifySpec, get_path, record_paths

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_BOOL = {"true", "false"}


def snake_case(text: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(text))
    s = re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower()
    if not s:
        s = "field"
    if not s[0].isalpha():
        s = f"f_{s}"
    return s[:64]


def _infer(values: List[Any]) -> str:
    present = [v for v in values if v is not None and not (isinstance(v, str) and not v.strip())]
    if not present:
        return "string"
    if all(isinstance(v, bool) for v in present):
        return "boolean"
    if all(isinstance(v, list) for v in present):
        return "array"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        return "integer" if all(float(v).is_integer() and isinstance(v, int) for v in present) else "number"
    if not all(isinstance(v, str) for v in present):
        return "string"
    texts = [v.strip() for v in present]
    if all(t.lower() in _BOOL for t in texts):
        return "boolean"
    # Leading zeros (zip codes, account numbers) are identifiers, not numbers.
    if any(len(t) > 1 and t.lstrip("+-").startswith("0") and not t.lstrip("+-").startswith("0.") for t in texts):
        return "string"
    if all(_INT.match(t) for t in texts):
        return "integer"
    if all(_INT.match(t) or _FLOAT.match(t) for t in texts):
        return "number"
    if all(_DATE.match(t) for t in texts):
        return "date"
    if all(_DATETIME.match(t) or _DATE.match(t) for t in texts):
        try:
            for t in texts[:50]:
                datetime.fromisoformat(t.replace("Z", "+00:00"))
            return "datetime"
        except ValueError:
            return "string"
    return "string"


def default_spec(records: List[Dict[str, Any]], description: str = "") -> UnifySpec:
    """snake_case names, nested paths flattened, types inferred from up to 2,000 records."""
    sample = records[:2000]
    used: Dict[str, int] = {}
    fields = []
    for path in record_paths(sample):
        name = snake_case(path)
        if name.startswith("_"):
            name = f"f{name}"
        if name in used:
            used[name] += 1
            name = f"{name}_{used[name]}"
        else:
            used[name] = 1
        fields.append(Field(name=name, type=_infer([get_path(r, path) for r in sample]), source=path))
    if not fields:
        fields = [Field(name="value", type="string", source="value")]
    return UnifySpec(mode="map", description=description or "Records with types inferred automatically", fields=fields)


def default_document_spec() -> UnifySpec:
    """One record per document with its text, when no AI is available to extract fields."""
    return UnifySpec(
        mode="extract", record_mode="one_per_document",
        description="Document text (no AI key configured, so fields were not extracted)",
        record_description="One record per document",
        fields=[Field(name="title", type="string", description="First line of the document, or the email subject"),
                Field(name="text", type="string", description="Full text of the document"),
                Field(name="word_count", type="integer", description="Number of words in the text")])
