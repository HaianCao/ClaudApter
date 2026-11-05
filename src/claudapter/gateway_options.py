"""Validated optional gateway settings, without importing Protoverta."""

import math
import re
from pathlib import Path

from .errors import ClaudapterError
from .toml_text import toml_value

TIMEOUTS = {"timeout", "total_timeout", "progress_timeout"}
COUNTS = {"max_concurrent_requests", "max_stream_bytes", "max_stream_items", "max_tool_argument_bytes"}


def validate_gateway_limits(value, mode):
    if not isinstance(value, dict) or set(value) - (TIMEOUTS | COUNTS):
        raise ClaudapterError("Unknown or invalid gateway runtime limits.")
    if value and mode != "gateway":
        raise ClaudapterError("Gateway runtime limits require gateway mode.")
    for key, number in value.items():
        valid = (type(number) is int and number > 0) if key in COUNTS else (not isinstance(number, bool) and isinstance(number, int | float) and math.isfinite(number) and number > 0)
        if not valid:
            raise ClaudapterError("Gateway runtime limits must be finite positive numbers; byte/item/concurrency limits must be integers.")
    return dict(value)


def limits_toml(value):
    return "{ " + ", ".join(key + " = " + repr(number) for key, number in sorted(value.items())) + " }"


def validate_gateway_runtime(value, mode):
    paths = {"shared_admission_db", "provider_profiles_file", "catalog_policy_file", "diagnostics_dir"}
    counts = {"shared_requests", "shared_concurrency", "retry_attempts", "evidence_max_age_days"}
    times = {"shared_window", "retry_max_wait"}
    flags = {"require_verified_routes", "require_current_evidence"}
    choices = {"context_preflight", "token_count_mode", "prompt_caching"}
    labels = {"openrouter_provider"}
    if not isinstance(value, dict) or set(value) - paths - counts - times - flags - choices - labels:
        raise ClaudapterError("Unknown gateway runtime policy.")
    if value and mode != "gateway":
        raise ClaudapterError("Runtime policies require gateway mode.")
    for key, option in value.items():
        if key in paths:
            valid = isinstance(option, str) and bool(option.strip()) and "\0" not in option
        elif key in flags:
            valid = type(option) is bool
        elif key in labels:
            valid = isinstance(option, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}", option) is not None
        elif key in choices:
            valid = isinstance(option, str) and option in (("off", "ephemeral") if key == "prompt_caching" else ("off", "estimate", "native") if key == "token_count_mode" else ("off", "estimate", "reject_estimate"))
        elif key in counts:
            valid = type(option) is int and option > 0 and (key != "retry_attempts" or option <= 4)
        else:
            valid = not isinstance(option, bool) and isinstance(option, int | float) and math.isfinite(option) and option > 0
        if not valid:
            raise ClaudapterError("Invalid gateway runtime policy value.")
    if any(key in value for key in ("shared_requests", "shared_window", "shared_concurrency")) and "shared_admission_db" not in value:
        raise ClaudapterError("Shared quota settings require a dedicated database path.")
    return dict(value)


def runtime_toml(value):
    return "{ " + ", ".join(key + " = " + toml_value(option) for key, option in sorted(value.items())) + " }"


def runtime_options(value, mode, config_file):
    options = validate_gateway_runtime(value, mode)
    for key in ("shared_admission_db", "provider_profiles_file", "catalog_policy_file"):
        if key in options:
            path = Path(options[key]).expanduser()
            options[key] = str(path.resolve() if path.is_absolute() else (config_file.resolve().parent / path).resolve())
    if "provider_profiles_file" in options:
        from protoverta.provider_profiles import ProviderProfiles

        options["provider_profiles"] = ProviderProfiles.load(options.pop("provider_profiles_file"))
    options.pop("catalog_policy_file", None)  # Used by discovery, not model inference.
    options.pop("diagnostics_dir", None)
    return options


def diagnostics_path(config_file, provider):
    value = provider.gateway_runtime.get("diagnostics_dir")
    if not value:
        return None
    path = Path(value).expanduser()
    root = path.resolve() if path.is_absolute() else (config_file.resolve().parent / path).resolve()
    return root / provider.id


def diagnostic_report(config_file, provider):
    path = diagnostics_path(config_file, provider)
    if path is None:
        return {"records": [], "enabled": False}
    try:
        from protoverta.session_diagnostics import latest_diagnostics

        return latest_diagnostics(path) | {"enabled": True}
    except (ImportError, OSError, ValueError):
        return {"records": [], "enabled": True, "unavailable": True}


def catalog_policy_fingerprint(config_file, provider):
    value = provider.gateway_runtime.get("catalog_policy_file")
    if not value:
        return ""
    if config_file is None:
        return "unresolved"
    import hashlib

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_file.resolve().parent / path
    try:
        with path.open("rb") as stream:
            content = stream.read(65537)
        return hashlib.sha256(content).hexdigest() if len(content) <= 65536 else "invalid"
    except OSError:
        return "unavailable"


def session_options(config_file, provider):
    from contextlib import ExitStack, contextmanager

    @contextmanager
    def options():
        path = diagnostics_path(config_file, provider)
        if path is None:
            yield {}
        else:
            from protoverta.session_diagnostics import diagnostic_journal

            with ExitStack() as stack:
                try:
                    observer = stack.enter_context(diagnostic_journal(path))
                except OSError:
                    yield {}  # Optional diagnostics cannot prevent CLI startup.
                else:
                    yield {"observer": observer}
    return options()
