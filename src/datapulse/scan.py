"""Language-agnostic discovery of code that reads a dataset and references its columns.

This is a heuristic *text search*, not a parser or data-flow analysis. Results are leads to review,
never verified facts: dynamic column names are missed and generic names can produce false positives.
"""
from __future__ import annotations

import bisect
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

LANGUAGES = {
    ".py": "Python", ".ipynb": "Jupyter Notebook",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala",
    ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".r": "R", ".cs": "C#", ".php": "PHP",
    ".sql": "SQL", ".jl": "Julia", ".swift": "Swift",
}
MANIFESTS = {
    "package.json": "Node.js (JavaScript/TypeScript)", "tsconfig.json": "TypeScript", "go.mod": "Go",
    "Cargo.toml": "Rust", "pom.xml": "Java (Maven)", "build.gradle": "JVM (Gradle)",
    "build.gradle.kts": "JVM (Gradle)", "build.sbt": "Scala (sbt)", "Gemfile": "Ruby",
    "composer.json": "PHP", "pyproject.toml": "Python", "requirements.txt": "Python", "setup.py": "Python",
    "Project.toml": "Julia", "Package.swift": "Swift", "DESCRIPTION": "R", "dbt_project.yml": "dbt (SQL)",
}
DEFAULT_EXCLUDE = {"node_modules", "venv", "env", "target", "build", "dist", "__pycache__", "vendor", "obj"}
MAX_FILES = 20000
MAX_BYTES = 1_000_000
MAX_LINES = 3


@dataclass
class CodeRef:
    file: str
    language: str
    reads_dataset: bool
    via: str
    columns: Dict[str, List[int]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"file": self.file, "language": self.language, "reads_dataset": self.reads_dataset,
                "via": self.via, "columns": self.columns}


@dataclass
class ScanResult:
    root: str
    ecosystems: List[str]
    languages: Dict[str, int]
    files_scanned: int
    truncated: bool
    refs: List[CodeRef]


def language_slug(language: str) -> str:
    return language.lower().replace("#", "sharp").replace("+", "p").replace(" ", "-")


def _variants(col: str) -> List[str]:
    """camelCase / PascalCase spellings of a column, for Java, Go, C#, JS and similar code."""
    parts = [p for p in re.split(r"[_\-\s]+", col) if p]
    if not parts:
        return []
    camel = parts[0][:1].lower() + parts[0][1:] + "".join(p[:1].upper() + p[1:] for p in parts[1:])
    pascal = "".join(p[:1].upper() + p[1:] for p in parts)
    return [v for v in dict.fromkeys((camel, pascal)) if v != col and len(v) >= 3 and re.fullmatch(r"\w+", v)]


_CACHE: Dict[tuple, "re.Pattern[str]"] = {}


def _column_regex(col: str, kind: str):
    key = (col, kind)
    if key not in _CACHE:
        esc = re.escape(col)
        alts = [rf'''\\?["'`]{esc}\\?["'`]''', rf"[.$@]{esc}(?![A-Za-z0-9_])"]
        if kind == "ruby":
            alts.append(rf"(?<![A-Za-z0-9_:]):{esc}(?![A-Za-z0-9_])")
        if kind == "sql":
            alts.append(rf"(?<![A-Za-z0-9_]){esc}(?![A-Za-z0-9_])")
        for v in _variants(col):
            alts.append(rf"(?<![A-Za-z0-9_])(?:get|set)?{re.escape(v)}(?![A-Za-z0-9_])")
        _CACHE[key] = re.compile("|".join(alts), re.I if kind == "sql" else 0)
    return _CACHE[key]


def _find_columns(text: str, columns: Iterable[str], kind: str) -> Dict[str, List[int]]:
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    hits: Dict[str, List[int]] = {}
    for col in columns:
        lines: List[int] = []
        for m in _column_regex(col, kind).finditer(text):
            ln = bisect.bisect_right(starts, m.start())
            if ln not in lines:
                lines.append(ln)
            if len(lines) >= MAX_LINES:
                break
        if lines:
            hits[col] = lines
    return hits


