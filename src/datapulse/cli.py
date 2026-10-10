"""DataPulse command line interface."""
import functools
import json
import os
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.text import Text

from . import __version__
from .agent import TriageContext, run_triage
from .ai import draft_rules, explain_issues, suggest_with_ai
from .baseline import build_baseline, describe, load_baseline, parse_baseline, save_baseline
from .checks import run_checks
from .config import DEFAULT_CONFIG_NAME, STARTER_CONFIG, Config, load_config, parse_config_text
from .errors import DataPulseError
from .github import (default_api_url, detect_pr_context, path_exists_at_ref, read_file_at_ref, ref_exists,
                     upsert_pr_comment)
from .io import load_dataframe
from .profile import profile_dataframe
from .scan import attach_code_references, consumers_yaml, describe_scan, scan_repo
from .report import action_outputs, render_json, render_markdown, render_terminal
from .suggest import render_yaml, suggest_rules

app = typer.Typer(name="datapulse", no_args_is_help=True, add_completion=False,
                  help="Catch data-quality regressions in CSV and Parquet files before they break "
                       "dashboards and models.")


def _version(value: bool):
    if value:
        typer.echo(f"datapulse {__version__}")
        raise typer.Exit()


@app.callback()
def main(version: bool = typer.Option(False, "--version", callback=_version, is_eager=True,
                                      help="Show the version and exit.")):
    pass


