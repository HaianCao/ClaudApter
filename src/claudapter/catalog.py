from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import nullcontext
from dataclasses import replace
from http.client import HTTPException
from pathlib import Path

from .config import Config, Provider, cache_path, claude_base_url
from .credentials import get_token, validate_token
from .errors import ClaudapterError
from .http_deadline import Deadline, DeadlineHTTPHandler, DeadlineHTTPSHandler
from .settings_store import SettingsGuard

_UNSET = object()


def _url(provider: Provider, suffix: str) -> str:
    if provider.protocol == "messages":
        return claude_base_url(provider.base_url) + suffix
    if suffix.startswith("/v1/"):
        suffix = suffix[3:]
    return provider.base_url.rstrip("/") + suffix


def _headers(config: Config, provider: Provider, *, token=_UNSET) -> dict[str, str]:
    if token is _UNSET:
        token = get_token(config, provider.id)
    if not token:
        raise ClaudapterError(f"No API key is saved for {provider.display_name}.")
    token = validate_token(token)
    headers = {"accept": "application/json"}
    if provider.protocol == "messages":
        headers["anthropic-version"] = "2023-06-01"
    headers["Authorization" if provider.auth_mode == "bearer" else "x-api-key"] = f"Bearer {token}" if provider.auth_mode == "bearer" else token
    return headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise ClaudapterError("Provider redirected the API request; refusing to forward your key.")


_OPENER = urllib.request.build_opener(_NoRedirect(), DeadlineHTTPHandler(), DeadlineHTTPSHandler())


# Catalog documents are shallow. Python 3.14 parses far deeper JSON without
# RecursionError, so bound nesting explicitly instead of relying on the parser.
MAX_JSON_DEPTH = 64


def _within_depth(value, limit=MAX_JSON_DEPTH):
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, dict | list):
            if depth > limit:
                return False
            pending.extend((child, depth + 1) for child in (item.values() if isinstance(item, dict) else item))
    return True


def _decode_json(raw):
    try:
        value = json.loads(raw)
        if not _within_depth(value):
            raise ValueError("catalog JSON nesting is too deep")
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Invalid catalog JSON.") from None
    return value


def request_json(request: urllib.request.Request, timeout: float = 12, *, budget=None) -> dict:
    try:
        if budget is None:
            with Deadline(timeout) as owned:
                return request_json(request, timeout, budget=owned)
        if request is not None:
            request.adapter_deadline = budget
        with _OPENER.open(request, timeout=budget.remaining()) as response:
            try:
                declared_size = int(response.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ClaudapterError("Provider returned an invalid Content-Length header.") from exc
            if declared_size > 8_000_000:
                raise ClaudapterError("Provider response is too large.")
            payload = response.read(8_000_001)
            budget.remaining()
            if len(payload) > 8_000_000:
                raise ClaudapterError("Provider response is too large.")
            data = _decode_json(payload)
            if not isinstance(data, dict):
                raise ClaudapterError("Provider returned an invalid JSON object.")
            return data
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096) if exc.fp is not None else b""
        except (OSError, ValueError):
            body = b""
        exc.close()
        from .connection_probe import friendly_http_error

        raise ClaudapterError(friendly_http_error(exc.code, body)) from None
    except (urllib.error.URLError, OSError, ValueError, UnicodeError, RecursionError, HTTPException) as exc:
        raise ClaudapterError(f"Provider request failed: {type(exc).__name__}.") from None


# Token limits that providers report in their model lists. These are the
# provider's own numbers (OpenRouter and TokenHarbor report them; NVIDIA NIM
# does not); nothing is estimated. The first positive integer found wins.
_CONTEXT_FIELDS = ("context_window_tokens", "context_window", "context_length", "max_input_tokens", "top_provider.context_length")
_OUTPUT_FIELDS = ("max_output_tokens", "max_completion_tokens", "top_provider.max_completion_tokens")
_GATEWAY_CONTEXT_FIELD, _GATEWAY_OUTPUT_FIELD = "context_length", "top_provider.max_completion_tokens"
_MAX_REPORTED_TOKENS = 2**31 - 1


def _reported_number(entry, paths):
    for path in paths:
        value = entry
        for key in path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if type(value) is int and 0 < value <= _MAX_REPORTED_TOKENS:
            return value
    return None


def reported_limits(entry) -> dict:
    """``context_window_tokens`` and ``max_output_tokens`` a model list reports, or ``None``."""
    return {"context_window_tokens": _reported_number(entry, _CONTEXT_FIELDS), "max_output_tokens": _reported_number(entry, _OUTPUT_FIELDS)}


def _present_limits(entry) -> dict:
    """Only the limits that were reported, so models without them keep their usual shape."""
    return {key: value for key, value in reported_limits(entry).items() if value is not None}


