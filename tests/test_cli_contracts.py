"""CLI forwarding and doctor failures must preserve arguments and operator data."""

from unittest.mock import Mock

import pytest

from claudapter import cli
from claudapter.config import Config, Provider
from claudapter.errors import ClaudapterError as ClientError


@pytest.fixture
def configured(monkeypatch):
    provider = Provider("demo", "Fixture Display", "https://example.invalid/v1")
    config = Config(providers={"demo": provider}, default_provider="demo", default_model="fixture-model")
    monkeypatch.setattr(cli, "load", lambda *args, **kwargs: config)
    return config, provider


@pytest.mark.parametrize("mode", ["run", "print", "resume", "continue"])
def test_cli_preserves_selection_and_literal_payload(configured, monkeypatch, mode):
    launch = Mock(return_value=7)
    monkeypatch.setattr(cli, "launch", launch)
    payload = ["a b & echo %TOKEN%", "-c", 'fixture="value"', "Unicode \\u2603"]
    assert cli.main([mode, "-p", "demo", "-m", "chosen-model", "--", *payload]) == 7
    args = launch.call_args.args
    assert args[:3] == (configured[0], configured[1], "chosen-model")
    assert args[4] == payload and args[5] == mode


def test_cli_default_selection_and_unknown_provider(configured, monkeypatch):
    launch = Mock(return_value=0)
    monkeypatch.setattr(cli, "launch", launch)
    assert cli.main([]) == 0
    assert launch.call_args.args[2] == "fixture-model"
    launch.reset_mock()
    assert cli.main(["run", "-p", "missing"]) != 0
    launch.assert_not_called()


@pytest.mark.parametrize("error,expected", [(ClientError("synthetic launch failure"), 1), (KeyboardInterrupt(), 130)])
def test_cli_contains_launch_failure_and_interrupt(configured, monkeypatch, error, expected, capsys):
    monkeypatch.setattr(cli, "launch", Mock(side_effect=error))
    assert cli.main(["run"]) == expected
    output = capsys.readouterr()
    assert "Traceback" not in output.err


def test_route_doctor_opt_in_is_required_before_dispatch(configured, monkeypatch):
    certification = pytest.importorskip("protoverta.certification")

    from claudapter import route_doctor

    config, provider = configured
    provider.execution_mode = "gateway"
    monkeypatch.setattr(route_doctor, "_gateway", lambda *_: (object(), None))
    check = Mock(side_effect=AssertionError("unexpected provider call"))
    monkeypatch.setattr(certification, "check_route", check)
    result = route_doctor.route_doctor(config, provider, "fixture-model")
    assert result[0]["requests"] == 0 and not result[0]["passed"]
    check.assert_not_called()


def test_route_doctor_does_not_overwrite_report(configured, monkeypatch, tmp_path):
    certification = pytest.importorskip("protoverta.certification")

    from claudapter import route_doctor

    config, provider = configured
    provider.execution_mode = "gateway"
    monkeypatch.setattr(route_doctor, "_gateway", lambda *_: (object(), None))
    monkeypatch.setattr(certification, "check_route", lambda *args, **kwargs: [])
    monkeypatch.setattr(certification, "evidence_report", lambda *args, **kwargs: {"fixture": True})
    report = tmp_path / "existing.json"
    report.write_text("preserve operator data", encoding="utf-8")
    with pytest.raises(ClientError, match="new report path"):
        route_doctor.route_doctor(config, provider, "fixture-model", allow_live=True, report=report)
    assert report.read_text(encoding="utf-8") == "preserve operator data"
