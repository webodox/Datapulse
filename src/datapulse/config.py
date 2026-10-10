"""Loading and strict validation of datapulse.yml."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .errors import DataPulseError

SEVERITIES = ("critical", "warning", "info")
LEVEL = {"info": 0, "warning": 1, "critical": 2}
FAIL_ON_CHOICES = ("critical", "warning", "info", "never")
DEFAULT_CONFIG_NAME = "datapulse.yml"


@dataclass
class SchemaCfg:
    missing_columns: str = "critical"
    added_columns: str = "warning"
    type_changes: str = "critical"


@dataclass
class RowCountCfg:
    severity: str = "warning"
    max_change_pct: float = 20.0
    min_rows: Optional[int] = None


@dataclass
class NullRateCfg:
    severity: str = "critical"
    max_increase: float = 0.05
    columns: Dict[str, float] = field(default_factory=dict)


@dataclass
class CategoricalCfg:
    severity: str = "warning"
    max_unexpected_rate: float = 0.0
    columns: Optional[List[str]] = None


@dataclass
class DuplicatesCfg:
    severity: str = "critical"
    max_duplicate_rate: float = 0.0
    columns: List[str] = field(default_factory=list)


@dataclass
class DistributionCfg:
    severity: str = "warning"
    psi_threshold: float = 0.2
    max_mean_shift_std: float = 1.0
    columns: Optional[List[str]] = None


@dataclass
class Consumer:
    name: str
    type: str
    columns: List[str]
    owner: str = ""
    description: str = ""


@dataclass
class ReportCfg:
    markdown: Optional[str] = "datapulse-report.md"
    max_examples: int = 5
    show_values: bool = True


@dataclass
class DiscoveryCfg:
    enabled: bool = False
    root: str = "."
    scope: str = "readers"  # readers: only files that mention the data file; all: every source file
    exclude: List[str] = field(default_factory=list)


@dataclass
class AICfg:
    enabled: bool = False
    model: str = "claude-sonnet-5-5"
    api_key_env: str = "ANTHROPIC_API_KEY"


@dataclass
class Config:
    baseline: str = ".datapulse/baseline.json"
    fail_on: str = "critical"
    max_categories: int = 50
    schema: SchemaCfg = field(default_factory=SchemaCfg)
    row_count: RowCountCfg = field(default_factory=RowCountCfg)
    null_rate: NullRateCfg = field(default_factory=NullRateCfg)
    categorical: CategoricalCfg = field(default_factory=CategoricalCfg)
    duplicates: DuplicatesCfg = field(default_factory=DuplicatesCfg)
    distribution: DistributionCfg = field(default_factory=DistributionCfg)
    consumers: List[Consumer] = field(default_factory=list)
    report: ReportCfg = field(default_factory=ReportCfg)
    ai: AICfg = field(default_factory=AICfg)
    discovery: DiscoveryCfg = field(default_factory=DiscoveryCfg)
    source: Optional[str] = None


STARTER_CONFIG = """\
# DataPulse configuration. Docs: README.md
version: 1

# Where `datapulse baseline` stores (and `datapulse check` reads) the baseline profile.
baseline: .datapulse/baseline.json

# Exit with a nonzero code when an issue at or above this severity is found.
# One of: critical | warning | info | never
fail_on: critical

checks:
  # Each severity is one of: critical | warning | info | off
  schema:
    missing_columns: critical
    added_columns: warning
    type_changes: critical
  row_count:
    severity: warning
    max_change_pct: 20        # allowed % change in row count vs. baseline
  null_rate:
    severity: critical
    max_increase: 0.05        # allowed absolute increase (0.05 = 5 percentage points)
    # columns:
    #   customer_id: 0.0      # per-column override
  categorical:
    severity: warning
    max_unexpected_rate: 0.0  # share of rows with a value not seen in the baseline
  duplicates:
    severity: critical
    max_duplicate_rate: 0.0
    # columns: [order_id]     # key columns that must be unique (omit for whole-row duplicates)
  distribution:
    severity: warning
    psi_threshold: 0.2        # Population Stability Index limit
    max_mean_shift_std: 1.0   # allowed mean shift, in baseline standard deviations

# Map columns to the things that read them so reports can show who is affected.
# consumers:
#   revenue_dashboard:
#     type: dashboard
#     owner: finance-analytics
#     columns: [amount, created_at]

report:
  markdown: datapulse-report.md   # set to null to skip writing the Markdown report
  max_examples: 5
  show_values: true               # false hides example data values from reports

