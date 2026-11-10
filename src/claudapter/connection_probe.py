"""Setup-time connection checks: which auth header works and whether the endpoint exists.

Shared verbatim by ClaudApter and CodexApter. The checks never generate text,
so they cost nothing: ``GET <models>`` with the key, and ``POST <endpoint>``
with an empty JSON body (a provider that has the endpoint answers 400/422,
one that does not answers 404/405). Redirects are refused so the key never
reaches another host, and the default Python User-Agent is kept.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

TIMEOUT_SECONDS = 10
_ANTHROPIC_VERSION = "2023-06-01"
# Providers that serve official CLIs only answer other clients with this.
_OFFICIAL_CLIENT_ONLY = ("unauthorized client",)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not followed", headers, fp)


def _headers(style: str, token: str) -> dict[str, str]:
    if style == "api_key":
        return {"x-api-key": token, "anthropic-version": _ANTHROPIC_VERSION}
    return {"Authorization": f"Bearer {token}", "anthropic-version": _ANTHROPIC_VERSION}


def _message(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return raw[:200].decode("utf-8", "replace")
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"][:200]
    if isinstance(error, str):
        return error[:200]
    return ""


def friendly_http_error(status: int, raw: bytes = b"") -> str:
    """One actionable sentence for a provider's HTTP error on setup or model refresh."""
    message = _message(raw).lower()
    if status in (401, 403) and any(marker in message for marker in _OFFICIAL_CLIENT_ONLY):
        return "This provider only accepts its official CLIs, so the model list cannot be read here. Enter the model ID yourself."
    if status in (401, 403):
        return f"The provider rejected the API key (HTTP {status}). Edit the provider and paste a valid key."
    if status == 402 or any(word in message for word in ("insufficient credit", "insufficient quota", "insufficient balance", "billing")):
        return f"The provider reports no credit or quota left (HTTP {status}). Top up the account, then try again."
    if status == 404:
        return "This provider has no model list at this address (HTTP 404). Enter the model ID yourself, or check the base URL."
    if status == 429:
        return "The provider is rate limiting requests (HTTP 429). Try again in a moment."
    if status >= 500:
        return f"The provider had a server error (HTTP {status}). Try again later."
    return f"The provider answered HTTP {status}."


@dataclass
class ProbeResult:
    """What setup learned about a provider; ``auth_mode`` is None when unknown."""

    auth_mode: str | None = None
    key_rejected: bool = False
    official_client_only: bool = False
    models_available: bool | None = None
    endpoint_available: bool | None = None
    notes: list[str] = field(default_factory=list)


def check_connection(models_url: str, endpoint_url: str, token: str, *, protocol_label: str, opener=None) -> ProbeResult:
    """Find the auth header the provider accepts and whether the endpoint exists.

    The endpoint itself checks the key: some model lists (OpenRouter's) are
    public and accept any key. ``POST {}`` answers 400/422 for a valid key and
    401/403 for a rejected one, without generating anything.
    """
    open_url = opener or urllib.request.build_opener(_NoRedirect).open
    result = ProbeResult()

    def request(method: str, url: str, headers: dict[str, str], body: bytes | None = None) -> tuple[int | None, bytes]:
        try:
            with open_url(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=TIMEOUT_SECONDS) as response:
                return response.status, response.read(4096)
        except urllib.error.HTTPError as error:
            return error.code, error.read(4096) if error.fp is not None else b""
        except (urllib.error.URLError, OSError, ValueError):
            return None, b""

    def official_only(raw: bytes) -> bool:
        if not any(marker in _message(raw).lower() for marker in _OFFICIAL_CLIENT_ONLY):
            return False
        result.official_client_only = True
        result.notes.append(
            "This provider only accepts its official CLIs, so the model list cannot be read here and the local gateway cannot be used. "
            "The connection was set to Direct; enter the model ID yourself."
        )
        return True

    def attempt(method: str, url: str, extra: dict[str, str], body: bytes | None) -> tuple[str | None, int | None, int]:
        """Bearer first, then x-api-key: (accepted style, last status, rejections)."""
        rejected, status = 0, None
        for style in ("bearer", "api_key"):
            status, raw = request(method, url, {**_headers(style, token), **extra}, body)
            if status in (401, 403):
                if official_only(raw):
                    return None, status, 0
                rejected += 1
                continue
            return (style if status is not None and status not in (404, 405) and status < 500 else None), status, rejected
        return None, status, rejected

    style, status, rejected = attempt("POST", endpoint_url, {"Content-Type": "application/json"}, b"{}")
    if result.official_client_only:
        return result
    if status is None:
        result.notes.append("Could not reach the provider to check the key. Check the base URL and your network; the settings were saved.")
        return result
    if rejected == 2:
        result.key_rejected = True
        result.notes.append("The provider rejected this API key (both Bearer and x-api-key). Check that the key is correct and active.")
        return result
    if status in (404, 405):
        result.endpoint_available = False
        result.notes.append(f"This provider has no {protocol_label} endpoint at this address. Check the base URL or choose another protocol.")
    elif style is not None:
        result.endpoint_available, result.auth_mode = True, style
    models_style = result.auth_mode
    if models_style is None:
        # No endpoint to check the key against: the model list decides.
        models_style, status, rejected = attempt("GET", models_url, {}, None)
        if result.official_client_only:
            return result
        if rejected == 2:
            result.key_rejected = True
            result.notes.append("The provider rejected this API key (both Bearer and x-api-key). Check that the key is correct and active.")
            return result
        result.auth_mode = models_style
    else:
        status, _ = request("GET", models_url, _headers(models_style, token))
    if status == 200:
        result.models_available = True
    elif status in (404, 405):
        result.models_available = False
        result.notes.append("This provider has no model list. Enter the model ID yourself.")
    return result