def guarded(fn):
    """Turn DataPulseError into a readable message and exit code 2."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DataPulseError as e:
            Console(stderr=True).print(Text(f"error: {e}", style="bold red"), soft_wrap=True)
            raise typer.Exit(2)
    return wrapper


CONFIG_OPT = typer.Option(None, "--config", "-c", help=f"Config file (default: ./{DEFAULT_CONFIG_NAME} if present).")


@app.command("init")
@guarded
def init_cmd(force: bool = typer.Option(False, "--force", help="Overwrite an existing file.")):
    """Write a commented starter datapulse.yml in the current directory."""
    p = Path(DEFAULT_CONFIG_NAME)
    if p.exists() and not force:
        raise DataPulseError(f"{p} already exists. Use --force to overwrite it.")
    p.write_text(STARTER_CONFIG, encoding="utf-8")
    typer.echo(f"Wrote {p}. Edit it, then run: datapulse baseline <data-file>")


@app.command("baseline")
@guarded
def baseline_cmd(data_file: Path = typer.Argument(..., help="Known-good CSV or Parquet file."),
                 config: Optional[Path] = CONFIG_OPT,
                 output: Optional[Path] = typer.Option(None, "--output", "-o",
                                                       help="Baseline path (default: `baseline` in the config).")):
    """Create (or bump the version of) the baseline profile from a known-good file."""
    cfg = load_config(config)
    out = output or Path(cfg.baseline)
    df = load_dataframe(data_file)
    if len(df) == 0:
        raise DataPulseError(f"{data_file} has no rows; a baseline needs data.")
    version = 1
    if out.exists():
        version = parse_baseline(out.read_text(encoding="utf-8"), str(out))["baseline_version"] + 1
    data = build_baseline(df, data_file, version, cfg.max_categories)
    save_baseline(out, data)
    typer.echo(f"Baseline v{version} written to {out} "
               f"({data['profile']['rows']:,} rows, {len(data['profile']['columns'])} columns).")
    if version > 1:
        typer.echo(f"Replaced previous baseline v{version - 1}. Commit {out} so CI can compare against it.")
    else:
        typer.echo(f"Commit {out} to your repository so CI can compare against it.")


def _load_check_config(config, config_ref, console):
    """Load the config from the working tree, or (hardened for CI) from a trusted git ref."""
    if not config_ref:
        cfg = load_config(config)
        if cfg.source is None:
            console.print(Text(f"No {DEFAULT_CONFIG_NAME} found; using default rules.", style="dim"))
        return cfg, cfg.source or "defaults (no datapulse.yml)"
    path = config or Path(DEFAULT_CONFIG_NAME)
    if not ref_exists(config_ref):
        raise DataPulseError(f"Git ref '{config_ref}' was not found, so the trusted config could not be read. "
                             "Fetch it first (git fetch origin <branch>). Refusing to fall back to the "
                             "working-tree config silently.")
    if path_exists_at_ref(config_ref, path):
        label = f"{Path(path).as_posix()} @ {config_ref}"
        return parse_config_text(read_file_at_ref(config_ref, path), label), label
    cfg = load_config(config) if Path(path).exists() else Config()
    console.print(Text(f"{path} does not exist at {config_ref}; using "
                       f"{'the working-tree config' if cfg.source else 'default rules'}. (There is no "
                       "trusted config on that ref to compare against.)", style="yellow"))
    return cfg, f"{cfg.source or 'defaults'} (working tree; none at {config_ref})"


def _triage(report, baseline, df, cfg, console) -> None:
    ctx = TriageContext(json.loads(render_json(report)), baseline["profile"], profile_dataframe(df, cfg.max_categories),
                        consumers=[vars(c) for c in cfg.consumers])
    try:
        with console.status("AI triage agent is investigating…"):
            report.triage = run_triage(ctx, cfg, on_step=lambda tool, args: console.print(
                Text(f"  agent looked at {tool} {json.dumps(args) if args else ''}", style="dim")))
    except DataPulseError as e:
        console.print(Text(f"AI triage unavailable: {e}", style="yellow"))


def _write_report(path: Optional[Path], text: str, kind: str, console: Console) -> None:
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        console.print(Text(f"{kind} report written to {path}", style="dim"))


@app.command("check")
@guarded
def check_cmd(data_file: Path = typer.Argument(..., help="CSV or Parquet file to check."),
              config: Optional[Path] = CONFIG_OPT,
              baseline: Optional[Path] = typer.Option(None, "--baseline", help="Baseline file (overrides config)."),
              baseline_ref: Optional[str] = typer.Option(
                  None, "--baseline-ref", help="Read the baseline from this git ref (e.g. origin/main) "
                                               "instead of the working tree."),
              config_ref: Optional[str] = typer.Option(
                  None, "--config-ref", help="Read datapulse.yml from this git ref too (use the PR's target "
                                             "branch so a PR cannot loosen its own rules)."),
              markdown: Optional[Path] = typer.Option(None, "--markdown", help="Write the Markdown report here."),
              json_out: Optional[Path] = typer.Option(None, "--json", help="Also write a machine-readable JSON report "
                                                                           "(for scripts in any language)."),
              reference: Optional[Path] = typer.Option(
                  None, "--reference", help="The known-good data file itself; enables the ML drift check, which "
                                            "trains a model to tell it apart from the new file (runs locally)."),
              ai: bool = typer.Option(False, "--ai", help="Add AI explanations (aggregates only; needs an API key)."),
              triage: bool = typer.Option(False, "--triage", help="If issues are found, let the AI triage agent "
                                                                  "investigate and write the incident note."),
              discover: bool = typer.Option(False, "--discover", help="Find code in any language that references "
                                                                      "the affected columns (heuristic)."),
              scan_root: Optional[Path] = typer.Option(None, "--scan-root", help="Directory to search (default: "
                                                                                 "discovery.root, i.e. '.').")):
    """Check a data file against its baseline. Exits 1 if issues reach the `fail_on` severity."""
    console = Console()
    cfg, config_label = _load_check_config(config, config_ref, console)
    path = baseline or Path(cfg.baseline)
    if baseline_ref:
        data = parse_baseline(read_file_at_ref(baseline_ref, path), f"{baseline_ref}:{path}")
        label = describe(data, f"{path} @ {baseline_ref}")
    else:
        data = load_baseline(path)
        label = describe(data, str(path))
    df = load_dataframe(data_file)
    report = run_checks(data, df, cfg, data_file=str(data_file), baseline_label=label, config_label=config_label,
                        reference=load_dataframe(reference) if reference else None)
    if discover or cfg.discovery.enabled:
        cols = sorted(set(data["profile"]["columns"]) | set(df.columns))
        src = data.get("source", {}).get("file")
        scan = scan_repo(scan_root or Path(cfg.discovery.root), data_file, cols, cfg.discovery.exclude,
                         cfg.discovery.scope, also_names=[src] if src else [])
        attach_code_references(report, scan)
        console.print(Text("Code discovery: " + describe_scan(scan), style="dim"))
    if ai or cfg.ai.enabled:
        note = explain_issues(report, cfg)
        if note:
            console.print(Text(note, style="dim"))
    if triage and report.issues:
        _triage(report, data, df, cfg, console)
    render_terminal(report, console)
    md = render_markdown(report)
    _write_report(markdown or (Path(cfg.report.markdown) if cfg.report.markdown else None), md, "Markdown", console)
    _write_report(json_out, render_json(report), "JSON", console)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    raise typer.Exit(report.exit_code)


def _write_new(output: Path, text: str, cfg, force: bool) -> None:
    """Write suggestions to a separate file; never to the user's config, never over a file without --force."""
    protected = {Path(DEFAULT_CONFIG_NAME).resolve()} | ({Path(cfg.source).resolve()} if cfg.source else set())
    if output.resolve() in protected:
        raise DataPulseError(f"Refusing to write to your configuration file {output}. Write to a separate "
                             "file and merge the entries you want by hand.")
    if output.exists() and not force:
        raise DataPulseError(f"{output} already exists. Use --force to overwrite it.")
    output.write_text(text, encoding="utf-8")


