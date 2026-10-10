"""Claude-powered features: issue explanations, rule suggestions, and plain-English rule drafting.

Privacy: only aggregate statistics, column names and detected-issue metrics are sent. Raw rows, category
values and example values never are (tests enforce this). The triage agent lives in `agent.py`.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .config import Config, parse_config_text
from .errors import DataPulseError
from .models import CheckReport

# Models that accept server-side refusal fallbacks ("default" routes by refusal category).
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

EXPLAIN_SYSTEM = (
    "You are a data engineer helping triage data-quality regressions. You receive aggregate statistics and "
    "detected issues about a dataset (never raw rows). For each issue give a short, hedged root-cause hypothesis "
    "and one concrete next step. These are hypotheses, not facts."
)
SUGGEST_SYSTEM = (
    "You are a data engineer. Given an aggregate profile of a dataset (never raw rows), suggest additional "
    "validation rules worth adding, each with a one-line reason. Be concise and say which suggestions are "
    "judgement calls. Plain text, at most 8 bullet points."
)
RULES_SYSTEM = """You edit DataPulse configuration files (datapulse.yml) from plain-English requests.

Return the complete updated file. Keep every existing setting and comment the request doesn't touch. Only use \
keys DataPulse supports - anything else fails validation:
- top level: version (1), baseline, fail_on (critical|warning|info|never), max_categories, checks, consumers, \
report, discovery, ai
- checks.schema: missing_columns, added_columns, type_changes (each a severity)
- checks.row_count: severity, max_change_pct (percent), min_rows
- checks.null_rate: severity, max_increase (0-1, absolute), columns (map column -> max increase 0-1)
- checks.categorical: severity, max_unexpected_rate (0-1), columns (list) - flags values not seen in the baseline
- checks.duplicates: severity, max_duplicate_rate (0-1), columns (key columns that must be unique)
- checks.distribution: severity, psi_threshold, max_mean_shift_std, columns (list)
- checks.ml_drift: severity, max_auc (0.5-1), max_rows, exclude (list)
- consumers: map name -> {type, owner, description, columns (list, required)}
Severities: critical | warning | info | off. Use only column names that exist in the dataset profile. If part of \
the request can't be expressed with these keys (e.g. an explicit allowed-values list or a value range), leave it \
out and list it under `not_supported` with the closest supported alternative."""

EXPLAIN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["explanations"],
    "properties": {"explanations": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["index", "text"],
        "properties": {"index": {"type": "integer"}, "text": {"type": "string"}}}}},
}
RULES_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["config_yaml", "summary", "not_supported"],
    "properties": {"config_yaml": {"type": "string"}, "summary": {"type": "string"},
                   "not_supported": {"type": "array", "items": {"type": "string"}}},
}


def get_api_key(cfg: Config) -> Optional[str]:
    # Keys pasted into a host's settings often pick up a trailing space or newline, which makes every request
    # fail as an illegal header ("Connection error"). Trim it.
    return (os.environ.get(cfg.ai.api_key_env) or "").strip() or None


def get_client(cfg: Config):
    """An Anthropic client, or a DataPulseError that says how to enable AI features."""
    key = get_api_key(cfg)
    if not key:
        raise DataPulseError(f"AI features need the ${cfg.ai.api_key_env} environment variable to be set.")
    import anthropic

    base_url = os.environ.get("DATAPULSE_AI_URL")  # e.g. a company proxy in front of the Anthropic API
    if base_url and base_url.rstrip("/").endswith("/v1/messages"):  # older docs gave the full endpoint
        base_url = base_url.rstrip("/")[: -len("/v1/messages")]
    # Organization-level keys must name a workspace on every request; workspace-scoped keys don't need this.
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    return anthropic.Anthropic(api_key=key, base_url=base_url or None, timeout=120.0, max_retries=2,
                               default_headers={"anthropic-workspace-id": workspace} if workspace else None)


