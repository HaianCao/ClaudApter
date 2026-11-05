import json
from unittest.mock import patch

import pytest

from claudapter.config import Config, Provider, load, save
from claudapter.errors import ClaudapterError
from claudapter.gateway_options import (
    catalog_policy_fingerprint,
    diagnostic_report,
    diagnostics_path,
    runtime_options,
    session_options,
    validate_gateway_runtime,
)
from claudapter.route_doctor import route_doctor


def provider():
    return Provider("demo", "Demo", "https://example.test/v1", protocol="chat", execution_mode="gateway")


def test_product_runtime_values_and_diagnostic_paths(tmp_path):
    values = {
        "diagnostics_dir": "diagnostics",
        "token_count_mode": "estimate",
        "prompt_caching": "ephemeral",
        "require_current_evidence": True,
        "evidence_max_age_days": 30,
    }
    item = provider()
    item.gateway_runtime = values
    assert validate_gateway_runtime(values, "gateway") == values
    options = runtime_options(values, "gateway", tmp_path / "config.toml")
    assert "diagnostics_dir" not in options
    assert options["require_current_evidence"] is True
    assert diagnostics_path(tmp_path / "config.toml", item) == tmp_path / "diagnostics" / "demo"
    pytest.importorskip("protoverta")
    from protoverta.observability import RequestObservation

    with session_options(tmp_path / "config.toml", item) as observer:
        observer["observer"](RequestObservation("a" * 32, "responses", "chat", 200, "invalid_stream", 10, True))
    result = diagnostic_report(tmp_path / "config.toml", item)
    assert result["records"][0]["outcome"] == "invalid_stream"
    assert result["enabled"]


def test_same_path_catalog_policy_edit_invalidates_identity(tmp_path):
    item = provider()
    item.gateway_runtime = {"catalog_policy_file": "catalog.json"}
    path = tmp_path / "catalog.json"
    path.write_text("first", encoding="utf-8")
    before = catalog_policy_fingerprint(tmp_path / "config.toml", item)
    path.write_text("second", encoding="utf-8")
    assert catalog_policy_fingerprint(tmp_path / "config.toml", item) != before


def test_route_doctor_no_live_flag_never_invokes_provider(tmp_path):
    pytest.importorskip("protoverta")
    item = provider()
    config = Config(providers={"demo": item}, path=tmp_path / "config.toml")
    with patch("claudapter.route_doctor._gateway", return_value=(object(), None)), patch("protoverta.certification.check_route") as checked:
        result = route_doctor(config, item, "m")
    checked.assert_not_called()
    assert result[0]["requests"] == 0 and not result[0]["passed"]
    assert "not_tested" in json.dumps(result)


def test_unavailable_optional_diagnostics_cannot_block_session(tmp_path):
    pytest.importorskip("protoverta")
    item = provider()
    item.gateway_runtime = {"diagnostics_dir": "ordinary-file"}
    path = tmp_path / "ordinary-file"
    path.write_text("sentinel", encoding="utf-8")
    with session_options(tmp_path / "config.toml", item) as options:
        assert options == {}
    assert path.read_text(encoding="utf-8") == "sentinel"


def test_runtime_directory_with_non_bmp_unicode_round_trips_toml(tmp_path):
    item = provider()
    item.gateway_runtime = {"diagnostics_dir": "diagnostics_🧪"}
    config = Config(providers={"demo": item}, path=tmp_path / "config.toml")
    save(config)
    assert load(config.path).providers["demo"].gateway_runtime == item.gateway_runtime


def test_openrouter_pin_runtime_round_trip_and_invalid_value(tmp_path):
    item = provider()
    item.gateway_runtime = {"openrouter_provider": "fixture/endpoint"}
    config = Config(providers={"demo": item}, path=tmp_path / "config.toml")
    save(config)
    restored = load(config.path).providers["demo"]
    assert runtime_options(restored.gateway_runtime, "gateway", config.path) == item.gateway_runtime
    with pytest.raises(ClaudapterError):
        validate_gateway_runtime({"openrouter_provider": "bad slug"}, "gateway")