def _reads(text: str, language: str, names: Sequence[str], stems: Sequence[str]) -> str:
    if any(n in text for n in names):
        return "file name"
    if language == "SQL" and any(
            re.search(rf"(?<![A-Za-z0-9_]){re.escape(st)}(?![A-Za-z0-9_])", text, re.I) for st in stems):
        return "table name"
    return ""


def scan_repo(root, data_file, columns: Sequence[str], exclude: Iterable[str] = (),
              scope: str = "readers", also_names: Sequence[str] = ()) -> ScanResult:
    """Search `root` for code that reads `data_file` (or any file named in `also_names`, e.g. the file
    the baseline was built from, which may differ from the file being checked)."""
    root = Path(root).resolve()
    names, stems = [], []
    for cand in (str(data_file), *also_names):
        c = Path(cand)
        try:
            rel = c.resolve().relative_to(root).as_posix()
        except ValueError:
            rel = c.name
        names += [c.name, rel]
        stems.append(c.stem)
    names, stems = list(dict.fromkeys(names)), list(dict.fromkeys(stems))
    skip = DEFAULT_EXCLUDE | set(exclude)
    ecosystems, langs, refs = set(), Counter(), []
    scanned, truncated = 0, False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip and not d.startswith("."))
        for fn in sorted(filenames):
            if fn in MANIFESTS:
                ecosystems.add(MANIFESTS[fn])
            elif fn.endswith((".csproj", ".sln")):
                ecosystems.add(".NET (C#)")
            elif fn.endswith(".Rproj"):
                ecosystems.add("R")
            language = LANGUAGES.get(Path(fn).suffix.lower())
            if not language:
                continue
            path = Path(dirpath) / fn
            try:
                if path.stat().st_size > MAX_BYTES:
                    continue
                if scanned >= MAX_FILES:
                    truncated = True
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            scanned += 1
            langs[language] += 1
            via = _reads(text, language, names, stems)
            if scope == "readers" and not via:
                continue
            kind = "sql" if language == "SQL" else "ruby" if language == "Ruby" else "other"
            hits = _find_columns(text, columns, kind)
            if via or hits:
                refs.append(CodeRef(path.relative_to(root).as_posix(), language, bool(via),
                                    via or "column text match", hits))
    return ScanResult(str(root), sorted(ecosystems), dict(langs.most_common()), scanned, truncated, refs)


def describe_scan(scan: ScanResult) -> str:
    langs = ", ".join(f"{n} {l}" for l, n in scan.languages.items()) or "no recognized source files"
    eco = f"; ecosystems: {', '.join(scan.ecosystems)}" if scan.ecosystems else ""
    more = " (stopped at the file limit)" if scan.truncated else ""
    return f"scanned {scan.files_scanned} source files ({langs}){eco}{more}"


def attach_code_references(report, scan: ScanResult, max_files: int = 8) -> None:
    """Add detected code references to each issue (a separate, labelled, heuristic section)."""
    for issue in report.issues:
        found = []
        for ref in scan.refs:
            if issue.columns:
                cols = {c: ls for c, ls in ref.columns.items() if c in issue.columns}
                if cols:
                    found.append({"file": ref.file, "language": ref.language, "via": ref.via, "columns": cols})
            elif ref.reads_dataset:
                found.append({"file": ref.file, "language": ref.language, "via": ref.via, "columns": {}})
        issue.code_refs = found[:max_files]
        issue.code_refs_more = max(0, len(found) - max_files)


def consumers_yaml(scan: ScanResult) -> str:
    import yaml
    consumers = {
        r.file: {"type": f"{language_slug(r.language)}-code",
                 "description": f"Detected by `datapulse scan` ({r.via}); heuristic, please review",
                 "columns": sorted(r.columns)}
        for r in scan.refs if r.reads_dataset and r.columns
    }
    head = ("# Consumers detected by `datapulse scan` (heuristic text search; review before use).\n"
            "# Merge the entries you trust into the `consumers:` section of datapulse.yml.\n"
            "# Nothing has been applied.\nversion: 1\n")
    return head + (yaml.safe_dump({"consumers": consumers}, sort_keys=False) if consumers else "consumers: {}\n")
