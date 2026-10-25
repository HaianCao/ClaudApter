"""Validated optional gateway settings, without importing Protoverta."""

import json
import math
from pathlib import Path

from .errors import ClaudapterError

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
    paths = {"shared_admission_db", "provider_profiles_file", "catalog_policy_file"}
    counts = {"shared_requests", "shared_concurrency", "retry_attempts"}
    times = {"shared_window", "retry_max_wait"}
    flags = {"require_verified_routes"}
    choices = {"context_preflight"}
    if not isinstance(value, dict) or set(value) - paths - counts - times - flags - choices:
        raise ClaudapterError("Unknown gateway runtime policy.")
    if value and mode != "gateway":
        raise ClaudapterError("Runtime policies require gateway mode.")
    for key, option in value.items():
        if key in paths:
            valid = isinstance(option, str) and bool(option.strip()) and "\0" not in option
        elif key in flags:
            valid = type(option) is bool
        elif key in choices:
            valid = isinstance(option, str) and option in ("off", "estimate", "reject_estimate")
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
    return "{ " + ", ".join(key + " = " + json.dumps(option) for key, option in sorted(value.items())) + " }"


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
    return options
