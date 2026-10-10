from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import LEVEL


@dataclass
class Issue:
    check: str
    severity: str
    title: str
    detail: str  # measured facts only
    columns: List[str] = field(default_factory=list)
    measured: Dict[str, Any] = field(default_factory=dict)  # aggregate scalars only
    examples: List[str] = field(default_factory=list)  # raw data values; never sent to AI
    explanation: str = ""  # deterministic, suggested (not verified)
    consumers: List[Dict[str, Any]] = field(default_factory=list)
    ai_explanation: Optional[str] = None
    code_refs: List[Dict[str, Any]] = field(default_factory=list)  # detected by text search (heuristic)
    code_refs_more: int = 0


@dataclass
class CheckReport:
    data_file: str
    baseline_label: str
    baseline_rows: int
    current_rows: int
    current_columns: int
    issues: List[Issue]
    checks_run: List[str]
    fail_on: str
    max_examples: int = 5
    config_label: str = "defaults"
    triage: Optional[Dict[str, Any]] = None  # AI triage agent output (agent.run_triage), when requested

    @property
    def failed(self) -> bool:
        if self.fail_on == "never":
            return False
        threshold = LEVEL[self.fail_on]
        return any(LEVEL[i.severity] >= threshold for i in self.issues)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def counts(self) -> Dict[str, int]:
        return {s: sum(1 for i in self.issues if i.severity == s) for s in ("critical", "warning", "info")}

    @property
    def schema_issues(self) -> List[Issue]:
        return [i for i in self.issues if i.check.startswith("schema.")]

    @property
    def schema_changed(self) -> bool:
        return bool(self.schema_issues)

    @property
    def affected_consumers(self) -> List[str]:
        return sorted({c["name"] for i in self.issues for c in i.consumers})

    @property
    def passed_checks(self) -> List[str]:
        failing = {i.check.split(".")[0] for i in self.issues}
        return [c for c in self.checks_run if c not in failing]
