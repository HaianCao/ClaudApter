"""Validated optional gateway settings, without importing Protoverta."""

import math

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
