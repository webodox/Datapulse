"""Raw files in, unified rows out: picks the map or extract path, reuses or (re)writes the spec."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from datapulse.ai import get_api_key
from datapulse.config import Config
from datapulse.errors import DataPulseError

from .agent import propose_spec, sample_records
from .documents import is_document, is_tabular, read_documents
from .extract import extract_all, fallback_records, propose_extraction_schema
from .heuristic import default_spec
from .records import read_raw_records
from .spec import UnifySpec, apply_spec, fingerprint, record_paths, spec_diff


@dataclass
class StoredSpec:
    spec: UnifySpec
    origin: str  # ai | heuristic | user
    fingerprint: str = ""
    stale: bool = False


@dataclass
class UnifyResult:
    mode: str
    spec: UnifySpec
    origin: str
    rows: List[Dict[str, Any]]
    stats: Dict[str, Any]
    fingerprint: str
    paths: List[str] = field(default_factory=list)
    sample: Optional[List[Dict[str, Any]]] = None  # map path: raw records kept for regenerating the spec
    reused: bool = False
    changed: Optional[Dict[str, Any]] = None
    raw_preview: List[Dict[str, Any]] = field(default_factory=list)


def ai_available(cfg: Config) -> bool:
    return bool(get_api_key(cfg)) and os.getenv("DATAPULSE_UNIFY_AI", "1") != "0"


def classify(files: List[Tuple[Path, str]]) -> str:
    kinds = {"extract" if is_document(p) else "map" if is_tabular(p) else "unknown" for p, _ in files}
    if "unknown" in kinds:
        bad = next(label for p, label in files if not (is_document(p) or is_tabular(p)))
        raise DataPulseError(f"Unsupported file type for {bad}")
    if len(kinds) > 1:
        raise DataPulseError("Upload data files (CSV, Excel, JSON, Parquet) and documents (PDF, Word, text, "
                             "email, web pages, images) separately; one upload is one kind of source.")
    return kinds.pop()


def _map(files: List[Tuple[Path, str]], previous: Optional[StoredSpec], cfg: Config, use_ai: bool, force: bool,
         client, source_label: str) -> UnifyResult:
    records: List[Dict[str, Any]] = []
    for path, label in files:
        records.extend(read_raw_records(path, label))
    paths = record_paths(records)
    fp = fingerprint(paths)
    prev = previous if previous and previous.spec.mode == "map" else None
    stats: Dict[str, Any] = {"records": len(records)}
    reuse = (prev is not None and not force and not prev.stale and prev.fingerprint == fp
             and not (prev.origin == "heuristic" and use_ai))
    if reuse:
        spec, origin = prev.spec, prev.origin
    elif use_ai:
        try:
            spec, origin = propose_spec(records, prev.spec if prev else None, cfg, client=client,
                                        source_label=source_label), "ai"
        except DataPulseError as exc:
            spec, origin = default_spec(records), "heuristic"
            stats["note"] = f"AI mapping unavailable ({exc}); used an automatic mapping."
    else:
        spec, origin = default_spec(records), "heuristic"
    rows, errors = apply_spec(records, spec)
    stats["parse_errors"] = errors
    return UnifyResult(mode="map", spec=spec, origin=origin, rows=rows, stats=stats, fingerprint=fp, paths=paths,
                       sample=sample_records(records), reused=reuse,
                       changed=None if reuse or prev is None else spec_diff(prev.spec, spec),
                       raw_preview=records[:20])


def _extract(files: List[Tuple[Path, str]], previous: Optional[StoredSpec], cfg: Config, use_ai: bool,
             force: bool, client, source_label: str, max_documents: Optional[int] = None) -> UnifyResult:
    docs, skipped = read_documents(files)
    if max_documents is not None and len(docs) > max_documents:
        skipped += [{"name": d.name, "reason": "not included in the preview"} for d in docs[max_documents:]]
        docs = docs[:max_documents]
    fp = fingerprint(sorted({d.kind for d in docs}))
    prev = previous if previous and previous.spec.mode == "extract" else None
    reuse = (prev is not None and not force and not prev.stale and not (prev.origin == "heuristic" and use_ai))
    note = None
    if not use_ai:
        spec, rows, stats = fallback_records(docs)
        origin, reuse = "heuristic", False
    else:
        if reuse:
            spec, origin = prev.spec, prev.origin
        else:
            try:
                spec = propose_extraction_schema(docs, prev.spec if prev else None, cfg, client=client,
                                                 source_label=source_label)
                origin = "ai"
            except DataPulseError as exc:
                spec, origin, note = None, "heuristic", f"AI extraction unavailable ({exc}); kept document text."
        if spec is None:
            spec, rows, stats = fallback_records(docs)
            stats["note"] = note
        else:
            rows, stats = extract_all(docs, spec, cfg, client=client)
    stats["skipped_documents"] = skipped
    raw_preview = [{"document": d.name, "kind": d.kind, "pages": d.pages, "text": d.text[:500]} for d in docs[:20]]
    return UnifyResult(mode="extract", spec=spec, origin=origin, rows=rows, stats=stats, fingerprint=fp,
                       reused=reuse, changed=None if reuse or prev is None else spec_diff(prev.spec, spec),
                       raw_preview=raw_preview)


def unify_files(files: List[Tuple[Path, str]], previous: Optional[StoredSpec], cfg: Config, *,
                force: bool = False, client=None, source_label: str = "",
                use_ai: Optional[bool] = None, max_documents: Optional[int] = None) -> UnifyResult:
    """files: (path, name the user knows). `previous` is the source's current spec, if any.
    `max_documents` limits how many documents go through the AI (used for previews)."""
    if not files:
        raise DataPulseError("No files to unify")
    use_ai = ai_available(cfg) if use_ai is None else use_ai
    if classify(files) == "map":
        return _map(files, previous, cfg, use_ai, force, client, source_label)
    return _extract(files, previous, cfg, use_ai, force, client, source_label, max_documents)


def write_rows(rows: List[Dict[str, Any]], path: Path) -> Path:
    if not rows:
        raise DataPulseError("No records were produced")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def read_rows(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
                if limit is not None and len(rows) >= limit:
                    break
    return rows