# Find code (any language) that reads the data file and references its columns, and show it in reports.
# Heuristic text search; also available as `datapulse check --discover` and `datapulse scan`.
discovery:
  enabled: false
  root: .
  scope: readers                  # readers | all
  exclude: []                     # extra directory names to skip

ai:
  enabled: false                  # or pass --ai; needs the API key env var below
  model: claude-sonnet-5-5
  api_key_env: ANTHROPIC_API_KEY
"""


def _fail(msg: str):
    raise DataPulseError(f"Invalid configuration: {msg}")


def _mapping(raw, path: str, allowed) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _fail(f"'{path}' must be a mapping")
    bad = sorted(str(k) for k in raw if k not in allowed)
    if bad:
        _fail(f"unknown key(s) {bad} in '{path}'. Allowed keys: {sorted(allowed)}")
    return raw


def _severity(v, path: str, allow_off: bool = True) -> str:
    choices = SEVERITIES + (("off",) if allow_off else ())
    if v not in choices:
        _fail(f"'{path}' must be one of {list(choices)}, got {v!r}")
    return v


def _number(v, path: str, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _fail(f"'{path}' must be a number, got {v!r}")
    if lo is not None and v < lo:
        _fail(f"'{path}' must be >= {lo}, got {v!r}")
    if hi is not None and v > hi:
        _fail(f"'{path}' must be <= {hi}, got {v!r}")
    return float(v)


def _int(v, path: str, lo: int = 0) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        _fail(f"'{path}' must be an integer, got {v!r}")
    if v < lo:
        _fail(f"'{path}' must be >= {lo}, got {v!r}")
    return v


def _bool(v, path: str) -> bool:
    if not isinstance(v, bool):
        _fail(f"'{path}' must be true or false, got {v!r}")
    return v


def _str(v, path: str) -> str:
    if not isinstance(v, str) or not v.strip():
        _fail(f"'{path}' must be a non-empty string, got {v!r}")
    return v


def _str_list(v, path: str) -> List[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        _fail(f"'{path}' must be a list of column names")
    return list(v)


def parse_config(raw) -> Config:
    top = _mapping(raw, "<root>", {"version", "baseline", "fail_on", "max_categories",
                                   "checks", "consumers", "report", "ai", "discovery"})
    cfg = Config()
    if top.get("version", 1) != 1:
        _fail(f"unsupported 'version' {top.get('version')!r} (this release supports 1)")
    if "baseline" in top:
        cfg.baseline = _str(top["baseline"], "baseline")
    if "fail_on" in top:
        if top["fail_on"] not in FAIL_ON_CHOICES:
            _fail(f"'fail_on' must be one of {list(FAIL_ON_CHOICES)}, got {top['fail_on']!r}")
        cfg.fail_on = top["fail_on"]
    if "max_categories" in top:
        cfg.max_categories = _int(top["max_categories"], "max_categories", 1)

    checks = _mapping(top.get("checks"), "checks",
                      {"schema", "row_count", "null_rate", "categorical", "duplicates", "distribution"})
    s = _mapping(checks.get("schema"), "checks.schema", {"missing_columns", "added_columns", "type_changes"})
    for k in s:
        setattr(cfg.schema, k, _severity(s[k], f"checks.schema.{k}"))

    r = _mapping(checks.get("row_count"), "checks.row_count", {"severity", "max_change_pct", "min_rows"})
    if "severity" in r:
        cfg.row_count.severity = _severity(r["severity"], "checks.row_count.severity")
    if "max_change_pct" in r:
        cfg.row_count.max_change_pct = _number(r["max_change_pct"], "checks.row_count.max_change_pct", 0)
    if r.get("min_rows") is not None:
        cfg.row_count.min_rows = _int(r["min_rows"], "checks.row_count.min_rows")

    n = _mapping(checks.get("null_rate"), "checks.null_rate", {"severity", "max_increase", "columns"})
    if "severity" in n:
        cfg.null_rate.severity = _severity(n["severity"], "checks.null_rate.severity")
    if "max_increase" in n:
        cfg.null_rate.max_increase = _number(n["max_increase"], "checks.null_rate.max_increase", 0, 1)
    cols = _mapping(n.get("columns"), "checks.null_rate.columns", n.get("columns") or {})
    for c, v in cols.items():
        cfg.null_rate.columns[str(c)] = _number(v, f"checks.null_rate.columns.{c}", 0, 1)

    c = _mapping(checks.get("categorical"), "checks.categorical", {"severity", "max_unexpected_rate", "columns"})
    if "severity" in c:
        cfg.categorical.severity = _severity(c["severity"], "checks.categorical.severity")
    if "max_unexpected_rate" in c:
        cfg.categorical.max_unexpected_rate = _number(c["max_unexpected_rate"], "checks.categorical.max_unexpected_rate", 0, 1)
    if c.get("columns") is not None:
        cfg.categorical.columns = _str_list(c["columns"], "checks.categorical.columns")

    d = _mapping(checks.get("duplicates"), "checks.duplicates", {"severity", "max_duplicate_rate", "columns"})
    if "severity" in d:
        cfg.duplicates.severity = _severity(d["severity"], "checks.duplicates.severity")
    if "max_duplicate_rate" in d:
        cfg.duplicates.max_duplicate_rate = _number(d["max_duplicate_rate"], "checks.duplicates.max_duplicate_rate", 0, 1)
    if d.get("columns") is not None:
        cfg.duplicates.columns = _str_list(d["columns"], "checks.duplicates.columns")

    t = _mapping(checks.get("distribution"), "checks.distribution",
                 {"severity", "psi_threshold", "max_mean_shift_std", "columns"})
    if "severity" in t:
        cfg.distribution.severity = _severity(t["severity"], "checks.distribution.severity")
    if "psi_threshold" in t:
        cfg.distribution.psi_threshold = _number(t["psi_threshold"], "checks.distribution.psi_threshold", 0)
    if "max_mean_shift_std" in t:
        cfg.distribution.max_mean_shift_std = _number(t["max_mean_shift_std"], "checks.distribution.max_mean_shift_std", 0)
    if t.get("columns") is not None:
        cfg.distribution.columns = _str_list(t["columns"], "checks.distribution.columns")

    cons = top.get("consumers")
    if cons is not None:
        if not isinstance(cons, dict):
            _fail("'consumers' must be a mapping of consumer name -> definition")
        for name, spec in cons.items():
            p = f"consumers.{name}"
            spec = _mapping(spec, p, {"type", "columns", "owner", "description"})
            if "columns" not in spec:
                _fail(f"'{p}.columns' is required")
            cfg.consumers.append(Consumer(
                name=str(name),
                type=_str(spec.get("type", "other"), f"{p}.type"),
                columns=_str_list(spec["columns"], f"{p}.columns"),
                owner=str(spec.get("owner", "")),
                description=str(spec.get("description", "")),
            ))

    rep = _mapping(top.get("report"), "report", {"markdown", "max_examples", "show_values"})
    if "markdown" in rep:
        cfg.report.markdown = None if rep["markdown"] is None else _str(rep["markdown"], "report.markdown")
    if "max_examples" in rep:
        cfg.report.max_examples = _int(rep["max_examples"], "report.max_examples")
    if "show_values" in rep:
        cfg.report.show_values = _bool(rep["show_values"], "report.show_values")

    dis = _mapping(top.get("discovery"), "discovery", {"enabled", "root", "scope", "exclude"})
    if "enabled" in dis:
        cfg.discovery.enabled = _bool(dis["enabled"], "discovery.enabled")
    if "root" in dis:
        cfg.discovery.root = _str(dis["root"], "discovery.root")
    if "scope" in dis:
        if dis["scope"] not in ("readers", "all"):
            _fail(f"'discovery.scope' must be 'readers' or 'all', got {dis['scope']!r}")
        cfg.discovery.scope = dis["scope"]
    if dis.get("exclude") is not None:
        cfg.discovery.exclude = _str_list(dis["exclude"], "discovery.exclude")

    ai = _mapping(top.get("ai"), "ai", {"enabled", "model", "api_key_env"})
    if "enabled" in ai:
        cfg.ai.enabled = _bool(ai["enabled"], "ai.enabled")
    if "model" in ai:
        cfg.ai.model = _str(ai["model"], "ai.model")
    if "api_key_env" in ai:
        cfg.ai.api_key_env = _str(ai["api_key_env"], "ai.api_key_env")
    return cfg


def load_config(path: Optional[Path] = None) -> Config:
    """Load `path` (must exist), or ./datapulse.yml if present, else defaults."""
    explicit = path is not None
    p = Path(path) if explicit else Path(DEFAULT_CONFIG_NAME)
    if not p.exists():
        if explicit:
            raise DataPulseError(f"Config file not found: {p}")
        return Config()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise DataPulseError(f"Could not read {p}: {e}")
    return parse_config_text(text, str(p))


def parse_config_text(text: str, origin: str) -> Config:
    """Parse and validate configuration text (a file, or a file read from a git ref)."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise DataPulseError(f"{origin} is not valid YAML: {e}")
    try:
        cfg = parse_config(raw)
    except DataPulseError as e:
        raise DataPulseError(f"{origin}: {e}")
    cfg.source = origin
    return cfg