def launch_limits(reported) -> dict:
    """Model limits for a launch: the model's reported maximums.

    The output limit is capped at half the context window, so a long
    conversation still has room for input (input plus max output must fit the
    window; some models report an output maximum close to the whole window).
    """
    context = reported.get("context_window_tokens") if isinstance(reported, dict) else None
    output = reported.get("max_output_tokens") if isinstance(reported, dict) else None
    limits = {}
    if type(context) is int and context > 1:
        limits["context_window"] = context
        if type(output) is int and output > 0:
            limits["max_output_tokens"] = min(output, context // 2)
    elif type(output) is int and output > 0:
        limits["max_output_tokens"] = output
    return limits


def _identity(config, provider, *, token=_UNSET):
    from .gateway_options import catalog_policy_fingerprint

    if token is _UNSET:
        token = get_token(config, provider.id)
    return {
        "base_url": provider.base_url,
        "protocol": provider.protocol,
        "auth_mode": provider.auth_mode,
        "credential_fingerprint": hashlib.sha256((token or "").encode()).hexdigest(),
        "catalog_policy_file": provider.gateway_runtime.get("catalog_policy_file", ""),
        "catalog_policy_fingerprint": catalog_policy_fingerprint(config.path, provider),
    }


class _CatalogDeadline(Deadline):
    """Own a catalog timeout while retaining caller cancellation/socket tracking."""

    def __init__(self, seconds, parent):
        self.parent = parent
        super().__init__(seconds)

    def remaining(self):
        remaining = super().remaining()
        return min(remaining, self.parent.remaining()) if self.parent is not None else remaining

    def bind(self, sock):
        super().bind(sock)
        if self.parent is not None:
            self.parent.bind(sock)


def _note_skipped(items):
    """Tell the user when the provider's catalog had entries that were left out."""
    skipped = getattr(items, "skipped", 0)
    if skipped:
        noun = "entry" if skipped == 1 else "entries"
        print(f"Note: skipped {skipped} invalid {noun} in the provider's model list.", file=sys.stderr)


def _gateway_models(config, provider, headers=None, budget=None, *, timeout=12):
    try:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Catalog timeout must be a finite positive number.")
        from protoverta.discovery import CatalogPolicy, fetch_catalog

        policy = CatalogPolicy(
            pagination="after_id" if provider.protocol == "messages" else "none",
            context_field=_GATEWAY_CONTEXT_FIELD,
            output_field=_GATEWAY_OUTPUT_FIELD,
            invalid_entries="skip",
        )
        policy_file = provider.gateway_runtime.get("catalog_policy_file")
        if policy_file:
            path = Path(policy_file).expanduser()
            if not path.is_absolute():
                path = (config.path.parent if config.path else Path.cwd()) / path
            policy = CatalogPolicy.load(path, provider.base_url)
        policy = replace(policy, timeout=min(timeout, policy.timeout))
        with _CatalogDeadline(policy.timeout, budget) as active_budget:
            active_budget.remaining()

            def upstream_open(request, *, timeout):
                request.adapter_deadline = active_budget
                return _OPENER.open(request, timeout=min(timeout, active_budget.remaining()))

            request_headers = _headers(config, provider) if headers is None else headers
            # Skip mode lists every model with a valid ID; malformed optional
            # metadata such as token limits is dropped, never guessed.
            items = fetch_catalog(provider.base_url, request_headers, policy, upstream_open=upstream_open)
            _note_skipped(items)
            return [item.as_dict() for item in items]
    except ImportError:
        raise ClaudapterError("Gateway discovery needs the Protoverta component. Run the installer command again to repair it.") from None
    except (ValueError, OSError, TypeError):
        raise ClaudapterError("Gateway catalog failed validation or exceeded its resource budget.") from None


def _direct_models(config, provider, headers, timeout, budget=None):
    # Anthropic's API uses cursor pagination; a gateway may provide fewer pages.
    models: dict[str, dict[str, str]] = {}
    cursor = ""
    try:
        with nullcontext(budget) if budget is not None else Deadline(timeout) as active_budget:
            for _ in range(100):
                active_budget.remaining()
                query = {"limit": "1000"} if provider.protocol == "messages" else {}
                if cursor:
                    query["after_id"] = cursor
                request = urllib.request.Request(_url(provider, "/v1/models") + ("?" + urllib.parse.urlencode(query) if query else ""), headers=headers)
                data = request_json(request, budget=active_budget)
                entries = data.get("data")
                if not isinstance(entries, list):
                    raise ClaudapterError("Provider model catalog has no data array.")
                for entry in entries:
                    if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
                        name = entry.get("display_name") or entry.get("name")
                        models[entry["id"]] = {
                            "id": entry["id"],
                            "display_name": name if isinstance(name, str) and name else entry["id"],
                            **_present_limits(entry),
                        }
                if not data.get("has_more"):
                    break
                next_cursor = data.get("last_id")
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                    raise ClaudapterError("Provider model catalog has invalid pagination.")
                cursor = next_cursor
            else:
                raise ClaudapterError("Provider model catalog exceeds 100 pages.")
    except (TimeoutError, ValueError):
        raise ClaudapterError("Model discovery exceeded its total deadline or has an invalid timeout.") from None
    return models


def store_catalog(config, provider_id, entry):
    path = cache_path(config)
    try:
        with SettingsGuard(path).edit():
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                existing = _decode_json(path.read_text(encoding="utf-8")) if path.exists() else {}
            except ValueError:
                # Invalid JSON/UTF-8 is disposable cache content. IO failures
                # still leave this transaction through the outer handler.
                existing = {}
            if not isinstance(existing, dict):
                existing = {}
            existing[provider_id] = entry
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".models-", suffix=".tmp", delete=False) as stream:
                    temporary = stream.name
                    json.dump(existing, stream, ensure_ascii=False)
                os.replace(temporary, path)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
    except (OSError, ValueError):
        pass  # Catalog caching must never break a successful live discovery.


