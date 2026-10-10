"""The unify spec: how raw records from one source become clean, typed JSON rows.

`map` specs say where each output field comes from in a raw record and how to convert it; code applies them
to every row. `extract` specs list the fields an AI pulls out of each document. Both share types and coercion.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from datapulse.errors import DataPulseError

TYPES = ("string", "integer", "number", "boolean", "date", "datetime", "object", "array")
TRANSFORMS = ("trim", "lowercase_trim", "parse_number", "parse_bool", "parse_date", "parse_datetime",
              "split", "json_string")
MODES = ("map", "extract")
RECORD_MODES = ("one_per_document", "many_per_document")
PROVENANCE = ("_document", "_page", "_confidence")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TRUE = {"true", "t", "yes", "y", "1"}
_FALSE = {"false", "f", "no", "n", "0"}
_NUMBER_JUNK = re.compile(r"[\s$€£¥₹,%]")


@dataclass
class Field:
    name: str
    type: str = "string"
    description: str = ""
    source: Optional[str] = None  # "from" in JSON: a key or dotted path into the raw record
    transform: Optional[str] = None
    format: Optional[str] = None  # strptime format for parse_date / parse_datetime
    separator: Optional[str] = None  # for split

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["from"] = d.pop("source")
        return {k: v for k, v in d.items() if v not in (None, "") or k in ("name", "type")}


@dataclass
class UnifySpec:
    mode: str = "map"
    description: str = ""
    fields: List[Field] = field(default_factory=list)
    record_mode: Optional[str] = None  # extract only
    record_description: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UnifySpec":
        if not isinstance(data, dict):
            raise DataPulseError("A unify spec must be a JSON object")
        raw_fields = data.get("fields")
        if not isinstance(raw_fields, list):
            raise DataPulseError("A unify spec needs a 'fields' list")
        fields = []
        for n, f in enumerate(raw_fields):
            if not isinstance(f, dict):
                raise DataPulseError(f"fields[{n}] must be an object")
            fields.append(Field(name=str(f.get("name") or ""), type=str(f.get("type") or "string"),
                                description=str(f.get("description") or ""), source=f.get("from"),
                                transform=f.get("transform") or None, format=f.get("format") or None,
                                separator=f.get("separator") or None))
        spec = cls(mode=str(data.get("mode") or "map"), description=str(data.get("description") or ""),
                   fields=fields, record_mode=data.get("record_mode") or None,
                   record_description=str(data.get("record_description") or ""))
        spec.validate()
        return spec

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"mode": self.mode, "description": self.description}
        if self.mode == "extract":
            out.update(record_mode=self.record_mode, record_description=self.record_description)
        out["fields"] = [f.to_dict() for f in self.fields]
        return out

    def validate(self) -> None:
        if self.mode not in MODES:
            raise DataPulseError(f"mode must be one of {', '.join(MODES)}")
        if not self.fields:
            raise DataPulseError("A unify spec needs at least one field")
        if self.mode == "extract" and self.record_mode not in RECORD_MODES:
            raise DataPulseError(f"record_mode must be one of {', '.join(RECORD_MODES)}")
        seen = set()
        for f in self.fields:
            where = f"field '{f.name}'"
            if not _NAME.match(f.name):
                raise DataPulseError(f"{where}: names must be snake_case (lowercase letters, digits, underscores)")
            if f.name in seen:
                raise DataPulseError(f"{where} appears twice")
            if f.name in PROVENANCE:
                raise DataPulseError(f"{where} is reserved for document provenance")
            seen.add(f.name)
            if f.type not in TYPES:
                raise DataPulseError(f"{where}: unknown type '{f.type}'. Use one of {', '.join(TYPES)}")
            if f.transform is not None and f.transform not in TRANSFORMS:
                raise DataPulseError(f"{where}: unknown transform '{f.transform}'. Use one of {', '.join(TRANSFORMS)}")
            if f.transform == "split" and not f.separator:
                raise DataPulseError(f"{where}: the split transform needs a separator")
            if self.mode == "map" and f.source is not None and not isinstance(f.source, str):
                raise DataPulseError(f"{where}: 'from' must be a string")


# ---------- reading values ----------

def get_path(record: Any, path: str) -> Any:
    """A key, or a dotted path through nested objects and lists (`items.0.sku`). Exact keys win, so CSV
    headers that contain dots still resolve."""
    if isinstance(record, dict) and path in record:
        return record[path]
    cur = record
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def record_paths(records: Iterable[Dict[str, Any]], limit: int = 5000) -> List[str]:
    """Every leaf path in the records, in first-seen order. Lists are leaves."""
    seen: Dict[str, None] = {}

    def walk(value: Any, prefix: str) -> None:
        if isinstance(value, dict) and value:
            for k, v in value.items():
                walk(v, f"{prefix}.{k}" if prefix else str(k))
        elif prefix:
            seen.setdefault(prefix, None)

    for n, rec in enumerate(records):
        if n >= limit:
            break
        walk(rec, "")
    return list(seen)


def fingerprint(paths: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(paths)).encode()).hexdigest()[:16]


# ---------- coercion ----------

def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and not value.strip()


def _to_number(value: Any, strip_symbols: bool) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    negative = False
    if strip_symbols:
        if text.startswith("(") and text.endswith(")"):  # accounting negatives: (1,200.00)
            negative, text = True, text[1:-1]
        text = _NUMBER_JUNK.sub("", text)
    number = float(text)
    if math.isinf(number) or math.isnan(number):
        raise ValueError("not a finite number")
    return -number if negative else number


def _to_datetime(value: Any, fmt: Optional[str]) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if fmt:
        return datetime.strptime(text, fmt)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = pd.to_datetime(text, errors="raise")
        return parsed.to_pydatetime()


def coerce(value: Any, f: Field) -> Tuple[Any, bool]:
    """Convert one raw value to the field's type. Returns (value, ok); failures become (None, False)."""
    if _is_missing(value):
        return None, True
    try:
        if f.transform == "trim" and isinstance(value, str):
            value = value.strip()
        elif f.transform == "lowercase_trim" and isinstance(value, str):
            value = value.strip().lower()
        elif f.transform == "split":
            if isinstance(value, str):
                value = [part.strip() for part in value.split(f.separator or ",") if part.strip()]
        elif f.transform == "json_string":
            value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)

        t = f.type
        if t == "string":
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False, default=str), True
            if isinstance(value, float) and value.is_integer():
                return str(int(value)), True
            return str(value), True
        if t in ("number", "integer"):
            number = _to_number(value, strip_symbols=f.transform == "parse_number")
            if t == "integer":
                if not number.is_integer():
                    return None, False
                return int(number), True
            return number, True
        if t == "boolean":
            if isinstance(value, bool):
                return value, True
            text = str(value).strip().lower()
            if text in _TRUE:
                return True, True
            if text in _FALSE:
                return False, True
            return None, False
        if t == "date":
            return _to_datetime(value, f.format).date().isoformat(), True
        if t == "datetime":
            # A space separator matches how pandas writes and reads timestamps in CSV files.
            return _to_datetime(value, f.format).isoformat(sep=" "), True
        if t == "object":
            if isinstance(value, dict):
                return value, True
            parsed = json.loads(value) if isinstance(value, str) else None
            return (parsed, True) if isinstance(parsed, dict) else (None, False)
        if t == "array":
            if isinstance(value, list):
                return value, True
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except ValueError:
                    parsed = None
                return (parsed, True) if isinstance(parsed, list) else ([value], True)
            return [value], True
    except (ValueError, TypeError, OverflowError):
        return None, False
    return None, False


