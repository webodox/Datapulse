# DataPulse

An AI data-quality engineer for your pipelines. DataPulse catches data changes that would break dashboards,
services or ML models, then does the on-call work: it investigates, says whether to block the release, ranks the
likely causes, writes the fix checklist and drafts the message to the people affected.

- **Rules** (schema, nulls, row counts, duplicates, categories, distributions) run offline and deterministically.
- **An ML drift model** learns to tell new rows from trusted rows and names the columns that changed, including
  shifts in how columns relate to each other that no single-column rule can see. Runs locally, no API key.
- **An AI triage agent** (Claude, with tools) investigates each failure and writes the incident note.
- **A rules assistant** turns "order_id must be unique and alert if amount shifts" into a validated `datapulse.yml`.

## AI and ML features

| Feature | What it takes off your plate | Where | Needs |
|---|---|---|---|
| ML drift model (`ml_drift` check) | Spotting subtle or multi-column drift and pointing at the columns responsible | Dashboard (automatic), `check --reference good.csv` | nothing; runs locally |
| AI triage agent | The first hour of an incident: root cause, block-or-ship call, fix steps, who to tell, the message | Dashboard **Triage with AI**, `check --triage` | `ANTHROPIC_API_KEY` |
| AI repair agent | The fix itself: rescales units, converts text back to numbers, removes duplicates, standardizes spellings; then re-runs every check to prove it worked | Dashboard **Fix with AI** (download the repaired copy), `datapulse fix data.csv -o fixed.csv` | `ANTHROPIC_API_KEY` |
| Before/after charts | Seeing a shift instantly: each column's value ranges and empty rates, baseline vs. new file | Dashboard, on each issue | nothing |
| Rules assistant | Writing and debugging YAML; drafts are validated and self-corrected before you see them | Dashboard Rules tab, `datapulse draft-rules "..."` | `ANTHROPIC_API_KEY` |
| Issue explanations | A hedged hypothesis per issue | `check --ai`, dashboard toggle | `ANTHROPIC_API_KEY` |

**How the drift model works.** It samples up to `max_rows` rows from each file and trains a random forest to
predict which file a row came from. Its out-of-bag ROC AUC is the drift score: 0.5 means the files are
indistinguishable, 1.0 means completely different. Above `max_auc` (default 0.7) it reports an issue naming the
columns the model relied on (feature importance), and those columns map to your downstream consumers like any
other issue. Identifier columns (all-unique values) and timestamps are skipped automatically, because they differ
between any two batches; add more to `checks.ml_drift.exclude`.

**How the triage agent works.** Claude gets read-only tools: `list_issues`, `get_column_stats` (baseline vs.
current aggregates), `get_run_history` and `get_consumers`. It decides what to look at, then returns a strict JSON
triage (verdict `block` / `investigate` / `safe_to_ship`, ranked hypotheses with confidence and evidence, actions
with owners, who to notify, a ready-to-send message). The dashboard shows the steps it took. Its output is labelled
AI-generated and never changes a check's pass/fail result.

**How the repair agent works.** It can only use a fixed set of safe repair operations (rename, rescale, convert to
numbers, remove duplicates, fill or drop empty values, drop a column, standardize spellings), never arbitrary code.
It applies them one at a time to a copy and re-checks after each, and it is told not to invent data (a missing
column or blank identifiers are left for a person). Its operations are logged and replayed onto the original to
produce the repaired file, then every check runs again, so "9 issues -> 5" is measured. The original is never
changed.

**Privacy.** Every AI feature sends column names, aggregate statistics and issue metrics only. Raw rows, category
values and example values are never sent; tests enforce this for each feature. The ML model never leaves your machine.

**Setting the key.** CLI: `export ANTHROPIC_API_KEY=...` (PowerShell: `$env:ANTHROPIC_API_KEY="..."`). Docker:
copy `.env.example` to `.env`, put your key in it, then `docker compose up --build`. The model is `ai.model` in
`datapulse.yml` (default `claude-sonnet-5-5`); `DATAPULSE_AI_URL` points the client at a proxy.

## Recognizing projects in any language

DataPulse does not assume a Python project. `datapulse scan` and `datapulse check --discover` recognize the
ecosystems and source languages in a repository and find the code that reads your data file and references its
columns, so reports can say *which files* a data change may break, whatever language they are written in.

