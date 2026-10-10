"""Optional AI explanations and rule suggestions.

Privacy: only aggregate statistics, column names and detected-issue metrics are
sent. Raw rows, category values and example values are never included.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .config import Config
from .errors import DataPulseError
from .models import CheckReport

DEFAULT_URL = "https://api.anthropic.com/v1/messages"

EXPLAIN_SYSTEM = (
    "You are a data engineer helping triage data-quality regressions. You receive aggregate statistics and "
    "detected issues about a dataset (never raw rows). For each issue give a short, hedged root-cause hypothesis "
    "and one concrete next step. These are hypotheses, not facts. Respond with ONLY JSON: "
    '{"explanations": [{"index": <int>, "text": <string>}]}'
)
SUGGEST_SYSTEM = (
    "You are a data engineer. Given an aggregate profile of a dataset (never raw rows), suggest additional "
    "validation rules worth adding, each with a one-line reason. Be concise and say which suggestions are "
    "judgement calls. Plain text, at most 8 bullet points."
)


def get_api_key(cfg: Config) -> Optional[str]:
    return os.environ.get(cfg.ai.api_key_env) or None


def build_explain_payload(report: CheckReport) -> Dict[str, Any]:
    return {
        "dataset": {"baseline_rows": report.baseline_rows, "current_rows": report.current_rows,
                    "current_columns": report.current_columns},
        "issues": [
            {"index": n, "check": i.check, "severity": i.severity, "columns": i.columns,
             "measured": {k: v for k, v in i.measured.items() if isinstance(v, (int, float, bool, str)) or v is None},
             "consumer_types": sorted({c["type"] for c in i.consumers})}
            for n, i in enumerate(report.issues)
        ],
    }


def build_suggest_payload(profile: Dict[str, Any]) -> Dict[str, Any]:
    cols = {}
    for name, info in profile["columns"].items():
        slim = {k: v for k, v in info.items() if k not in ("categories", "numeric")}
        if "categories" in info:
            counts = list(info["categories"].values())
            slim["category_count"] = len(counts)
            slim["largest_category_share"] = round(max(counts) / sum(counts), 3)
        if "numeric" in info:
            slim["numeric"] = {k: v for k, v in info["numeric"].items() if k not in ("bin_edges", "bin_props")}
        cols[name] = slim
    return {"rows": profile["rows"], "duplicate_rows": profile["duplicate_rows"], "columns": cols}


def _post(url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: int = 45) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise DataPulseError(f"AI request failed with HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise DataPulseError(f"AI request failed: {e}")


def _call(cfg: Config, key: str, system: str, user: Dict[str, Any]) -> str:
    url = os.environ.get("DATAPULSE_AI_URL", DEFAULT_URL)
    headers = {"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"}
    body = {"model": cfg.ai.model, "max_tokens": 1500, "system": system,
            "messages": [{"role": "user", "content": json.dumps(user)}]}
    data = _post(url, headers, body)
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    if not text.strip():
        raise DataPulseError("AI response was empty")
    return text


def explain_issues(report: CheckReport, cfg: Config) -> str:
    """Attach AI explanations to issues. Returns a status line for the user ('' if nothing to say)."""
    key = get_api_key(cfg)
    if not key:
        return f"AI explanations requested but ${cfg.ai.api_key_env} is not set; using deterministic explanations."
    if not report.issues:
        return ""
    try:
        text = _call(cfg, key, EXPLAIN_SYSTEM, build_explain_payload(report))
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
        for item in data["explanations"]:
            idx = int(item["index"])
            if 0 <= idx < len(report.issues):
                report.issues[idx].ai_explanation = str(item["text"]).strip()
    except (DataPulseError, ValueError, KeyError, TypeError) as e:
        return f"AI explanations unavailable ({e}); using deterministic explanations."
    return "AI explanations added (generated from aggregate statistics only; unverified)."


def suggest_with_ai(profile: Dict[str, Any], cfg: Config) -> str:
    key = get_api_key(cfg)
    if not key:
        raise DataPulseError(f"--ai needs the ${cfg.ai.api_key_env} environment variable to be set.")
    return _call(cfg, key, SUGGEST_SYSTEM, build_suggest_payload(profile)).strip()
