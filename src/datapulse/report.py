"""Terminal and Markdown reports."""
from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.text import Text

from . import __version__
from .models import CheckReport, Issue

MARKER = "<!-- datapulse-report -->"
STYLE = {"critical": "bold red", "warning": "yellow", "info": "cyan"}
EMOJI = {"critical": "🔴", "warning": "🟡", "info": "🔵"}


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _label(key: str) -> str:
    return key.replace("_", " ")


def _consumer_line(c: dict) -> str:
    meta = c["type"] + (f", owner: {c['owner']}" if c["owner"] else "")
    if c["dataset_wide"]:
        return f"{c['name']} ({meta}): reads this file; the issue is dataset-wide"
    return f"{c['name']} ({meta}): uses {', '.join(c['columns'])}"


def _code_ref_line(r: dict, md: bool = False) -> str:
    q = (lambda x: f"`{x}`") if md else (lambda x: f"'{x}'")
    what = "; ".join(f"{q(c)} line {', '.join(map(str, ls))}" for c, ls in r["columns"].items())
    via = r["via"]
    return f"{q(r['file']) if md else r['file']} ({r['language']}): {what or f'reads this dataset (matched {via})'}"


def _summary_line(report: CheckReport) -> str:
    return ", ".join(f"{n} {s}" for s, n in report.counts().items() if n) or "no issues"


def render_terminal(report: CheckReport, console: Console) -> None:
    def out(text: str = "", style: str = "") -> None:
        console.print(Text(text, style=style))

    head = Text("DataPulse check: ", style="bold")
    head.append("FAILED" if report.failed else "PASSED", style="bold red" if report.failed else "bold green")
    console.print(head)
    out(f"Data file: {report.data_file}")
    out(f"Baseline:  {report.baseline_label}")
    out(f"Config:    {report.config_label}")
    out(f"Rows:      {report.baseline_rows:,} (baseline) -> {report.current_rows:,} (current), "
        f"{report.current_columns} columns")
    out(f"Issues:    {_summary_line(report)}  (fail_on: {report.fail_on})")
    for n, issue in enumerate(report.issues, 1):
        out()
        t = Text(f"[{issue.severity.upper()}] ", style=STYLE[issue.severity])
        t.append(f"{n}. {issue.title}", style="bold")
        t.append(f"   ({issue.check})", style="dim")
        console.print(t)
        out("  Measured: " + issue.detail)
        for k, v in issue.measured.items():
            out(f"    - {_label(k)}: {_fmt(v)}", "dim")
        if issue.examples:
            out("  Examples from the data: " + "; ".join(issue.examples), "dim")
        out("  Possible explanation (suggested, not verified): " + issue.explanation, "italic")
        if issue.ai_explanation:
            out("  AI-generated explanation (suggested, not verified): " + issue.ai_explanation, "italic magenta")
        if issue.code_refs:
            out("  Code that references this (detected by text search; heuristic, not verified):")
            for r in issue.code_refs:
                out("    - " + _code_ref_line(r))
            if issue.code_refs_more:
                out(f"    ...and {issue.code_refs_more} more file(s)", "dim")
        if issue.consumers:
            out("  Downstream consumers that may be affected:")
            for c in issue.consumers:
                out("    * " + _consumer_line(c))
        elif issue.columns:
            out("  Downstream consumers: none of the configured consumers use this column.", "dim")
    out()
    if report.passed_checks:
        out("Passed checks: " + ", ".join(report.passed_checks), "green")
    if report.triage:
        out()
        for line in triage_lines(report.triage):
            out(line, "magenta" if line.startswith("AI triage") else "")