```bash
datapulse scan examples/data/orders.csv --root examples/polyglot           # what reads this dataset?
datapulse scan examples/data/orders.csv --root examples/polyglot -o consumers.yml   # reviewable consumers YAML

datapulse baseline examples/data/orders.csv -c examples/datapulse.yml
datapulse check examples/data/orders_drifted.csv -c examples/datapulse.yml \
  --discover --scan-root examples/polyglot
```

| Recognized | How |
|---|---|
| **Languages** (by file extension) | Python, Jupyter notebooks, JavaScript, TypeScript, Java, Kotlin, Scala, Go, Rust, Ruby, R, C#, PHP, SQL, Julia, Swift |
| **Ecosystems** (by manifest file) | Node.js, TypeScript, Go modules, Cargo, Maven, Gradle, sbt, Bundler, Composer, pyproject/requirements, Julia, SwiftPM, R, .NET, dbt |

How it finds things (plain text search, no parsing):
1. A file is a **reader** if it mentions the data file's name or path (or, for SQL, the table name = file stem).
   Both the file being checked and the file the baseline was built from are searched for.
2. In readers, a column is **referenced** when it appears as a quoted string (`"status"`), an attribute
   (`row.status`, `df$status`, `:status` in Ruby), a bare word in SQL, or a camelCase/PascalCase spelling
   (`customer_id` -> `customerId`, `CustomerId`, `getCustomerId()`) in Java, Go, C#, JS and similar.
3. Excluded by default: dot-directories, `node_modules`, `venv`, `target`, `build`, `dist`, `vendor`, `obj`.
   Add more with `--exclude` or `discovery.exclude`. `--scope all` also reports non-reader files that match.

**These are leads, not facts.** The reports label them "detected by text search; heuristic, not verified". It
cannot follow data passed between modules, and misses dynamically built column names (`row[name]`). Generic
names (`id`, `status`) can match unrelated code. `scan -o` writes suggested `consumers:` to a **new** file and
never edits your `datapulse.yml`. Configuration lives in the `discovery:` section of `datapulse.yml`.

## Web dashboard and REST API

The same checks also run as a small self-hosted service: a React dashboard plus a FastAPI server that
stores projects, baselines and run history in SQLite. A project's rules are a normal `datapulse.yml`,
validated exactly like the CLI's, and each run returns the same JSON report as `datapulse check --json`.

```bash
docker compose up --build        # dashboard: http://localhost:8080   API docs: http://localhost:8000/docs
```

Without Docker (Python 3.10+, Node.js 20+):

```bash
pip install -e ".[web]"
uvicorn server.app:app --reload --port 8000
cd web && npm install && npm run dev    # http://localhost:5173 (proxies /api to port 8000)
```

