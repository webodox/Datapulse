import pytest

from datapulse.config import load_config, parse_config
from datapulse.errors import DataPulseError


def test_defaults():
    cfg = parse_config({})
    assert cfg.fail_on == "critical" and cfg.null_rate.max_increase == 0.05 and cfg.consumers == []


def test_valid_config_values_are_parsed():
    cfg = parse_config({
        "fail_on": "warning",
        "checks": {"null_rate": {"max_increase": 0.1, "columns": {"id": 0}}, "row_count": {"min_rows": 10},
                   "schema": {"added_columns": "off"}},
        "consumers": {"m": {"type": "model", "columns": ["a"]}},
    })
    assert cfg.fail_on == "warning" and cfg.null_rate.columns == {"id": 0.0}
    assert cfg.row_count.min_rows == 10 and cfg.schema.added_columns == "off"
    assert cfg.consumers[0].name == "m"


@pytest.mark.parametrize("raw,needle", [
    ({"fail_on": "sometimes"}, "fail_on"),
    ({"checks": {"null_rate": {"severity": "fatal"}}}, "checks.null_rate.severity"),
    ({"checks": {"null_rate": {"max_increase": "lots"}}}, "must be a number"),
    ({"checks": {"null_rate": {"max_increase": 5}}}, "<= 1"),
    ({"checks": {"nulls": {}}}, "unknown key"),
    ({"consumers": {"x": {"type": "model"}}}, "columns"),
    ({"consumers": ["x"]}, "mapping"),
    ({"report": {"show_values": "yes"}}, "true or false"),
    ({"version": 2}, "version"),
])
def test_invalid_config_has_actionable_errors(raw, needle):
    with pytest.raises(DataPulseError) as e:
        parse_config(raw)
    assert needle in str(e.value)


def test_yaml_syntax_error(tmp_path):
    p = tmp_path / "datapulse.yml"
    p.write_text("a: [unclosed")
    with pytest.raises(DataPulseError, match="not valid YAML"):
        load_config(p)
