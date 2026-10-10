"""The AI unifier for unstructured data: proposes an extraction schema, then extracts records per document.

Documents are sent to the AI in full (PDFs and images as files, so scans can be read). Code still coerces
every value to the schema's types, so the output shape never depends on what the model returns.
"""
from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from datapulse.ai import create, get_client, json_format, text_of
from datapulse.config import Config
from datapulse.errors import DataPulseError

from .documents import Document
from .heuristic import default_document_spec
from .spec import RECORD_MODES, TYPES, UnifySpec, apply_spec

SAMPLE_DOCUMENTS = 3
SAMPLE_TEXT_CHARS = 20_000
CONCURRENCY = 4

SCHEMA_SYSTEM = f"""You are the data unifier for a data platform. Unstructured documents arrive - PDFs, scans, \
photos, Word files, Markdown and text, emails, web pages - and must become clean, well-typed JSON records. \
Look at the sample documents from one source and design the record schema every document from this source \
will be extracted into.

Decide record_mode:
- one_per_document when each document describes one thing (a contract, an email, a resume, an article).
- many_per_document when each document holds a list or table of things (invoice line items, a transactions \
statement, a product list on a web page). Repeat document-level facts (vendor, invoice date) on every record.

Fields: snake_case names, a type from {", ".join(TYPES)}, and a one-sentence description precise enough that \
another reader extracts the same value from a new document. Prefer the facts people would filter, total or \
join on (ids, names, dates, amounts, statuses, emails). Use number for money and quantities (no currency \
symbols), date or datetime for dates. Do not add _document, _page or _confidence; they are added automatically. \
If a previous schema is given, keep its field names and types unless they clearly no longer fit."""

EXTRACT_SYSTEM = """You extract structured records from one document into a fixed schema. Use only facts that \
appear in the document; use null when a field is not present - never guess. Numbers are plain numbers (no \
currency symbols or thousands separators). Dates are ISO 8601 (YYYY-MM-DD, or YYYY-MM-DDTHH:MM:SS). For each \
record give the page it came from when you can tell (else null), and your confidence: high when the value is \
printed clearly, medium when you had to interpret layout or wording, low when the text is unclear or partly \
illegible."""

SCHEMA_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["description", "record_mode", "record_description", "fields"],
    "properties": {
        "description": {"type": "string"},
        "record_mode": {"type": "string", "enum": list(RECORD_MODES)},
        "record_description": {"type": "string"},
        "fields": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "type", "description"],
            "properties": {"name": {"type": "string"}, "type": {"type": "string", "enum": list(TYPES)},
                           "description": {"type": "string"}}}},
    },
}

_JSON = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean",
         "date": "string", "datetime": "string", "object": "string", "array": "array"}


def records_schema(spec: UnifySpec) -> Dict[str, Any]:
    """The structured-output schema for one document's records. Objects travel as JSON text."""
    props: Dict[str, Any] = {}
    for f in spec.fields:
        if f.type == "array":
            props[f.name] = {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
                             "description": f.description}
        else:
            props[f.name] = {"type": [_JSON[f.type], "null"], "description": f.description}
    props["_page"] = {"type": ["integer", "null"]}
    props["_confidence"] = {"type": "string", "enum": ["low", "medium", "high"]}
    return {"type": "object", "additionalProperties": False, "required": ["records"],
            "properties": {"records": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": list(props), "properties": props}}}}


def content_blocks(doc: Document, max_text: Optional[int] = None) -> List[Dict[str, Any]]:
    """A document as Messages API content: PDFs and images as files, everything else as text."""
    label = f"Document: {doc.name} ({doc.kind}{f', {doc.pages} pages' if doc.pages else ''})"
    if doc.kind == "pdf" and doc.data:
        return [{"type": "text", "text": label},
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.b64encode(doc.data).decode("ascii")}}]
    if doc.kind == "image" and doc.data:
        return [{"type": "text", "text": label},
                {"type": "image", "source": {"type": "base64", "media_type": doc.media_type or "image/png",
                                             "data": base64.b64encode(doc.data).decode("ascii")}}]
    text = doc.text if max_text is None else doc.text[:max_text]
    note = "\n[text truncated]" if doc.truncated or (max_text is not None and len(doc.text) > max_text) else ""
    return [{"type": "text", "text": f"{label}\n\n{text}{note}"}]


