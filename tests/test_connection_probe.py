"""Setup-time connection checks against scripted provider answers; no network."""

import io
import json
import urllib.error
from email.message import Message

import pytest

from claudapter.config import Config
from claudapter.connection_probe import check_connection
from claudapter.errors import ClaudapterError
from claudapter.web import detect_connection, probe_urls


def provider(models, endpoint):
    """An opener answering GET models by auth style and POST endpoint with one status."""
    calls = []

    def opener(request, timeout):
        style = "api_key" if request.get_header("X-api-key") else "bearer"
        calls.append((request.get_method(), request.full_url, style))
        answers = models if request.get_method() == "GET" else endpoint
        status, body = answers[style] if isinstance(answers, dict) else answers
        if status == 200:
            return _Response(200, body)
        raise urllib.error.HTTPError(
            request.full_url, status, "error", Message(), io.BytesIO(json.dumps(body).encode())
        )

    return opener, calls


class _Response(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status


OK = (200, {"data": [{"id": "m"}]})
BAD_KEY = (401, {"error": {"message": "Invalid API key"}})
OFFICIAL_ONLY = (401, {"error": {"message": "unauthorized client detected, contact support"}})
INVALID_BODY = (400, {"error": {"message": "model field is required"}})


def test_bearer_is_used_when_it_works():
    opener, calls = provider({"bearer": OK, "api_key": BAD_KEY}, INVALID_BODY)
    result = check_connection(
        "https://p.test/v1/models",
        "https://p.test/v1/messages",
        "k",
        protocol_label="Anthropic Messages",
        opener=opener,
    )
    assert (result.auth_mode, result.models_available, result.endpoint_available, result.notes) == (
        "bearer",
        True,
        True,
        [],
    )
    assert [style for method, _, style in calls if method == "GET"] == ["bearer"]


def test_x_api_key_is_the_fallback():
    opener, _ = provider({"bearer": BAD_KEY, "api_key": OK}, {"bearer": BAD_KEY, "api_key": INVALID_BODY})
    result = check_connection(
        "https://p.test/v1/models",
        "https://p.test/v1/messages",
        "k",
        protocol_label="Anthropic Messages",
        opener=opener,
    )
    assert result.auth_mode == "api_key" and not result.key_rejected


def test_key_rejected_by_both_styles():
    # A public model list (OpenRouter's) accepts any key; the endpoint does not.
    opener, _ = provider({"bearer": OK, "api_key": OK}, BAD_KEY)
    result = check_connection(
        "https://p.test/v1/models",
        "https://p.test/v1/messages",
        "k",
        protocol_label="Anthropic Messages",
        opener=opener,
    )
    assert result.key_rejected and "rejected this API key" in result.notes[-1]


def test_official_client_only_provider():
    opener, calls = provider({"bearer": OFFICIAL_ONLY, "api_key": OFFICIAL_ONLY}, OFFICIAL_ONLY)
    result = check_connection(
        "https://p.test/v1/models",
        "https://p.test/v1/messages",
        "k",
        protocol_label="Anthropic Messages",
        opener=opener,
    )
    assert result.official_client_only and result.auth_mode is None and "official CLIs" in result.notes[0]
    assert len(calls) == 1  # Nothing else is tried.


def test_model_list_decides_when_the_endpoint_is_missing():
    opener, _ = provider({"bearer": BAD_KEY, "api_key": OK}, (404, {}))
    result = check_connection(
        "https://p.test/v1/models", "https://p.test/v1/responses", "k", protocol_label="Responses", opener=opener
    )
    assert (result.auth_mode, result.endpoint_available, result.models_available) == ("api_key", False, True)


def test_missing_model_list_and_wrong_protocol_are_explained():
    opener, _ = provider({"bearer": (404, {}), "api_key": (404, {})}, (404, {}))
    result = check_connection(
        "https://p.test/v1/models", "https://p.test/v1/responses", "k", protocol_label="Responses", opener=opener
    )
    assert result.models_available is False and result.endpoint_available is False
    assert any("no model list" in note for note in result.notes)
    assert any("no Responses endpoint" in note for note in result.notes)


def test_unreachable_provider_is_saved_with_a_note():
    def offline(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError())

    result = check_connection(
        "https://p.test/v1/models",
        "https://p.test/v1/messages",
        "k",
        protocol_label="Anthropic Messages",
        opener=offline,
    )
    assert result.auth_mode is None and "Could not reach" in result.notes[0]


def test_probe_urls_match_the_launcher_and_gateway():
    assert probe_urls("https://openrouter.ai/api/v1", "messages") == (
        "https://openrouter.ai/api/v1/models",
        "https://openrouter.ai/api/v1/messages",
    )
    assert probe_urls("https://openrouter.ai/api", "messages") == (
        "https://openrouter.ai/api/v1/models",
        "https://openrouter.ai/api/v1/messages",
    )
    assert probe_urls("https://integrate.api.nvidia.com/v1", "chat") == (
        "https://integrate.api.nvidia.com/v1/models",
        "https://integrate.api.nvidia.com/v1/chat/completions",
    )


def test_detect_connection_resolves_auto_and_switches_official_only_providers_to_direct():
    body = {
        "id": "demo",
        "base_url": "https://p.test/v1",
        "protocol": "messages",
        "auth_mode": "auto",
        "execution_mode": "gateway",
        "api_key": "k",
    }

    def probe_for(models, endpoint):
        opener, _ = provider(models, endpoint)
        return lambda *args, **kwargs: check_connection(*args, **kwargs, opener=opener)

    resolved, notes = detect_connection(
        Config(), body, probe_for({"bearer": BAD_KEY, "api_key": OK}, {"bearer": BAD_KEY, "api_key": INVALID_BODY})
    )
    assert (resolved["auth_mode"], resolved["execution_mode"], notes) == ("api_key", "gateway", [])
    resolved, notes = detect_connection(
        Config(), body, probe_for({"bearer": OFFICIAL_ONLY, "api_key": OFFICIAL_ONLY}, OFFICIAL_ONLY)
    )
    assert (resolved["auth_mode"], resolved["execution_mode"]) == ("bearer", "direct") and notes
    with pytest.raises(ClaudapterError, match="rejected this API key"):
        detect_connection(Config(), body, probe_for({"bearer": OK, "api_key": OK}, BAD_KEY))
    explicit = {**body, "auth_mode": "bearer"}
    assert detect_connection(Config(), explicit, lambda *a, **k: pytest.fail("explicit auth must not be probed")) == (
        explicit,
        [],
    )
