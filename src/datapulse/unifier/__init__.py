"""The AI data unifier: any source - tables, nested JSON, documents - into clean, typed JSON records.

- Map path (databases, spreadsheets, CSV/Excel/Parquet, JSON APIs): the AI writes a mapping from a sample;
  code applies it to every row (`agent.propose_spec`, `spec.apply_spec`).
- Extract path (PDF, scans, Word, Markdown/text, email, web pages): the AI proposes a schema, then extracts
  records from each document (`extract`).
"""
from .pipeline import StoredSpec, UnifyResult, ai_available, classify, read_rows, unify_files, write_rows
from .spec import Field, UnifySpec, apply_spec, coerce, fingerprint, spec_diff, to_json_schema

__all__ = ["Field", "StoredSpec", "UnifyResult", "UnifySpec", "ai_available", "apply_spec", "classify", "coerce",
           "fingerprint", "read_rows", "spec_diff", "to_json_schema", "unify_files", "write_rows"]