def fetch_models(config: Config, provider: Provider, *, timeout=12, cache_sink=None, budget=None, token=_UNSET) -> list[dict[str, str]]:
    if token is _UNSET:
        token = get_token(config, provider.id)
    headers = _headers(config, provider, token=token)
    identity = _identity(config, provider, token=token)
    gateway_info = _gateway_models(config, provider, headers, budget, timeout=timeout) if provider.execution_mode == "gateway" else None
    models = (
        {item["id"]: {"id": item["id"], "display_name": item["display_name"], **_present_limits(item)} for item in gateway_info}
        if gateway_info is not None
        else _direct_models(config, provider, headers, timeout, budget)
    )
    result = list(models.values())
    entry = {"identity": identity, "saved_at": time.time(), "models": result, "model_info": gateway_info}
    if cache_sink is not None:
        cache_sink(entry)
    else:
        store_catalog(config, provider.id, entry)
    return result


def cached_model_names(config, provider, identifiers, *, token=_UNSET):
    """Read display names, never capabilities or extra advertised IDs."""
    try:
        with cache_path(config).open("rb") as source:
            raw = source.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            return {}
        entry = _decode_json(raw).get(provider.id, {})
        if entry.get("identity") != _identity(config, provider, token=token):
            return {}
        saved, now = entry.get("saved_at"), time.time()
        if type(saved) not in (int, float) or not 0 <= saved <= now or now - saved > 86400:
            return {}
        info = entry.get("models", [])
        if not isinstance(info, list) or len(info) > 10000:
            return {}
        wanted = set(identifiers)
        return {
            item["id"]: item["display_name"]
            for item in info
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"] in wanted
            and isinstance(item.get("display_name"), str)
            and 1 <= len(item["display_name"]) <= 512
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in item["display_name"])
        }
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return {}


def cached_models(config: Config, provider: Provider) -> tuple[list[dict[str, str]], bool]:
    try:
        data = _decode_json(cache_path(config).read_text(encoding="utf-8"))
        entry = data.get(provider.id, {})
        if entry.get("identity") != _identity(config, provider) or not isinstance(entry.get("models"), list):
            return [], True
        if any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or not isinstance(item.get("display_name"), str)
            for item in entry["models"]
        ):
            return [], True
        saved_at, now = entry.get("saved_at"), time.time()
        # The bounded comparison also rejects NaN/infinity and arbitrarily
        # large JSON integers without a potentially overflowing float cast.
        if type(saved_at) not in (int, float) or not 0 <= saved_at <= now:
            return entry["models"], True
        return entry["models"], now - saved_at > 86_400
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return [], True


def cached_model_limits(config, provider, model, *, token=_UNSET) -> dict:
    """Reported limits for ``model`` from this provider's catalog cache, or ``{}``."""
    try:
        with cache_path(config).open("rb") as source:
            raw = source.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            return {}
        entry = _decode_json(raw).get(provider.id, {})
        if not isinstance(entry, dict) or entry.get("identity") != _identity(config, provider, token=token):
            return {}
        models = entry.get("models", [])
        if not isinstance(models, list) or len(models) > 100000:
            return {}
        item = next((value for value in models if isinstance(value, dict) and value.get("id") == model), None)
        return reported_limits(item) if item else {}
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, RecursionError):
        return {}


def probe_messages(config: Config, provider: Provider, model: str) -> dict:
    payload = json.dumps({"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "."}], "stream": False}).encode("utf-8")
    headers = _headers(config, provider)
    headers["content-type"] = "application/json"
    result = request_json(urllib.request.Request(_url(provider, "/v1/messages"), data=payload, headers=headers, method="POST"), timeout=30)
    if not isinstance(result.get("content"), list):
        raise ClaudapterError("Provider did not return a Messages-compatible response.")
    return result