def create(client, cfg: Config, **params):
    """One Messages API call with refusal fallback, normalised errors and stop-reason checks."""
    import anthropic

    if cfg.ai.model in FALLBACK_MODELS:
        params.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    try:
        response = client.beta.messages.create(model=cfg.ai.model, max_tokens=16000, **params)
    except anthropic.AuthenticationError:
        raise DataPulseError(f"The API key in ${cfg.ai.api_key_env} was rejected.")
    except anthropic.RateLimitError:
        raise DataPulseError("The AI service is rate limiting requests; try again in a minute.")
    except anthropic.APIStatusError as e:
        raise DataPulseError(f"AI request failed with HTTP {e.status_code}: {e.message}")
    except anthropic.APIConnectionError as e:
        cause = f" ({e.__cause__})" if e.__cause__ else ""
        raise DataPulseError(f"Could not reach the AI service: {e}{cause}")
    if response.stop_reason == "refusal":
        raise DataPulseError("The AI declined this request.")
    if response.stop_reason == "max_tokens":
        raise DataPulseError("The AI response was cut off before it finished.")
    return response


def text_of(response) -> str:
    text = "".join(b.text for b in response.content if b.type == "text")
    if not text.strip():
        raise DataPulseError("AI response was empty")
    return text


def json_format(schema: Dict[str, Any]) -> Dict[str, Any]:
    return {"format": {"type": "json_schema", "schema": schema}}


def slim_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    """A profile without category values or histogram internals: safe to send."""
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
    return {"rows": profile["rows"], "duplicate_rows": profile.get("duplicate_rows", 0), "columns": cols}


build_suggest_payload = slim_profile


def safe_measured(measured: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in measured.items() if isinstance(v, (int, float, bool, str)) or v is None}


def build_explain_payload(report: CheckReport) -> Dict[str, Any]:
    return {
        "dataset": {"baseline_rows": report.baseline_rows, "current_rows": report.current_rows,
                    "current_columns": report.current_columns},
        "issues": [{"index": n, "check": i.check, "severity": i.severity, "columns": i.columns,
                    "measured": safe_measured(i.measured), "consumer_types": sorted({c["type"] for c in i.consumers})}
                   for n, i in enumerate(report.issues)],
    }


def explain_issues(report: CheckReport, cfg: Config) -> str:
    """Attach AI explanations to issues. Returns a status line for the user ('' if nothing to say)."""
    if not get_api_key(cfg):
        return f"AI explanations requested but ${cfg.ai.api_key_env} is not set; using deterministic explanations."
    if not report.issues:
        return ""
    try:
        response = create(get_client(cfg), cfg, system=EXPLAIN_SYSTEM, output_config=json_format(EXPLAIN_SCHEMA),
                          messages=[{"role": "user", "content": json.dumps(build_explain_payload(report))}])
        for item in json.loads(text_of(response))["explanations"]:
            if 0 <= int(item["index"]) < len(report.issues):
                report.issues[int(item["index"])].ai_explanation = str(item["text"]).strip()
    except (DataPulseError, ValueError, KeyError, TypeError) as e:
        return f"AI explanations unavailable ({e}); using deterministic explanations."
    return "AI explanations added (generated from aggregate statistics only; unverified)."


def suggest_with_ai(profile: Dict[str, Any], cfg: Config) -> str:
    response = create(get_client(cfg), cfg, system=SUGGEST_SYSTEM,
                      messages=[{"role": "user", "content": json.dumps(slim_profile(profile))}])
    return text_of(response).strip()


def draft_rules(request: str, config_yaml: str, profile: Optional[Dict[str, Any]], cfg: Config,
                client=None, attempts: int = 2) -> Dict[str, Any]:
    """Turn a plain-English request into a complete, validated datapulse.yml. Nothing is saved.

    If the draft fails DataPulse's own validation, the error goes back to the model for one more try.
    """
    client = client or get_client(cfg)
    context = {"request": request, "current_config_yaml": config_yaml,
               "dataset_profile": slim_profile(profile) if profile else "no baseline yet; column names unknown"}
    messages: List[Dict[str, Any]] = [{"role": "user", "content": json.dumps(context)}]
    error = ""
    for _ in range(attempts):
        response = create(client, cfg, system=RULES_SYSTEM, output_config=json_format(RULES_SCHEMA),
                          messages=messages)
        try:
            draft = json.loads(text_of(response))
            parse_config_text(draft["config_yaml"], "AI draft")
            return {"config_yaml": draft["config_yaml"], "summary": draft["summary"],
                    "not_supported": draft["not_supported"]}
        except (ValueError, KeyError, DataPulseError) as e:
            error = str(e)
            messages += [{"role": "assistant", "content": response.content},
                         {"role": "user", "content": f"That draft failed validation: {error}\nReturn a corrected draft."}]
    raise DataPulseError(f"The AI could not produce a valid configuration ({error}).")
