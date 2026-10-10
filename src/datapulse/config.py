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
class MLDriftCfg:
    severity: str = "warning"
    max_auc: float = 0.7  # 0.5 = the model can't tell the files apart; 1.0 = it always can
    max_rows: int = 5000
    exclude: List[str] = field(default_factory=list)


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
    ml_drift: MLDriftCfg = field(default_factory=MLDriftCfg)
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
  ml_drift:                   # a model learns to tell baseline rows from new rows; needs the baseline file
    severity: warning
    max_auc: 0.7              # 0.5 = indistinguishable (no drift), 1.0 = completely different

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


def _choice(*choices):
    def check(v, path):
        if v not in choices:
            _fail(f"'{path}' must be one of {list(choices)}, got {v!r}")
        return v
    return check


def _number(lo: Optional[float] = None, hi: Optional[float] = None):
    def check(v, path):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            _fail(f"'{path}' must be a number, got {v!r}")
        if lo is not None and v < lo:
            _fail(f"'{path}' must be >= {lo}, got {v!r}")
        if hi is not None and v > hi:
            _fail(f"'{path}' must be <= {hi}, got {v!r}")
        return float(v)
    return check


def _int(lo: int = 0):
    def check(v, path):
        if isinstance(v, bool) or not isinstance(v, int):
            _fail(f"'{path}' must be an integer, got {v!r}")
        if v < lo:
            _fail(f"'{path}' must be >= {lo}, got {v!r}")
        return v
    return check


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


def _rate_map(v, path: str) -> Dict[str, float]:
    return {str(c): _number(0, 1)(x, f"{path}.{c}") for c, x in _mapping(v, path, v or {}).items()}


def _nullable(check):
    """A validator whose explicit `null` is stored (by default `null` keeps the default value)."""
    check.nullable = True
    return check


SEVERITY = _choice(*SEVERITIES, "off")
MIN_ML_ROWS = 100
RATE = _number(0, 1)
# section path -> (attribute on Config, {key: validator})
SECTIONS = {
    "checks.schema": ("schema", {"missing_columns": SEVERITY, "added_columns": SEVERITY, "type_changes": SEVERITY}),
    "checks.row_count": ("row_count", {"severity": SEVERITY, "max_change_pct": _number(0), "min_rows": _int()}),
    "checks.null_rate": ("null_rate", {"severity": SEVERITY, "max_increase": RATE, "columns": _rate_map}),
    "checks.categorical": ("categorical", {"severity": SEVERITY, "max_unexpected_rate": RATE, "columns": _str_list}),
    "checks.duplicates": ("duplicates", {"severity": SEVERITY, "max_duplicate_rate": RATE, "columns": _str_list}),
    "checks.distribution": ("distribution", {"severity": SEVERITY, "psi_threshold": _number(0),
                                             "max_mean_shift_std": _number(0), "columns": _str_list}),
    "checks.ml_drift": ("ml_drift", {"severity": SEVERITY, "max_auc": _number(0.5, 1), "max_rows": _int(MIN_ML_ROWS),
                                     "exclude": _str_list}),
    "report": ("report", {"markdown": _nullable(_str), "max_examples": _int(), "show_values": _bool}),
    "discovery": ("discovery", {"enabled": _bool, "root": _str, "scope": _choice("readers", "all"),
                                "exclude": _str_list}),
    "ai": ("ai", {"enabled": _bool, "model": _str, "api_key_env": _str}),
}
TOP = {"baseline": _str, "fail_on": _choice(*FAIL_ON_CHOICES), "max_categories": _int(1)}


def _apply(target, raw: dict, path: str, spec: dict) -> None:
    for key, check in spec.items():
        if key not in raw:
            continue
        value = raw[key]
        if value is None:
            if getattr(check, "nullable", False):
                setattr(target, key, None)
            continue
        setattr(target, key, check(value, f"{path}.{key}" if path else key))


def parse_config(raw) -> Config:
    top = _mapping(raw, "<root>", {"version", "checks", "consumers", *TOP, *SECTIONS})
    if top.get("version", 1) != 1:
        _fail(f"unsupported 'version' {top.get('version')!r} (this release supports 1)")
    cfg = Config()
    _apply(cfg, top, "", TOP)
    checks = _mapping(top.get("checks"), "checks", {p.split(".")[1] for p in SECTIONS if p.startswith("checks.")})
    for path, (attr, spec) in SECTIONS.items():
        parent, _, name = path.rpartition(".")
        raw_section = (checks if parent else top).get(name)
        _apply(getattr(cfg, attr), _mapping(raw_section, path, spec), path, spec)

    cons = top.get("consumers")
    if cons is not None and not isinstance(cons, dict):
        _fail("'consumers' must be a mapping of consumer name -> definition")
    for name, spec in (cons or {}).items():
        p = f"consumers.{name}"
        spec = _mapping(spec, p, {"type", "columns", "owner", "description"})
        if "columns" not in spec:
            _fail(f"'{p}.columns' is required")
        cfg.consumers.append(Consumer(name=str(name), type=_str(spec.get("type", "other"), f"{p}.type"),
                                      columns=_str_list(spec["columns"], f"{p}.columns"),
                                      owner=str(spec.get("owner", "")), description=str(spec.get("description", ""))))
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
