import importlib.util
from pathlib import Path

import pytest


def gate():
    spec = importlib.util.spec_from_file_location("report_gate", Path(__file__).resolve().parents[1] / "scripts/check_test_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "payload",
    [
        "<testsuites/>",
        '<testsuite><testcase name="gateway"><skipped/></testcase></testsuite>',
        '<testsuite><testcase name="gateway"><failure/></testcase></testsuite>',
        '<testsuite><testcase name="gateway"><error/></testcase></testsuite>',
        '<testsuite><testcase name="ok"/><testcase classname="test_new_gateway_feature"><skipped/></testcase></testsuite>',
    ],
)
def test_gateway_evidence_rejects_empty_skip_and_failure_in_any_module(tmp_path, payload):
    report = tmp_path / "tests.xml"
    report.write_text(payload)
    with pytest.raises(ValueError):
        gate().validate_report(report)


def test_direct_only_skip_permission_does_not_hide_failure_or_empty_execution(tmp_path):
    report = tmp_path / "tests.xml"
    report.write_text('<testsuite><testcase name="direct"/><testcase name="optional_gateway"><skipped/></testcase></testsuite>')
    assert gate().validate_report(report, allow_skips=True) == 2
    report.write_text("<testsuite><testcase><failure/></testcase></testsuite>")
    with pytest.raises(ValueError):
        gate().validate_report(report, allow_skips=True)
    report.write_text("<testsuite><testcase><skipped/></testcase></testsuite>")
    with pytest.raises(ValueError):
        gate().validate_report(report, allow_skips=True)
