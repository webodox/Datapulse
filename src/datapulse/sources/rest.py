"""Generic REST API plug-in: paste a URL, get JSON rows."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from datapulse.errors import DataPulseError
from datapulse.sources.http import fetch_bytes, require_str


def _dig(data: Any, path: str) -> Any:
    """Follow a dotted path like 'data.items' through dicts."""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise DataPulseError(f"records_path '{path}' not found in the JSON response")
        cur = cur[part]
    return cur


def _first_list_of_objects(data: Any) -> list | None:
    """Walk a JSON value and return the first list whose items are all objects."""
    if isinstance(data, list):
        if data and all(isinstance(x, dict) for x in data):
            return data
        return None
    if isinstance(data, dict):
        for value in data.values():
            found = _first_list_of_objects(value)
            if found is not None:
                return found
    return None


def extract_records(payload: Any, records_path: str | None) -> list:
    if records_path:
        payload = _dig(payload, records_path.strip())
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        # Single object → one-row table.
        found = _first_list_of_objects(payload)
        if found is not None:
            return found
        return [payload]
    raise DataPulseError("JSON response must be an object or an array of objects")


class RestSource:
    kind = "rest"

    def fetch(self, config: dict, secrets: dict, dest_dir: Path) -> Path:
        url = require_str(config, "url", label="API URL")
        method = str(config.get("method") or "GET").upper()
        records_path = config.get("records_path") or None
        if records_path is not None and not isinstance(records_path, str):
            raise DataPulseError("records_path must be a string like 'data.items'")

        # Non-secret headers from config + secret headers (Authorization, etc.).
        headers: dict[str, str] = {}
        for bucket in (config.get("headers"), secrets.get("headers")):
            if not bucket:
                continue
            if not isinstance(bucket, dict):
                raise DataPulseError("headers must be an object of string → string")
            for k, v in bucket.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise DataPulseError("Each header name and value must be a string")
                headers[k] = v

        body, response_headers, _ = fetch_bytes(url, method=method, headers=headers or None)
        ctype = response_headers.get("content-type", "").lower()
        if "json" not in ctype and not body.lstrip()[:1] in (b"{", b"["):
            raise DataPulseError(
                f"Expected a JSON response but got Content-Type '{ctype or 'unknown'}'"
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataPulseError(f"Response is not valid JSON: {exc}") from exc

        records = extract_records(payload, records_path)
        if not records:
            raise DataPulseError("No rows found in the response")
        if not all(isinstance(r, dict) for r in records):
            raise DataPulseError("Each row must be a JSON object")
        # Nesting is kept: the unifier decides how nested fields map to output fields.
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{uuid.uuid4().hex}.jsonl"
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
        return path