@app.command("scan")
@guarded
def scan_cmd(data_file: Path = typer.Argument(..., help="The CSV/Parquet file whose readers to look for."),
             root: Path = typer.Option(Path("."), "--root", help="Repository directory to search."),
             scope: str = typer.Option("readers", "--scope", help="readers: only files that mention the data file "
                                                                  "(or SQL table); all: every source file."),
             exclude: Optional[List[str]] = typer.Option(None, "--exclude", help="Extra directory name to skip "
                                                                                 "(repeatable)."),
             config: Optional[Path] = CONFIG_OPT,
             output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write suggested `consumers:` YAML "
                                                                                "to this NEW file."),
             force: bool = typer.Option(False, "--force", help="Allow overwriting an existing --output file.")):
    """Recognize the languages in a repo and find code that reads a data file (heuristic text search)."""
    cfg = load_config(config)
    if scope not in ("readers", "all"):
        raise DataPulseError("--scope must be 'readers' or 'all'.")
    if not root.is_dir():
        raise DataPulseError(f"--root is not a directory: {root}")
    df = load_dataframe(data_file)
    result = scan_repo(root, data_file, list(df.columns), cfg.discovery.exclude + list(exclude or []), scope)
    typer.echo(f"Recognized: {describe_scan(result)}")
    readers = [r for r in result.refs if r.reads_dataset]
    typer.echo(f"\nFiles that read {data_file.name}: {len(readers)}" if readers else
               f"\nNo source file mentions {data_file.name}. (Try --scope all, or check --root.)")
    for r in result.refs:
        why = "reads dataset via " + r.via if r.reads_dataset else "column text match only"
        typer.echo(f"  {r.file} [{r.language}] ({why})")
        for col, lines in r.columns.items():
            typer.echo(f"      '{col}' at line {', '.join(map(str, lines))}")
    text = consumers_yaml(result)
    if output:
        _write_new(output, text, cfg, force)
        typer.echo(f"\nSuggested consumers written to {output}. Review, then merge into {cfg.source or DEFAULT_CONFIG_NAME}.")
    else:
        typer.echo("\n" + text.rstrip())


@app.command("suggest")
@guarded
def suggest_cmd(data_file: Path = typer.Argument(..., help="CSV or Parquet file to learn rules from."),
                config: Optional[Path] = CONFIG_OPT,
                output: Optional[Path] = typer.Option(None, "--output", "-o",
                                                      help="Write the suggested YAML to this NEW file."),
                force: bool = typer.Option(False, "--force", help="Allow overwriting an existing --output file."),
                ai: bool = typer.Option(False, "--ai", help="Also ask the AI for extra ideas (aggregates only).")):
    """Propose validation rules from the data profile. Never edits your datapulse.yml."""
    cfg = load_config(config)
    df = load_dataframe(data_file)
    if len(df) == 0:
        raise DataPulseError(f"{data_file} has no rows; nothing to learn rules from.")
    profile = profile_dataframe(df, cfg.max_categories)
    text = render_yaml(suggest_rules(profile))
    if output:
        _write_new(output, text, cfg, force)
        typer.echo(f"Suggested rules written to {output}. Review them, then merge what you want into "
                   f"{cfg.source or DEFAULT_CONFIG_NAME}.")
    else:
        typer.echo(text)
        typer.echo("# Nothing was applied. Copy the blocks you want into datapulse.yml.")
    if ai:
        typer.echo("\nAI-generated suggestions (unverified; generated from aggregate statistics only):")
        typer.echo(suggest_with_ai(profile, cfg))


@app.command("draft-rules")
@guarded
def draft_rules_cmd(request: str = typer.Argument(..., help='What you want, in plain English, e.g. "order_id must '
                                                             'be unique and alert if amount shifts".'),
                    data_file: Optional[Path] = typer.Option(None, "--data", help="Data file, so the AI knows the "
                                                                                  "real column names (aggregates only)."),
                    config: Optional[Path] = CONFIG_OPT,
                    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write the draft to this NEW file."),
                    force: bool = typer.Option(False, "--force", help="Allow overwriting an existing --output file.")):
    """Turn a plain-English request into a validated datapulse.yml (AI). Never edits your config."""
    from .config import STARTER_CONFIG
    cfg = load_config(config)
    current = Path(cfg.source).read_text(encoding="utf-8") if cfg.source else STARTER_CONFIG
    profile = profile_dataframe(load_dataframe(data_file), cfg.max_categories) if data_file else None
    with Console(stderr=True).status("Drafting rules…"):
        draft = draft_rules(request, current, profile, cfg)
    if output:
        _write_new(output, draft["config_yaml"], cfg, force)
        typer.echo(f"Draft written to {output}. Review it, then replace {cfg.source or DEFAULT_CONFIG_NAME} if you like it.")
    else:
        typer.echo(draft["config_yaml"])
    typer.echo(f"# What changed: {draft['summary']}")
    for item in draft["not_supported"]:
        typer.echo(f"# Not expressible as a rule: {item}")