def apply_spec(records: Iterable[Dict[str, Any]], spec: UnifySpec) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Every record through the spec. Returns (rows, parse_errors per field); unparsable values become null."""
    errors: Dict[str, int] = {}
    rows = []
    for rec in records:
        row = {}
        for f in spec.fields:
            raw = get_path(rec, f.source or f.name)
            value, ok = coerce(raw, f)
            if not ok:
                errors[f.name] = errors.get(f.name, 0) + 1
            row[f.name] = value
        rows.append(row)
    return rows, errors


_JSON_TYPES = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean",
               "date": "string", "datetime": "string", "object": "object", "array": "array"}


def to_json_schema(spec: UnifySpec) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    for f in spec.fields:
        prop: Dict[str, Any] = {"type": [_JSON_TYPES[f.type], "null"]}
        if f.type == "date":
            prop["format"] = "date"
        elif f.type == "datetime":
            prop["format"] = "date-time"
        if f.description:
            prop["description"] = f.description
        props[f.name] = prop
    if spec.mode == "extract":
        props["_document"] = {"type": "string", "description": "File name or URL the record came from"}
        props["_page"] = {"type": ["integer", "null"], "description": "Page number, when known"}
        props["_confidence"] = {"type": "string", "enum": ["low", "medium", "high"],
                                "description": "How sure the AI was about this record"}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
            "description": spec.description, "properties": props}


def spec_diff(old: Optional[UnifySpec], new: UnifySpec) -> Optional[Dict[str, Any]]:
    """What changed between two specs, for the run report. None when nothing did."""
    if old is None:
        return None
    before = {f.name: f for f in old.fields}
    after = {f.name: f for f in new.fields}
    diff = {
        "added": [n for n in after if n not in before],
        "removed": [n for n in before if n not in after],
        "remapped": [{"name": n, "from_before": before[n].source, "from_now": after[n].source}
                     for n in after if n in before and before[n].source != after[n].source],
        "retyped": [{"name": n, "before": before[n].type, "now": after[n].type}
                    for n in after if n in before and before[n].type != after[n].type],
    }
    return diff if any(diff.values()) else None
