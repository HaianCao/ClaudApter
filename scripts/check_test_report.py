"""Fail CI for missing/empty/failed JUnit evidence or skipped gateway tests."""

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path


def validate_report(path: Path, *, allow_skips: bool = False) -> int:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    skipped = [case for case in cases if case.find("skipped") is not None]
    failed = [case for case in cases if any(case.find(tag) is not None for tag in ("failure", "error"))]
    if not cases or len(skipped) == len(cases) or failed or (skipped and not allow_skips):
        raise ValueError(f"Invalid test evidence: {len(cases)} cases, {len(skipped)} skipped, {len(failed)} failed; gateway jobs permit no skips")
    return len(cases)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--allow-skips", action="store_true", help="Direct-only job without the optional gateway")
    args = parser.parse_args()
    try:
        count = validate_report(args.report, allow_skips=args.allow_skips)
    except (OSError, ValueError, ET.ParseError) as exc:
        parser.exit(1, f"Test evidence gate failed: {exc}\n")
    print(f"Test evidence PASS: {count} cases; allow_skips={args.allow_skips}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
