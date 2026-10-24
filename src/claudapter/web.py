from __future__ import annotations

import copy
import json
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .catalog import cached_models, fetch_models
from .config import (
    EFFORTS,
    Config,
    Provider,
    config_path,
    save,
    validate,
    validate_base_url,
    validate_capability_settings,
    validate_provider_id,
)
from .credentials import env_path, get_token, remove_token, store_token
from .errors import ClaudapterError
from .gateway_options import validate_gateway_limits, validate_gateway_runtime
from .launcher import launch, preflight
from .settings_store import SettingsGuard, atomic_bytes
from .web_security import local_request

_HTML = Path(__file__).with_name("web_template.html").read_text(encoding="utf-8")


def public_config(config: Config) -> dict:
    result = {
        "providers": [{"id": p.id, "display_name": p.display_name, "base_url": p.base_url, "auth_mode": p.auth_mode, "default_model": p.default_model, "execution_mode": p.execution_mode, "protocol": p.protocol, "upstream_reasoning": p.upstream_reasoning, "capabilities_file": p.capabilities_file, "require_verified_capabilities": p.require_verified_capabilities, "has_key": bool(get_token(config, p.id))} for p in config.providers.values()],
        "defaults": {"provider": config.default_provider, "model": config.default_model, "effort": config.default_effort},
    }
    for provider in result["providers"]:
        provider["gateway_limits"] = config.providers[provider["id"]].gateway_limits
        provider["gateway_runtime"] = config.providers[provider["id"]].gateway_runtime
    return result


def persist_provider_change(config: Config, updated: Config, provider_id: str, token: str | None) -> None:
    """Persist a provider edit and its secret, restoring the secret if config saving fails."""
    validate(updated)
    target = env_path(config)
    previous = target.read_bytes() if token is not None and target.exists() else None
    try:
        if token is not None:
            if token:
                store_token(config, provider_id, token)
            else:
                remove_token(config, provider_id)
        save(updated)
    except Exception as exc:
        if token is not None:
            try:
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    atomic_bytes(target, previous)
            except OSError:
                raise ClaudapterError("Saving settings and restoring the API key failed. Check local storage permissions.") from exc
        raise
    config.providers = updated.providers
    config.default_provider = updated.default_provider
    config.default_model = updated.default_model
    config.default_effort = updated.default_effort
    config.path = updated.path