In the dashboard: create a project (or **Try it with sample data**), drop a known-good file to save the baseline, then drop each new version to check it; the check starts as soon as the file lands. Each project has four tabs: **Checks** (results and history), **Baseline** (column profile, replace it), **Rules** (the project's `datapulse.yml`, plus `datapulse suggest` output to copy from; nothing is applied) and **Send data** (the ingest API). Uploads accept CSV, JSON (an array of row objects), JSONL/NDJSON and Parquet, up to 25 MB.

**Sending data from any language.** Creating a project returns an API key once (only its hash is stored).
Any service that can make an HTTP request can send rows:

```bash
curl -X POST http://localhost:8000/api/projects/PROJECT_ID/ingest \
  -H 'Content-Type: application/json' -H 'X-DataPulse-Key: PROJECT_API_KEY' \
  -d '{"dataset_name": "orders", "role": "candidate",
       "rows": [{"order_id": 1, "customer_id": "C0001", "amount": 22.5, "status": "pending"}]}'
```

Send `"role": "baseline"` first (each re-baseline bumps `baseline_version`), then `candidate` batches; each
request takes up to 50,000 rows. The response is the JSON report (`status`, `issues`, `affected_consumers`, ...).
`"use_ai": true` adds AI explanations when `ANTHROPIC_API_KEY` is set for the server (see `.env.example`).

Data is stored under `DATAPULSE_HOME` (default `.datapulse/`): uploaded files, baselines, reports and accounts.

### Running it for your team

Run **one** shared copy and have everyone open it, rather than each person running their own. Everyone then sees
the same projects, checks and AI triage, and only that one server needs an `ANTHROPIC_API_KEY`.

1. On a machine everyone can reach (a server, a cloud VM, or one teammate's computer on the office network), put
   the key in `.env` and run `docker compose up -d --build`.
2. Open `http://<that-machine>:8080`. The first person to open a fresh install creates the **admin** account.
3. The admin adds teammates under **Account & team** (name, email, temporary password). Teammates sign in and can
   change their password there.
4. On a failed check, anyone can set **Open / Acknowledged / Resolved**; the dashboard shows who did it and when,
   who ran each check, and who requested each AI triage.

### Deploying to the cloud (one permanent link for everyone)

`Dockerfile.app` packages the dashboard and the API into one container on one port, with all data (accounts,
projects, checks) on a volume at `/data`. `railway.json` configures [Railway](https://railway.com) to use it:

1. In Railway: **New Project → Deploy from GitHub repo** → pick this repository (and the branch to deploy).
2. On the service, open **Variables** and add `ANTHROPIC_API_KEY` (your key) and `DATAPULSE_SECURE_COOKIES=1`.
   Add `ANTHROPIC_WORKSPACE_ID` only if your key isn't tied to a workspace.
3. Right-click the service → **Attach volume**, mount path `/data`. Without it, data is lost on every redeploy.
4. **Settings → Networking → Generate Domain**. Open that `https://...up.railway.app` link; the first person to open
   it creates the admin account, then adds teammates under **Account & team**.

Every push to the deployed branch redeploys automatically. Any other host that runs a Dockerfile and offers a
persistent volume works the same way (Fly.io, Render with a disk, a VM with `docker run -p 80:8000 -v dp:/data`).

Every `/api` route needs a signed-in user except `/api/health`, sign-in itself, and `/ingest`, which uses the
project's API key so other services can send data. Passwords are stored as scrypt hashes; sessions are HttpOnly
cookies that last 14 days. If the dashboard is reachable from the internet, serve it over HTTPS (for example behind
a reverse proxy) and set `DATAPULSE_SECURE_COOKIES=1`.

## Running it without Python

DataPulse itself needs no Python in your project: it reads CSV/Parquet and YAML and adds nothing to your
dependency tree. Choose how to run it:

| You use... | Run DataPulse with | Needs on your machine |
|---|---|---|
| **GitHub Actions, no Python at all** | `uses: your-org/datapulse@v1` with `binary-url:` (see "Standalone executable") | nothing; a prebuilt executable is downloaded |
| **GitHub Actions** (any language) | `uses: your-org/datapulse@v1` (see below) | nothing; the action installs itself into an isolated virtualenv |
| **Any machine or CI, no Python** | download `datapulse-<os>-<arch>` from Releases | nothing |
| **Any CI or laptop** | Docker image: `docker run --rm -v "$PWD":/work datapulse check data.csv` | Docker |
| **Developers who just want the CLI** | `pipx install .` or `uvx --from . datapulse ...` | pipx or uv |
| **A Python project** | `pip install -e ".[dev]"` | Python 3.10+ |
| **A team that wants a dashboard / REST API** | `docker compose up --build` (see "Web dashboard and REST API") | Docker |

Scripts in any language can read results from the exit code (`0` / `1` / `2`) or from the JSON report
(`datapulse check data.csv --json report.json`; fields: `status`, `exit_code`, `counts`, `rows`, and per-issue
`check`, `severity`, `columns`, `measured`, `suggested_explanation`, `consumers`, `code_references`; plus top-level `config`, `schema_changed`, `affected_consumers`). The schema is versioned
(`schema_version: 1`).

Examples for other stacks are in `examples/workflows/`: `node.yml`, `go.yml` (monorepo with `working-directory`),
`java.yml`, and `docker.yml` (non-GitHub CI).

## Standalone executable (no Python needed)

DataPulse is built as a single executable with its own Python runtime bundled (about 180 MB, because it includes
pandas, Parquet support and scikit-learn). Teams in Go, Node, Java, Rust, .NET, etc. can run it without installing Python.

```bash
# use a release build
chmod +x datapulse-linux-x64 && ./datapulse-linux-x64 check data/orders.csv

# or build it yourself on your OS (inside a virtualenv)
python -m venv .venv && source .venv/bin/activate
bash scripts/build_binary.sh            # -> dist/datapulse (dist/datapulse.exe on Windows)
```

`.github/workflows/release.yml` builds Linux, macOS (arm64 and x64) and Windows executables with SHA-256
checksums and attaches them to each GitHub Release. Use them in Actions without Python:

```yaml
- uses: your-org/datapulse@v1
  with:
    data-file: data/orders.csv
    binary-url: https://github.com/your-org/datapulse/releases/download/v0.1.0/datapulse-linux-x64
    binary-sha256: <contents of datapulse-linux-x64.sha256>   # the step fails on a mismatch
```

Notes: the executable still needs `git` on `PATH` for `--baseline-ref` (GitHub runners have it). Builds are
per-OS and per-architecture, and a Linux build links against the glibc of the machine that built it, so the
release workflow builds on standard runners.

## Install

From a source checkout (Python 3.10+; other options in the table above):

```bash
git clone <this-repo> datapulse && cd datapulse
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
datapulse --version
```

In another project, any language: `pipx install /path/to/datapulse` (or `pipx install git+https://github.com/<you>/datapulse`).
Docker: `docker build -t datapulse .` then `docker run --rm --user "$(id -u):$(id -g)" -v "$PWD":/work datapulse check data/orders.csv`.

## Quick start (sample data included)

```bash
# 1. Create a versioned baseline from a known-good file
datapulse baseline examples/data/orders.csv --config examples/datapulse.yml

# 2. Check the same file: passes, exit code 0
datapulse check examples/data/orders.csv --config examples/datapulse.yml

# 3. Check a drifted file: reports issues, exit code 1
datapulse check examples/data/orders_drifted.csv --config examples/datapulse.yml
echo $?    # 1

# 4. Get reviewable rule suggestions (never edits your config)
datapulse suggest examples/data/orders.csv --output suggested.yml

# 5. Run the full test suite
datapulse test
```

## Using it in your own project

```bash
datapulse init                          # writes a commented datapulse.yml (never overwrites without --force)
datapulse baseline data/orders.csv      # writes .datapulse/baseline.json; commit it
datapulse check data/orders.csv         # terminal report + datapulse-report.md
```

Re-running `datapulse baseline` after an intentional data change bumps `baseline_version` and replaces the file.

## Commands

| Command | Purpose |
|---|---|
| `datapulse init` | Write a starter `datapulse.yml`. |
| `datapulse baseline <file> [-c CONFIG] [-o PATH]` | Profile a known-good file into a versioned baseline JSON. |
| `datapulse check <file> [-c CONFIG] [--baseline PATH] [--baseline-ref REF] [--config-ref REF] [--markdown PATH] [--json PATH] [--discover] [--scan-root DIR] [--reference FILE] [--triage] [--ai]` | Run all checks. `--reference` (the known-good data file) adds the ML drift check; `--triage` runs the AI agent on failures. |
| `datapulse scan <file> [--root DIR] [--scope readers\|all] [--exclude DIR] [-o FILE]` | Recognize languages in a repo and find code that reads the data file. |
| `datapulse fix <file> -o FILE [--reference FILE]` | AI repair agent: writes a repaired copy to a **new** file and re-checks it. |
| `datapulse draft-rules "<request>" [--data FILE] [-o FILE]` | Plain-English request -> complete, validated `datapulse.yml` (AI). Prints it or writes a **new** file. |
| `datapulse suggest <file> [-o FILE] [--force] [--ai]` | Propose rules from the profile. Prints YAML or writes a **new** file; refuses to touch your config. |
| `datapulse github-outputs <report.json>` | Print `status`, `issue-count`, `schema-changed`, `affected-consumers` as `key=value` lines for `$GITHUB_OUTPUT` (values are sanitized). |
| `datapulse comment <report.md> [--repo OWNER/NAME] [--pr N]` | Create/update the PR comment (uses `GITHUB_TOKEN`). |
| `datapulse test` | Run the DataPulse test suite (source checkout with `.[dev]`). |

**Exit codes:** `0` passed · `1` issues at or above `fail_on` · `2` bad file, config, baseline, or environment.

## Checks

| Check (`datapulse.yml` key) | Detects | Key settings |
|---|---|---|
| `schema` | Missing columns, added columns, type changes | severity per kind |
| `row_count` | Row count change vs. baseline, minimum rows | `max_change_pct`, `min_rows` |
| `null_rate` | Null-rate increase (absolute) | `max_increase`, per-column `columns` |
| `categorical` | Values not seen in the baseline (low-cardinality text columns) | `max_unexpected_rate`, `columns` |
| `duplicates` | Duplicate key values or whole rows | `columns`, `max_duplicate_rate` |
| `distribution` | Numeric shift: Population Stability Index (PSI) and mean shift in baseline std units | `psi_threshold`, `max_mean_shift_std`, `columns` |
| `ml_drift` | Learned drift: a model separates baseline rows from new rows (needs the baseline's rows) | `max_auc`, `max_rows`, `exclude` |

Severities are `critical`, `warning`, `info`, or `off`. `fail_on` (`critical` by default; also `warning`, `info`,
`never`) decides which severity produces exit code 1.

Behaviour notes:
- A whole-number float column (what pandas produces when an integer column gains nulls) counts as `integer`, so a
  few nulls do not look like a type change. Float to integer narrowing is not reported.
- Categorical values are only tracked for text columns with at most `max_categories` (default 50) distinct values.
- Duplicates by key columns are absolute; whole-row duplicates are compared with the baseline's duplicate rate.
- Sequential identifier columns (all-unique integers) are skipped by `distribution` unless named in `columns`.

See `examples/datapulse.yml` for a complete configuration.

## Downstream consumers

```yaml
consumers:
  revenue_dashboard:
    type: dashboard
    owner: finance-analytics
    columns: [amount, created_at]
```

When a column fails, every consumer listing it is shown. Dataset-wide issues (row count, whole-row duplicates)
list all consumers. Reports separate **Measured from the data** (facts) from **Possible explanation (suggested,
not verified)**.

## GitHub Action

`action.yml` is a composite action. On pull requests it:

1. fetches the target branch and reads **both the baseline and `datapulse.yml` from `origin/<base>`**, so a PR cannot pass by re-baselining its own data or by loosening its own thresholds (rule changes take effect once merged; set `config-from-base: "false"` to opt out),
2. runs `datapulse check` on the changed file,
3. posts or updates one PR comment (identified by a hidden marker) and writes the report to the job summary,
4. fails the job if issues reach `fail_on`.

Setup:

1. Commit `datapulse.yml` and `.datapulse/baseline.json` to your default branch.
2. Copy `examples/workflows/datapulse.yml` to `.github/workflows/datapulse.yml` and set `uses:` to the repository
   hosting this action.

```yaml
permissions:
  contents: read
  pull-requests: write
steps:
  - uses: actions/checkout@v4
  - uses: your-org/datapulse@v1
    with:
      data-file: data/orders.csv
```

Inputs: `data-file` (required), `config`, `baseline`, `baseline-ref`, `config-from-base`, `working-directory`, `discover`, `setup-python`,
`binary-url`, `binary-sha256`, `python-version`, `comment`, `ai`, `github-token`. Outputs: `status` (`passed`/`failed`/`error`), `issue-count`, `schema-changed` (`true`/`false`), `affected-consumers` (comma-separated),
`exit-code`, `report-markdown`, `report-json`. Use them to gate later steps:

```yaml
- id: dq
  uses: your-org/datapulse@v1
  with: { data-file: data/orders.csv }
- if: always() && steps.dq.outputs.schema-changed == 'true'
  run: echo "Schema changed; notify: ${{ steps.dq.outputs.affected-consumers }}"
```

The PR comment and report start with a **Pull request impact** section (schema changes and affected consumers).
If a ref's config is missing, the check says so and uses the working-tree config; if the ref itself was not
fetched it fails with exit code 2 rather than silently trusting the PR.

The action is language-agnostic: it installs DataPulse into its own virtualenv under `$RUNNER_TEMP` (it never
touches your project's dependencies or lockfiles), writes its reports outside your working tree, and accepts
`working-directory` for monorepos. Set `setup-python: "false"` to use the runner's own `python3` instead of
`actions/setup-python`, which otherwise puts a Python on `PATH` for the rest of the job.
The default `github.token` is used; no tokens are stored. On fork PRs the token is read-only, so the comment step
is skipped without failing the job and the report remains in the job summary.

## AI explanations from the CLI

```bash
export ANTHROPIC_API_KEY=...        # name configurable via ai.api_key_env
datapulse check data.csv --ai       # or ai.enabled: true in datapulse.yml
datapulse suggest data.csv --ai
```

- Sent: column names, aggregate statistics, and numeric metrics of detected issues.
- Never sent: raw rows, category values, or example values (a test enforces this).
- AI text is labelled "AI-generated, unverified". With no key, or if the call fails, the deterministic explanations are used.
- `DATAPULSE_AI_URL` overrides the endpoint (for a proxy). `report.show_values: false` also hides example values from reports.

Datasets are never uploaded anywhere by DataPulse. Baselines contain aggregates plus the category labels of
low-cardinality text columns, so avoid baselining files where such labels are sensitive.

## Development

```bash
pip install -e ".[dev,web]"   # web is only needed for tests/test_server.py
datapulse test          # or: pytest -q
cd web && npm install && npm run build   # type-check and build the dashboard
python examples/generate_samples.py   # regenerate sample data
```
