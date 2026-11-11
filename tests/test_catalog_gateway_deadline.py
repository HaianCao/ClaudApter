import io
import json
import threading

import pytest

from claudapter import catalog
from claudapter.config import Config, Provider
from claudapter.errors import ClaudapterError
from claudapter.http_deadline import Deadline


def fixture(tmp_path, monkeypatch, policy_timeout=None):
    pytest.importorskip("protoverta.discovery")
    provider = Provider(
        "fixture", "Fixture", "https://example.invalid/v1", protocol="messages", execution_mode="gateway"
    )
    config = Config(providers={provider.id: provider}, path=tmp_path / "config.toml")
    if policy_timeout is not None:
        provider.gateway_runtime["catalog_policy_file"] = "policy.json"
        (tmp_path / "policy.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "base_url": provider.base_url,
                    "checked_at": "2026-10-29",
                    "evidence": "synthetic fixture",
                    "policy": {"timeout": policy_timeout, "pagination": "after_id"},
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(catalog, "get_token", lambda *args, **kwargs: "synthetic")
    return config, provider


@pytest.mark.parametrize(
    "caller,policy_timeout,expected",
    [
        (None, None, 12),
        (0.1, None, 0.1),
        (2, 0.5, 0.5),
        (0.1, 2, 0.1),
        (200, None, 30),
    ],
)
def test_gateway_uses_shorter_caller_and_policy_timeout(tmp_path, monkeypatch, caller, policy_timeout, expected):
    config, provider = fixture(tmp_path, monkeypatch, policy_timeout)
    from protoverta import discovery

    observed = []

    def fetch(base, headers, policy, **options):
        observed.append(policy.timeout)
        return (discovery.ModelInfo("m", "Model"),)

    monkeypatch.setattr(discovery, "fetch_catalog", fetch)
    options = {} if caller is None else {"timeout": caller}
    assert catalog.fetch_models(config, provider, cache_sink=lambda entry: None, **options)
    assert observed == [expected]
    assert not any(t.name == "AdapterHTTPDeadline" for t in threading.enumerate())


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), "1", None])
def test_gateway_rejects_invalid_caller_timeout_before_request(tmp_path, monkeypatch, timeout):
    config, provider = fixture(tmp_path, monkeypatch)
    from protoverta import discovery

    def unexpected(*args, **kwargs):
        pytest.fail("invalid timeout reached upstream")

    monkeypatch.setattr(discovery, "fetch_catalog", unexpected)
    with pytest.raises(ClaudapterError):
        catalog.fetch_models(config, provider, timeout=timeout, cache_sink=unexpected)


@pytest.mark.parametrize("parent_timeout", [2, 0.2])
def test_gateway_keeps_external_budget_and_shares_owned_deadline_across_pages(tmp_path, monkeypatch, parent_timeout):
    config, provider = fixture(tmp_path, monkeypatch)
    requests, entries = [], []
    bodies = iter(
        [
            {"data": [{"id": "a"}], "has_more": True, "last_id": "a"},
            {"data": [{"id": "b"}]},
        ]
    )
    with Deadline(parent_timeout) as parent:

        def open_request(request, *, timeout):
            requests.append(request)
            assert 0 < timeout <= 0.5
            assert request.adapter_deadline.remaining() <= parent.remaining() + 0.01
            return io.BytesIO(json.dumps(next(bodies)).encode())

        monkeypatch.setattr(catalog._OPENER, "open", open_request)
        result = catalog.fetch_models(config, provider, timeout=0.5, budget=parent, cache_sink=entries.append)
        assert parent.timer.is_alive()  # Fetch must not exit or cancel a caller-owned context.
        assert len([t for t in threading.enumerate() if t.name == "AdapterHTTPDeadline"]) == 1
    assert [item["id"] for item in result] == ["a", "b"] and len(entries) == 1
    assert len(requests) == 2
    assert requests[0].adapter_deadline is requests[1].adapter_deadline
    assert not any(t.name == "AdapterHTTPDeadline" for t in threading.enumerate())