def _md(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: CheckReport) -> str:
    counts = report.counts()
    icon = "❌" if report.failed else ("⚠️" if report.issues else "✅")
    title = ("DataPulse: checks failed" if report.failed else
             "DataPulse: passed with issues" if report.issues else "DataPulse: all checks passed")
    L = [MARKER, f"## {icon} {title}", "",
         "| | |", "|---|---|",
         f"| Data file | `{report.data_file}` |",
         f"| Baseline | {_md(report.baseline_label)} |",
         f"| Config | {_md(report.config_label)} |",
         f"| Rows | {report.baseline_rows:,} → {report.current_rows:,} ({report.current_columns} columns) |",
         f"| Issues | {_summary_line(report)} |",
         f"| CI gate | fails on `{report.fail_on}` → **{'FAILED' if report.failed else 'PASSED'}** |", ""]
    L += ["### Pull request impact", ""]
    if report.schema_issues:
        for i in report.schema_issues:
            cols = ", ".join(f"`{c}`" for c in i.columns)
            L.append(f"- **{i.severity.upper()}** {cols}: {_md(i.title)}")
    else:
        L.append("No schema changes detected.")
    names = report.affected_consumers
    L += ["", "**Affected consumers:** " + (", ".join(f"`{n}`" for n in names) if names else "none mapped"), ""]
    if report.issues:
        L += ["### Issues", "", "| Severity | Check | Column | Measured | Affected consumers |", "|---|---|---|---|---|"]
        for i in report.issues:
            cols = ", ".join(f"`{c}`" for c in i.columns) or "(dataset)"
            cons = ", ".join(c["name"] for c in i.consumers) or "none"
            L.append(f"| {EMOJI[i.severity]} {i.severity} | `{i.check}` | {cols} | {_md(i.detail)} | {_md(cons)} |")
        L += ["", "### Details", ""]
        for n, i in enumerate(report.issues, 1):
            L += [f"#### {EMOJI[i.severity]} {n}. {i.title}", "", "**Measured from the data**", "", f"{i.detail}", ""]
            L += [f"- {_label(k)}: `{_fmt(v)}`" for k, v in i.measured.items()]
            if i.examples:
                L += ["", "Examples from the data: " + "; ".join(f"`{e}`" for e in i.examples)]
            L += ["", "**Possible explanation** _(suggested, not verified)_", "", f"{i.explanation}", ""]
            if i.ai_explanation:
                L += ["**AI-generated explanation** _(suggested, not verified)_", "", i.ai_explanation, ""]
            if i.code_refs:
                L += ["**Code that references this** _(detected by text search; heuristic, not verified)_", ""]
                L += [f"- {_code_ref_line(r, md=True)}" for r in i.code_refs]
                if i.code_refs_more:
                    L.append(f"- _...and {i.code_refs_more} more file(s)_")
                L.append("")
            if i.consumers:
                L += ["**Downstream consumers that may be affected**", ""]
                L += [f"- {_consumer_line(c)}" for c in i.consumers]
                L.append("")
            elif i.columns:
                L += ["_No configured consumer uses this column._", ""]
    if report.passed_checks:
        L += ["### Checks that passed", "", ", ".join(f"`{c}`" for c in report.passed_checks), ""]
    if report.triage:
        t = report.triage
        L += ["### AI triage _(AI-generated from aggregate statistics; verify before acting)_", "",
              f"**{_md(t['headline'])}**", "", f"Verdict: **{VERDICT[t['verdict']]}**. {t['verdict_reason']}", ""]
        L += ["**Likely causes**", ""] + [f"- ({c['confidence']}) {c['hypothesis']} _Evidence: {c['evidence']}_"
                                          for c in t["root_causes"]] + [""]
        L += ["**Next steps**", ""] + [f"- [ ] {a['step']} ({a['owner']})" for a in t["actions"]] + [""]
        if t["notify"]:
            L += ["**Notify**", ""] + [f"- {n['who']}: {n['why']}" for n in t["notify"]] + [""]
        L += ["**Message to send**", "", "> " + t["message"].replace("\n", "\n> "), ""]
    L += ["---", "_Numbers above are measured from aggregate profile statistics. Explanations are suggestions "
          "and are not verified. No dataset rows were uploaded anywhere._", ""]
    return "\n".join(L)


VERDICT = {"block": "Block this batch", "investigate": "Investigate before shipping", "safe_to_ship": "Safe to ship"}


def triage_lines(t: dict) -> list:
    lines = [f"AI triage (AI-generated, verify before acting): {t['headline']}",
             f"  Verdict: {VERDICT[t['verdict']]}. {t['verdict_reason']}", "  Likely causes:"]
    lines += [f"    - ({c['confidence']}) {c['hypothesis']}" for c in t["root_causes"]]
    lines += ["  Next steps:"] + [f"    [ ] {a['step']} ({a['owner']})" for a in t["actions"]]
    lines += ["  Notify:"] + [f"    - {n['who']}: {n['why']}" for n in t["notify"]] if t["notify"] else []
    return lines + ["  Message to send:"] + ["    " + ln for ln in t["message"].splitlines()]


def render_json(report: CheckReport) -> str:
    """Machine-readable report for tooling in any language (stable, versioned schema)."""
    doc = {
        "schema_version": 1,
        "tool": "datapulse",
        "version": __version__,
        "status": "failed" if report.failed else "passed",
        "exit_code": report.exit_code,
        "fail_on": report.fail_on,
        "data_file": report.data_file,
        "baseline": report.baseline_label,
        "config": report.config_label,
        "schema_changed": report.schema_changed,
        "affected_consumers": report.affected_consumers,
        "rows": {"baseline": report.baseline_rows, "current": report.current_rows},
        "columns_current": report.current_columns,
        "counts": report.counts(),
        "checks_run": report.checks_run,
        "passed_checks": report.passed_checks,
        "triage": report.triage,
        "issues": [
            {"check": i.check, "severity": i.severity, "title": i.title, "columns": i.columns,
             "measured_summary": i.detail, "measured": i.measured, "examples": i.examples,
             "suggested_explanation": i.explanation, "ai_explanation": i.ai_explanation,
             "consumers": i.consumers, "code_references": i.code_refs}
            for i in report.issues
        ],
    }
    return json.dumps(doc, indent=2, default=str) + "\n"


def action_outputs(doc: dict) -> dict:
    """Single-line values for GitHub Actions step outputs, derived from a JSON report.

    Values are sanitized (no control characters or newlines) so they can never inject extra
    `key=value` lines into $GITHUB_OUTPUT, even if a consumer name in the config is hostile.
    """
    def clean(value: Any) -> str:
        return " ".join("".join(ch if ch.isprintable() else " " for ch in str(value)).split())

    issues = doc.get("issues", [])
    consumers = sorted({str(c.get("name", "")) for i in issues for c in i.get("consumers", [])})
    return {
        "status": clean(doc.get("status", "error")),
        "issue-count": str(len(issues)),
        "schema-changed": "true" if any(str(i.get("check", "")).startswith("schema.") for i in issues) else "false",
        "affected-consumers": clean(", ".join(consumers)),
    }
