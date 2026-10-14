"""AI triage agent: investigates a failed check the way an on-call data engineer would, then writes the
incident note - likely root cause, whether to block the release, fix steps, who to notify, a message to send.

Claude drives the investigation through read-only tools over aggregate data (issues, column statistics, run
history, downstream consumers). Tool results never contain raw rows, category values or example values.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .ai import create, get_client, json_format, safe_measured, slim_profile, text_of
from .config import Config
from .errors import DataPulseError

SYSTEM = """You are the on-call data engineer for a team whose dashboards, models and services read this dataset. \
A data-quality check has just flagged a new batch. Investigate with the tools before concluding: start with \
list_issues, then look at the columns involved, the run history, and who depends on the data.

Then triage it:
- headline: one short sentence (under 25 words) a manager can read.
- verdict_reason: one sentence.
- verdict: "block" if shipping this batch would likely break a consumer or corrupt results, "investigate" if it \
needs a human look first, "safe_to_ship" if the findings are benign.
- root_causes: at most 3 ranked hypotheses tied to evidence you looked up, each one short sentence with one \
short sentence of evidence. Say how sure you are; never present a hypothesis as fact.
- actions: at most 4 concrete next steps of one sentence each, each with an owner taken from the consumer list or a role such as \
"data producer"; never invent people.
- notify: only consumers or owners that appear in the data, and in one sentence why each should hear about it.
- message: a Slack/email message under 70 words the engineer can send as-is.
Be specific and brief. You only ever see aggregates, never rows."""

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["headline", "verdict", "verdict_reason", "root_causes", "actions", "notify", "message"],
    "properties": {
        "headline": {"type": "string"},
        "verdict": {"type": "string", "enum": ["block", "investigate", "safe_to_ship"]},
        "verdict_reason": {"type": "string"},
        "root_causes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["hypothesis", "confidence", "evidence"],
            "properties": {"hypothesis": {"type": "string"}, "evidence": {"type": "string"},
                           "confidence": {"type": "string", "enum": ["low", "medium", "high"]}}}},
        "actions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["step", "owner"],
            "properties": {"step": {"type": "string"}, "owner": {"type": "string"}}}},
        "notify": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["who", "why"],
            "properties": {"who": {"type": "string"}, "why": {"type": "string"}}}},
        "message": {"type": "string"},
    },
}


def _tool(name: str, description: str, properties: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    props = properties or {}
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": props, "required": list(props),
                             "additionalProperties": False}}


TOOLS = [
    _tool("list_issues", "Every issue the checks found: check name, severity, columns, measured numbers and "
                         "which consumers read those columns."),
    _tool("get_column_stats", "Baseline vs. current aggregate statistics for one column: type, null rate, "
                              "distinct values, numeric summary.",
          {"column": {"type": "string", "description": "Exact column name"}}),
    _tool("get_run_history", "Earlier checks of this dataset, newest first: when, pass/fail, which checks and "
                             "columns failed. Use it to tell a one-off from a recurring or worsening problem."),
    _tool("get_consumers", "Downstream dashboards, models and services, their owners and the columns they read."),
]


@dataclass
class TriageContext:
    """What the agent may look at. `report` is a JSON report (render_json); profiles are profile_dataframe output."""
    report: Dict[str, Any]
    baseline_profile: Dict[str, Any]
    current_profile: Optional[Dict[str, Any]] = None
    history: Optional[List[Dict[str, Any]]] = None
    consumers: List[Dict[str, Any]] = field(default_factory=list)

    def list_issues(self) -> Any:
        return [{"check": i["check"], "severity": i["severity"], "title": i["title"], "columns": i["columns"],
                 "measured": safe_measured(i.get("measured", {})),
                 "consumers": [{"name": c["name"], "type": c["type"], "owner": c["owner"]} for c in i["consumers"]]}
                for i in self.report["issues"]]

    def get_column_stats(self, column: str) -> Any:
        side = lambda p: slim_profile(p)["columns"].get(column, "not present") if p else "not available"
        return {"column": column, "baseline": side(self.baseline_profile), "current": side(self.current_profile)}

    def get_run_history(self) -> Any:
        return self.history if self.history is not None else "No run history is available (single CLI run)."

    def get_consumers(self) -> Any:
        return self.consumers or "No consumers are configured for this dataset."


def run_triage(ctx: TriageContext, cfg: Config, client=None, max_turns: int = 10,
               on_step: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    """Run the agent loop. Returns the triage plus `steps`, the tool calls it made (shown to the user)."""
    client = client or get_client(cfg)
    rows = ctx.report.get("rows", {})
    messages: List[Dict[str, Any]] = [{"role": "user", "content": (
        f"The check of {ctx.report.get('data_file', 'a new batch')} {ctx.report.get('status', 'ran')} with "
        f"{len(ctx.report['issues'])} issue(s); rows went from {rows.get('baseline')} (baseline) to "
        f"{rows.get('current')}. Fail threshold: {ctx.report.get('fail_on')}. Investigate and triage it.")}]
    steps: List[Dict[str, Any]] = []
    for _ in range(max_turns):
        response = create(client, cfg, system=SYSTEM, tools=TOOLS, messages=messages,
                          output_config={**json_format(SCHEMA), "effort": "medium"})
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue
        calls = [b for b in response.content if b.type == "tool_use"]
        if not calls:
            try:
                triage = json.loads(text_of(response))
            except ValueError as e:
                raise DataPulseError(f"The AI triage was not valid JSON: {e}")
            return {**triage, "steps": steps, "model": cfg.ai.model}
        results = []
        for call in calls:
            steps.append({"tool": call.name, "input": call.input})
            if on_step:
                on_step(call.name, call.input)
            handler = getattr(ctx, call.name, None) if call.name in {t["name"] for t in TOOLS} else None
            if handler is None:
                results.append({"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                                "content": f"Unknown tool {call.name}"})
                continue
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": json.dumps(handler(**call.input), default=str)})
        messages.append({"role": "user", "content": results})
    raise DataPulseError(f"The AI triage did not finish within {max_turns} steps.")
