from pathlib import Path
from unittest.mock import patch

import pytest

from claudapter.config import Config, Provider, load, save
from claudapter.errors import ClaudapterError
from claudapter.gateway_options import runtime_options, validate_gateway_runtime
from claudapter.launcher import _gateway


@pytest.mark.parametrize(
    "value",
    [
        {"unknown": 1},
        {"shared_requests": 10},
        {"retry_attempts": 5},
        {"retry_attempts": True},
        {"shared_admission_db": ""},
        {"shared_window": float("nan")},
    ],
)
def test_invalid_runtime_policies_are_rejected(value):
    with pytest.raises(ClaudapterError):
        validate_gateway_runtime(value, "gateway")


def test_runtime_config_round_trip_and_paths_are_relative_to_config_not_cwd(tmp_path):
    protocol = "chat"
    provider = Provider("demo", "Demo", "https://example.test/v1", protocol=protocol, execution_mode="gateway")
    provider.gateway_runtime = {
        "shared_admission_db": "budget.sqlite",
        "shared_requests": 10,
        "shared_concurrency": 2,
        "retry_attempts": 2,
        "catalog_policy_file": "catalog.json",
        "context_preflight": "estimate",
        "require_verified_routes": False,
    }
    config = Config(providers={"demo": provider}, path=tmp_path / "config.toml")
    save(config)
    restored = load(config.path).providers["demo"]
    assert restored.gateway_runtime == provider.gateway_runtime
    options = runtime_options(restored.gateway_runtime, "gateway", config.path)
    assert options["shared_admission_db"] == str((tmp_path / "budget.sqlite").resolve())
    assert not Path(options["shared_admission_db"]).exists()  # loading config never creates shared state
    with pytest.raises(ClaudapterError):
        validate_gateway_runtime(restored.gateway_runtime, "direct")


def test_runtime_options_reach_actual_core(tmp_path):
    pytest.importorskip("protoverta")
    provider = Provider("demo", "Demo", "https://example.test/v1", protocol="chat", execution_mode="gateway")
    provider.gateway_runtime = {"shared_admission_db": "budget.sqlite", "retry_attempts": 2}
    config = Config(providers={"demo": provider}, path=tmp_path / "config.toml")
    with patch("claudapter.launcher.get_token", return_value="fixture"):
        gateway, _ = _gateway(config, provider)
    assert gateway.retry_attempts == 2
    assert gateway.shared_admission_db == str((tmp_path / "budget.sqlite").resolve())