def propose_extraction_schema(docs: List[Document], previous: Optional[UnifySpec], cfg: Config,
                              client=None, attempts: int = 2, source_label: str = "") -> UnifySpec:
    client = client or get_client(cfg)
    content: List[Dict[str, Any]] = [{"type": "text", "text": json.dumps({
        "source": source_label or "unknown", "document_count": len(docs),
        "document_kinds": sorted({d.kind for d in docs}),
        "previous_schema": previous.to_dict() if previous else None})}]
    for doc in docs[:SAMPLE_DOCUMENTS]:
        content += content_blocks(doc, SAMPLE_TEXT_CHARS)
    messages: List[Dict[str, Any]] = [{"role": "user", "content": content}]
    error = ""
    for _ in range(attempts):
        response = create(client, cfg, system=SCHEMA_SYSTEM, output_config=json_format(SCHEMA_SCHEMA),
                          messages=messages)
        try:
            draft = json.loads(text_of(response))
            return UnifySpec.from_dict({**draft, "mode": "extract"})
        except (ValueError, KeyError, TypeError, DataPulseError) as e:
            error = str(e)
            messages += [{"role": "assistant", "content": response.content},
                         {"role": "user", "content": f"That schema is invalid: {error}\nReturn a corrected schema."}]
    raise DataPulseError(f"The AI could not produce a valid extraction schema ({error}).")


def extract_records(doc: Document, spec: UnifySpec, cfg: Config, client=None) -> List[Dict[str, Any]]:
    """One document's records, as the AI returned them (not yet coerced)."""
    client = client or get_client(cfg)
    instruction = {"schema_description": spec.description, "record_mode": spec.record_mode,
                   "each_record_is": spec.record_description,
                   "fields": [{"name": f.name, "type": f.type, "description": f.description} for f in spec.fields]}
    if spec.record_mode == "one_per_document":
        instruction["rule"] = "Return exactly one record."
    content = [{"type": "text", "text": json.dumps(instruction)}] + content_blocks(doc)
    response = create(client, cfg, system=EXTRACT_SYSTEM, output_config=json_format(records_schema(spec)),
                      messages=[{"role": "user", "content": content}])
    try:
        records = json.loads(text_of(response))["records"]
    except (ValueError, KeyError, TypeError) as e:
        raise DataPulseError(f"The AI returned invalid records for {doc.name}: {e}")
    if not isinstance(records, list):
        raise DataPulseError(f"The AI returned invalid records for {doc.name}")
    records = [r for r in records if isinstance(r, dict)]
    if spec.record_mode == "one_per_document":
        records = records[:1]
    return records


def _finish(raw: List[Dict[str, Any]], doc: Document, spec: UnifySpec,
            errors: Dict[str, int]) -> List[Dict[str, Any]]:
    rows, errs = apply_spec(raw, spec)
    for k, v in errs.items():
        errors[k] = errors.get(k, 0) + v
    out = []
    for rec, row in zip(raw, rows):
        page = rec.get("_page")
        confidence = rec.get("_confidence")
        row["_document"] = doc.name
        row["_page"] = page if isinstance(page, int) and not isinstance(page, bool) else None
        row["_confidence"] = confidence if confidence in ("low", "medium", "high") else "medium"
        out.append(row)
    return out


def extract_all(docs: List[Document], spec: UnifySpec, cfg: Config,
                client=None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Every document through the AI, a few at a time. Returns (rows, stats)."""
    client = client or get_client(cfg)
    errors: Dict[str, int] = {}
    failed: List[Dict[str, str]] = []
    empty: List[str] = []

    def run(doc: Document):
        try:
            return doc, extract_records(doc, spec, cfg, client), None
        except DataPulseError as exc:
            return doc, [], str(exc)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        results = list(pool.map(run, docs))  # map keeps document order
    rows: List[Dict[str, Any]] = []
    for doc, raw, error in results:
        if error:
            failed.append({"name": doc.name, "reason": error})
            continue
        if not raw:
            empty.append(doc.name)
            continue
        rows.extend(_finish(raw, doc, spec, errors))
    if not rows:
        reasons = "; ".join(f"{f['name']}: {f['reason']}" for f in failed[:3])
        raise DataPulseError(f"No records could be extracted{f' ({reasons})' if reasons else ''}")
    stats = {"documents": len(docs), "failed_documents": failed, "documents_without_records": empty,
             "parse_errors": errors, "low_confidence": sum(1 for r in rows if r["_confidence"] == "low")}
    return rows, stats


def fallback_records(docs: List[Document]) -> Tuple[UnifySpec, List[Dict[str, Any]], Dict[str, Any]]:
    """No AI: one record per document with its text. Scans and images have no text without the AI."""
    spec = default_document_spec()
    rows = []
    for doc in docs:
        text = doc.text.strip()
        title = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if doc.kind == "email":
            subject = next((line[len("Subject:"):].strip() for line in text.splitlines()
                            if line.startswith("Subject:")), "")
            title = subject or title
        if title.startswith("[page "):
            title = next((line.strip() for line in text.splitlines()[1:] if line.strip()), "")
        rows.append({"title": title[:200] or doc.name, "text": text, "word_count": len(text.split()),
                     "_document": doc.name, "_page": None, "_confidence": "low" if not text else "high"})
    stats = {"documents": len(docs), "failed_documents": [], "documents_without_records": [],
             "parse_errors": {}, "low_confidence": sum(1 for r in rows if r["_confidence"] == "low"),
             "note": "No AI key is configured, so each document became one record of its text."}
    return spec, rows, stats
