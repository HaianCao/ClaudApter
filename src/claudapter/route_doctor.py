"""Route-aware checks; provider calls need an explicit CLI flag."""

import json
from pathlib import Path

from .errors import ClaudapterError
from .launcher import _gateway


def route_doctor(config, provider, model, *, allow_live=False, checks=None, report=None):
    if provider.execution_mode != "gateway":
        raise ClaudapterError("Route checks require gateway mode; native doctor remains available without --check.")
    try:
        from protoverta.certification import CASES, check_route, evidence_report

        gateway, _ = _gateway(config, provider, model)
        if not allow_live:
            return [
                {
                    "case": "route",
                    "passed": False,
                    "outcome": "not_tested_use_allow_live",
                    "requests": 0,
                    "duration_ms": 0,
                }
            ]
        results = check_route(gateway, model, "messages", cases=tuple(checks or CASES), allow_live=True)
        if report:
            body = evidence_report(
                gateway,
                model,
                "messages",
                results,
                verified_with="local route doctor; consult installed package/CLI versions separately",
            )
            with Path(report).open("x", encoding="utf-8") as stream:
                json.dump(body, stream, indent=2)
        return results
    except (ImportError, OSError, ValueError, TypeError):
        raise ClaudapterError(
            "Route doctor failed to configure or write its report; check gateway settings and use a new report path."
        ) from None