def serve(config: Config) -> int:
    token = secrets.token_urlsafe(32)
    requested_run: tuple[Provider, str, str] | None = None
    state_lock = threading.RLock()
    guard = SettingsGuard(config.path or config_path())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _json(self, value: dict, status: int = 200):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            return local_request(self.headers, server.server_port) and secrets.compare_digest(self.headers.get("X-Claudapter-Token", ""), token)

        def _body(self) -> dict:
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > 64_000:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ClaudapterError("Request body must be a JSON object.")
                return body
            except (ValueError, json.JSONDecodeError) as exc:
                raise ClaudapterError("Invalid JSON request.") from exc

        def _provider(self, value: str) -> Provider:
            provider = config.providers.get(value)
            if not provider:
                raise ClaudapterError("Choose a configured provider.")
            return provider

        def do_GET(self):
            if not local_request(self.headers, server.server_port):
                self._json({"error": "Setup is local-only."}, HTTPStatus.FORBIDDEN)
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/":
                supplied = parse_qs(parsed.query).get("token", [""])[0]
                if not secrets.compare_digest(supplied, token):
                    self._json({"error": "Unauthorized"}, HTTPStatus.FORBIDDEN)
                    return
                page = _HTML.replace("__SESSION_TOKEN__", token).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if parsed.path in {
                "/assets/ClaudApter_black.png",
                "/assets/ClaudApter_white.png",
                "/assets/ClaudApter_black_icon.png",
                "/assets/ClaudApter_white_icon.png",
            }:
                asset_name = parsed.path.rsplit("/", 1)[-1]
                packaged = Path(__file__).with_name("assets") / asset_name
                source = Path(__file__).resolve().parents[2] / "assets" / asset_name
                asset_path = packaged if packaged.is_file() else source
                if not asset_path.is_file():
                    self._json({"error": "Brand asset not found"}, HTTPStatus.NOT_FOUND)
                    return
                payload = asset_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # A closed tab can cancel an in-flight brand asset.
                return
            if not self._authorized():
                self._json({"error": "Unauthorized"}, HTTPStatus.FORBIDDEN)
                return
            try:
                if parsed.path == "/api/config":
                    with state_lock:
                        self._json(public_config(config))
                elif parsed.path == "/api/models":
                    with state_lock:
                        provider = self._provider(parse_qs(parsed.query).get("provider", [""])[0])
                    models, stale = cached_models(config, provider)
                    self._json({"models": models, "stale": stale})
                else:
                    self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            except (ClaudapterError, OSError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def do_POST(self):
            if not self._authorized():
                self._json({"error": "Unauthorized"}, HTTPStatus.FORBIDDEN)
                return
            try:
                self.connection.settimeout(15)
                with state_lock, guard.edit():
                    self._post()
            except (OSError, ValueError):
                self._json({"error": "Could not save settings. Reopen setup if settings changed elsewhere; check local storage permissions."}, HTTPStatus.BAD_REQUEST)

        def _post(self):
            nonlocal requested_run
            if not self._authorized():
                self._json({"error": "Unauthorized"}, HTTPStatus.FORBIDDEN)
                return
            try:
                body = self._body()
                path = urlsplit(self.path).path
                if path == "/api/models/refresh":
                    with state_lock:
                        provider = self._provider(str(body.get("provider", "")))
                    self._json({"models": fetch_models(config, provider)})
                    return
            except (ClaudapterError, OSError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            state_lock.acquire()
            try:
                if path == "/api/provider":
                    provider_id = validate_provider_id(str(body.get("id", "")).strip())
                    display_name = str(body.get("display_name", "")).strip()
                    if not display_name:
                        raise ClaudapterError("Display name is required.")
                    mode = str(body.get("auth_mode", "bearer"))
                    if mode not in {"bearer", "api_key"}:
                        raise ClaudapterError("Choose Bearer token or API key authentication.")
                    old_provider = config.providers.get(provider_id)
                    default_model = str(body.get("default_model", "")).strip() or (old_provider.default_model if old_provider else "")
                    execution_mode = str(body.get("execution_mode", old_provider.execution_mode if old_provider else "direct"))
                    protocol = str(body.get("protocol", old_provider.protocol if old_provider else "messages"))
                    upstream_reasoning = str(body.get("upstream_reasoning", old_provider.upstream_reasoning if old_provider else "provider_default"))
                    capabilities_file, require_verified = validate_capability_settings(body.get("capabilities_file", old_provider.capabilities_file if old_provider else ""), body.get("require_verified_capabilities", old_provider.require_verified_capabilities if old_provider else False), execution_mode)
                    provider = Provider(provider_id, display_name, validate_base_url(str(body.get("base_url", "")), protocol), mode, default_model, execution_mode, protocol, upstream_reasoning, capabilities_file, require_verified)
                    provider.gateway_limits = validate_gateway_limits(body.get("gateway_limits", old_provider.gateway_limits if old_provider else {}), execution_mode)
                    provider.gateway_runtime = validate_gateway_runtime(body.get("gateway_runtime", old_provider.gateway_runtime if old_provider else {}), execution_mode)
                    updated = copy.deepcopy(config)
                    updated.providers[provider_id] = provider
                    persist_provider_change(config, updated, provider_id, str(body["api_key"]) if body.get("api_key") else None)
                    self._json(public_config(config))
                    return
                if path == "/api/default":
                    provider = self._provider(str(body.get("provider", "")))
                    model = str(body.get("model", "")).strip()
                    effort = str(body.get("effort", "default"))
                    if not model or effort not in EFFORTS:
                        raise ClaudapterError("Choose a model and valid effort level.")
                    updated = copy.deepcopy(config)
                    updated.default_provider, updated.default_model, updated.default_effort = provider.id, model, effort
                    save(updated)
                    config.default_provider, config.default_model, config.default_effort = provider.id, model, effort
                    self._json(public_config(config))
                    return
                if path == "/api/run":
                    provider = self._provider(str(body.get("provider", "")))
                    model = str(body.get("model", "")).strip()
                    effort = str(body.get("effort", "default"))
                    preflight(config, provider, model, effort)
                    updated = copy.deepcopy(config)
                    updated.default_provider, updated.default_model, updated.default_effort = provider.id, model, effort
                    save(updated)
                    config.default_provider, config.default_model, config.default_effort = provider.id, model, effort
                    requested_run = provider, model, effort
                    self._json({"ready": True})
                    threading.Thread(target=server.shutdown, daemon=True).start()
                    return
                self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            except (ClaudapterError, OSError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            finally:
                state_lock.release()

        def do_DELETE(self):
            if not self._authorized():
                self._json({"error": "Unauthorized"}, HTTPStatus.FORBIDDEN)
                return
            try:
                with state_lock, guard.edit():
                    self._delete()
            except (OSError, ValueError):
                self._json({"error": "Could not remove provider. Reopen setup if settings changed elsewhere."}, HTTPStatus.BAD_REQUEST)

        def _delete(self):
            if not self._authorized():
                self._json({"error": "Unauthorized"}, HTTPStatus.FORBIDDEN)
                return
            provider_id = parse_qs(urlsplit(self.path).query).get("provider", [""])[0]
            state_lock.acquire()
            try:
                if provider_id not in config.providers:
                    self._json({"error": "Unknown provider"}, HTTPStatus.NOT_FOUND)
                    return
                updated = copy.deepcopy(config)
                del updated.providers[provider_id]
                if updated.default_provider == provider_id:
                    updated.default_provider = updated.default_model = ""
                persist_provider_change(config, updated, provider_id, "")
                self._json(public_config(config))
            except (ClaudapterError, OSError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            finally:
                state_lock.release()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_port}/?token={token}"
    print("ClaudApter setup is open in your browser. Press Ctrl+C to stop it.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    if requested_run:
        return launch(config, *requested_run, [])
    return 0
