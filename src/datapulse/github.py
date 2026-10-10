"""Git and GitHub helpers: baseline-from-ref and PR comments (stdlib only)."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from .errors import DataPulseError
from .report import MARKER

MAX_COMMENT = 60000


def _git(args, cwd=None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    except FileNotFoundError:
        raise DataPulseError("git is not installed, but --baseline-ref needs it.")


def _repo_relative(path):
    top = _git(["rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        raise DataPulseError("Reading files from a git ref requires running inside a git repository.")
    root = Path(top.stdout.strip())
    try:
        return root, Path(path).resolve().relative_to(root.resolve())
    except ValueError:
        raise DataPulseError(f"{path} is outside the git repository at {root}.")


def ref_exists(ref: str) -> bool:
    return _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"]).returncode == 0


def path_exists_at_ref(ref: str, path) -> bool:
    root, rel = _repo_relative(path)
    return _git(["cat-file", "-e", f"{ref}:{rel.as_posix()}"], cwd=root).returncode == 0


def read_file_at_ref(ref: str, path) -> str:
    root, rel = _repo_relative(path)
    res = _git(["show", f"{ref}:{rel.as_posix()}"], cwd=root)
    if res.returncode != 0:
        raise DataPulseError(
            f"Could not read '{rel.as_posix()}' at git ref '{ref}': {res.stderr.strip()}. "
            "Is the baseline committed on that branch, and has the ref been fetched "
            "(git fetch origin <branch>)?")
    return res.stdout


def _request(method: str, url: str, token: str, payload: Optional[dict] = None) -> Any:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "datapulse"}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        hint = " (fork PRs get a read-only token; the report is still in the job summary)" if e.code == 403 else ""
        raise DataPulseError(f"GitHub API {method} failed with HTTP {e.code}{hint}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise DataPulseError(f"GitHub API request failed: {e}")


def detect_pr_context(env: Mapping[str, str]) -> Tuple[Optional[str], Optional[int]]:
    repo = env.get("GITHUB_REPOSITORY")
    pr = None
    event = env.get("GITHUB_EVENT_PATH")
    if event and Path(event).exists():
        try:
            data = json.loads(Path(event).read_text(encoding="utf-8"))
            pr = (data.get("pull_request") or {}).get("number") or data.get("number")
        except (OSError, ValueError):
            pass
    return repo, pr


def upsert_pr_comment(body: str, repo: str, pr: int, token: str, api_url: str = "https://api.github.com") -> str:
    if MARKER not in body:
        body = MARKER + "\n" + body
    if len(body) > MAX_COMMENT:
        body = body[:MAX_COMMENT] + "\n\n_...report truncated; see the Markdown report artifact or job summary._"
    base = f"{api_url.rstrip('/')}/repos/{repo}"
    existing = None
    page = 1
    while existing is None:
        comments = _request("GET", f"{base}/issues/{pr}/comments?per_page=100&page={page}", token) or []
        existing = next((c for c in comments if MARKER in (c.get("body") or "")), None)
        if len(comments) < 100:
            break
        page += 1
    if existing:
        _request("PATCH", f"{base}/issues/comments/{existing['id']}", token, {"body": body})
        return "updated"
    _request("POST", f"{base}/issues/{pr}/comments", token, {"body": body})
    return "created"


def default_api_url() -> str:
    return os.environ.get("GITHUB_API_URL", "https://api.github.com")