@app.command("fix")
@guarded
def fix_cmd(data_file: Path = typer.Argument(..., help="The data file that failed its checks."),
            output: Path = typer.Option(..., "--output", "-o", help="Where to write the repaired copy (a NEW file)."),
            config: Optional[Path] = CONFIG_OPT,
            baseline: Optional[Path] = typer.Option(None, "--baseline", help="Baseline file (overrides config)."),
            reference: Optional[Path] = typer.Option(None, "--reference", help="The known-good data file, so the "
                                                                               "ML drift check is re-run too."),
            force: bool = typer.Option(False, "--force", help="Allow overwriting an existing --output file.")):
    """Let the AI repair agent fix a copy of a file, then re-check it (AI). Never changes the original."""
    from .fix import RepairSession, run_repair
    console = Console()
    cfg = load_config(config)
    if output.resolve() == data_file.resolve():
        raise DataPulseError("--output must be a new file; the original is never overwritten.")
    session = RepairSession(load_dataframe(data_file), load_baseline(baseline or Path(cfg.baseline)), cfg,
                            reference=load_dataframe(reference) if reference else None)
    with console.status("AI repair agent is working…"):
        result = run_repair(session, on_step=lambda tool, args: console.print(
            Text(f"  agent: {tool} {json.dumps(args) if args else ''}", style="dim")))
    fixed = result.pop("fixed_frame")
    _write_new(output, fixed.to_csv(index=False), cfg, force)
    console.print(Text(f"\nIssues: {result['before']['total']} -> {result['after']['total']} "
                       f"(rows {result['rows']['before']:,} -> {result['rows']['after']:,}). {result['summary']}", style="bold"))
    for op in result["operations"]:
        console.print(Text(f"  fixed: {op['effect']}  ({op['reason']})"))
    for item in result["not_fixed"]:
        console.print(Text(f"  left for a person: {item['issue']}: {item['why']}", style="yellow"))
    console.print(Text(f"Repaired copy written to {output}. Review it before using it.", style="dim"))


@app.command("github-outputs")
@guarded
def github_outputs_cmd(report_json: Path = typer.Argument(..., help="JSON report from `datapulse check --json`.")):
    """Print `key=value` lines (status, issue-count, schema-changed, affected-consumers) for $GITHUB_OUTPUT."""
    if not report_json.exists():
        raise DataPulseError(f"Report not found: {report_json}. Run `datapulse check --json {report_json}` first.")
    try:
        doc = json.loads(report_json.read_text(encoding="utf-8"))
    except ValueError as e:
        raise DataPulseError(f"{report_json} is not valid JSON: {e}")
    for key, value in action_outputs(doc).items():
        typer.echo(f"{key}={value}")


@app.command("comment")
@guarded
def comment_cmd(report_file: Path = typer.Argument(..., help="Markdown report from `datapulse check`."),
                repo: Optional[str] = typer.Option(None, "--repo", help="owner/name (default: $GITHUB_REPOSITORY)."),
                pr: Optional[int] = typer.Option(None, "--pr", help="Pull request number (default: from the event).")):
    """Create or update the DataPulse comment on a pull request (uses $GITHUB_TOKEN)."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise DataPulseError("GITHUB_TOKEN is not set. In Actions pass `env: GITHUB_TOKEN: ${{ github.token }}`.")
    if not report_file.exists():
        raise DataPulseError(f"Report not found: {report_file}. Run `datapulse check` first.")
    env_repo, env_pr = detect_pr_context(os.environ)
    repo, pr = repo or env_repo, pr or env_pr
    if not repo or not pr:
        raise DataPulseError("Could not determine the repository and PR number; pass --repo and --pr.")
    action = upsert_pr_comment(report_file.read_text(encoding="utf-8"), repo, pr, token, default_api_url())
    typer.echo(f"PR comment {action} on {repo}#{pr}.")


@app.command("test")
@guarded
def test_cmd():
    """Run DataPulse's own test suite (needs a source checkout and `pip install -e '.[dev]'`)."""
    tests = Path(__file__).resolve().parents[2] / "tests"
    if not tests.is_dir():
        raise DataPulseError("Test suite not found. Run this from a source checkout installed with "
                             "`pip install -e '.[dev]'`.")
    try:
        import pytest
    except ImportError:
        raise DataPulseError("pytest is not installed. Run: pip install -e '.[dev]'")
    raise typer.Exit(int(pytest.main([str(tests), "-q"])))
