"""DataPulse must recognize and search projects written in many languages."""
import json
from pathlib import Path

import pytest

from conftest import make_df
from datapulse.cli import app
from datapulse.config import load_config, parse_config
from datapulse.errors import DataPulseError
from datapulse.scan import scan_repo

FILES = {
    "analytics/report.py": 'import pandas as pd\ndf = pd.read_csv("data/orders.csv")\ntotal = df["amount"].sum()\n',
    "notebooks/explore.ipynb": '{"cells":[{"source":["df = pd.read_csv(\\"data/orders.csv\\")\\n","df[\\"status\\"].value_counts()"]}]}',
    "web/src/dashboard.ts": 'const r = await fetch("/data/orders.csv");\nrows.map(row => row.customerId);\n',
    "web/src/legacy.js": "const f = fs.readFileSync('orders.csv');\nconsole.log(row.country);\n",
    "billing/Loader.java": 'var p = Paths.get("data/orders.csv");\nlong id = rec.getOrderId();\n',
    "svc/main.go": 'f, _ := os.Open("data/orders.csv")\nsum += rec.Amount\n',
    "cli/src/main.rs": 'let f = File::open("data/orders.csv")?;\nlet s = row.get("status");\n',
    "etl/load.rb": 'rows = CSV.read("data/orders.csv")\nq = row[:quantity]\n',
    "stats/model.R": 'df <- read.csv("data/orders.csv")\nmean(df$amount)\n',
    "dotnet/Orders.cs": 'var t = File.ReadAllText("data/orders.csv");\nvar c = row.CustomerId;\n',
    "php/report.php": "<?php $h = fopen('data/orders.csv', 'r');\n$s = $row['status'];\n",
    "sql/revenue.sql": "SELECT country, SUM(amount)\nFROM orders\nGROUP BY country;\n",
    # decoys: must NOT count as readers
    "unrelated/utils.py": 'def total(df):\n    return df["amount"].sum()\n',
    "node_modules/lib/x.js": 'read("orders.csv"); row.amount;\n',
}
MANIFEST_FILES = ["package.json", "go.mod", "Cargo.toml", "pom.xml", "Gemfile", "composer.json", "requirements.txt"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for rel, body in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    for name in MANIFEST_FILES:
        (tmp_path / name).write_text("x")
    (tmp_path / "data").mkdir()
    make_df().to_csv(tmp_path / "data/orders.csv", index=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return tmp_path


COLUMNS = ["order_id", "customer_id", "status", "amount", "quantity", "country"]


def test_recognizes_ecosystems_and_languages(repo):
    r = scan_repo(repo, "data/orders.csv", COLUMNS)
    assert {"Node.js (JavaScript/TypeScript)", "Go", "Rust", "Java (Maven)", "Ruby", "PHP", "Python"} <= set(r.ecosystems)
    assert {"Python", "Jupyter Notebook", "TypeScript", "JavaScript", "Java", "Go", "Rust", "Ruby", "R", "C#",
            "PHP", "SQL"} <= set(r.languages)


def test_finds_readers_and_columns_in_every_language(repo):
    r = scan_repo(repo, "data/orders.csv", COLUMNS)
    by_file = {x.file: x for x in r.refs}
    expected = {
        "analytics/report.py": "amount", "notebooks/explore.ipynb": "status", "web/src/dashboard.ts": "customer_id",
        "web/src/legacy.js": "country", "billing/Loader.java": "order_id", "svc/main.go": "amount",
        "cli/src/main.rs": "status", "etl/load.rb": "quantity", "stats/model.R": "amount",
        "dotnet/Orders.cs": "customer_id", "php/report.php": "status", "sql/revenue.sql": "amount",
    }
    assert set(by_file) == set(expected)  # decoys excluded
    for f, col in expected.items():
        assert by_file[f].reads_dataset and col in by_file[f].columns, f
    assert by_file["sql/revenue.sql"].via == "table name"
    assert by_file["analytics/report.py"].columns["amount"] == [3]  # line numbers are 1-based


def test_scope_all_includes_non_readers_as_text_matches(repo):
    r = scan_repo(repo, "data/orders.csv", COLUMNS, scope="all")
    extra = next(x for x in r.refs if x.file == "unrelated/utils.py")
    assert not extra.reads_dataset and extra.via == "column text match" and "amount" in extra.columns
    assert not any(x.file.startswith("node_modules") for x in r.refs)


def test_scan_command_prints_and_writes_reviewable_consumers(repo, runner):
    r = runner.invoke(app, ["scan", "data/orders.csv"])
    assert r.exit_code == 0, r.output
    assert "Files that read orders.csv: 12" in r.output and "Nothing has been applied" in r.output
    r = runner.invoke(app, ["scan", "data/orders.csv", "-o", "consumers.yml"])
    assert r.exit_code == 0
    cfg = load_config(Path("consumers.yml"))
    names = {c.name: c for c in cfg.consumers}
    assert names["analytics/report.py"].type == "python-code" and names["dotnet/Orders.cs"].type == "csharp-code"
    assert "customer_id" in names["web/src/dashboard.ts"].columns
    assert runner.invoke(app, ["scan", "data/orders.csv", "-o", "consumers.yml"]).exit_code == 2  # no clobber
    (repo / "datapulse.yml").write_text("version: 1\n")
    r = runner.invoke(app, ["scan", "data/orders.csv", "-o", "datapulse.yml", "--force"])
    assert r.exit_code == 2 and "Refusing" in r.output


def test_check_discover_attaches_labelled_code_references(repo, runner):
    assert runner.invoke(app, ["baseline", "data/orders.csv"]).exit_code == 0
    bad = make_df().drop(columns=["country"])
    bad["customer_id"] = None
    bad.to_csv("data/orders.csv", index=False)
    r = runner.invoke(app, ["check", "data/orders.csv", "--discover", "--json", "r.json"])
    assert r.exit_code == 1, r.output
    assert "Code discovery: scanned" in r.output and "heuristic" in r.output
    md = Path("datapulse-report.md").read_text()
    assert "detected by text search; heuristic, not verified" in md
    doc = json.loads(Path("r.json").read_text())
    issues = {(i["check"], tuple(i["columns"])): i for i in doc["issues"]}
    nulls = {x["file"] for x in issues[("null_rate", ("customer_id",))]["code_references"]}
    assert {"web/src/dashboard.ts", "dotnet/Orders.cs"} <= nulls and "analytics/report.py" not in nulls
    missing = {x["file"] for x in issues[("schema.missing_column", ("country",))]["code_references"]}
    assert {"web/src/legacy.js", "sql/revenue.sql"} <= missing  # found via the BASELINE's columns
    assert runner.invoke(app, ["check", "data/orders.csv"]).exit_code == 1


def test_discovery_is_off_by_default(repo, runner):
    runner.invoke(app, ["baseline", "data/orders.csv"])
    make_df().drop(columns=["country"]).to_csv("data/orders.csv", index=False)
    runner.invoke(app, ["check", "data/orders.csv", "--json", "r.json"])
    doc = json.loads(Path("r.json").read_text())
    assert all(i["code_references"] == [] for i in doc["issues"])


def test_discovery_config_is_validated():
    assert parse_config({"discovery": {"enabled": True, "scope": "all", "exclude": ["gen"]}}).discovery.scope == "all"
    with pytest.raises(DataPulseError, match="discovery.scope"):
        parse_config({"discovery": {"scope": "everything"}})


def test_bundled_polyglot_example_works_end_to_end(tmp_path, monkeypatch, runner):
    ex = Path(__file__).resolve().parents[1] / "examples"
    monkeypatch.chdir(tmp_path)
    cfg = str(ex / "datapulse.yml")
    runner.invoke(app, ["baseline", str(ex / "data/orders.csv"), "-c", cfg])
    r = runner.invoke(app, ["check", str(ex / "data/orders_drifted.csv"), "-c", cfg, "--discover",
                            "--scan-root", str(ex / "polyglot")])
    assert r.exit_code == 1
    md = Path("datapulse-report.md").read_text()
    for needle in ("web/src/legacy.js", "sql/revenue.sql", "billing/Loader.java", "heuristic"):
        assert needle in md
