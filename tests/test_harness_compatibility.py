import os
import subprocess
import sys
import warnings

import pytest

from claudapter import harness


def test_native_route_does_not_probe_harness_version(monkeypatch):
    from claudapter import launcher
    from claudapter.config import Config, Provider
    provider = Provider("fixture", "Fixture", "https://example.invalid")
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "fixture.exe")
    monkeypatch.setattr(launcher, "get_token", lambda *_: "synthetic")
    monkeypatch.setattr(launcher, "cross_protocol_environment", lambda *_: pytest.fail("native route must not probe"))
    args, _ = launcher.build_invocation(Config(), provider, "model", "default", [])
    assert "model" in args


@pytest.fixture(autouse=True)
def reset_probe_cache():
    harness._probe.cache_clear()
    yield
    harness._probe.cache_clear()


def test_parse_exact_release_and_reject_ambiguous_output():
    assert harness.parse_version(b"2.1.308 (Claude Code)\r\n") == (2, 1, 308)
    for raw in (b"", b"1.2", b"not-a-cli 1.2.3", b"2.1.308 (Claude Code)-dev", b"x" * 257,
                b"2.1.308 (Claude Code)\n2.1.308 (Claude Code)", b"\xff"):
        assert harness.parse_version(raw) is None


def test_probe_uses_isolated_profile_without_credentials(tmp_path, monkeypatch):
    script = tmp_path / "fixture.py"
    script.write_text(
        "import os,sys\n"
        "assert sys.argv[1:] == ['--version']\n"
        "assert 'UNUSUAL_CREDENTIAL_NAME' not in os.environ\n"
        "assert 'HTTPS_PROXY' not in os.environ\n"
        "assert 'PYTHONPATH' not in os.environ\n"
        "assert os.getcwd() != os.environ['USERPROFILE']\n"
        "assert os.path.isdir(os.environ['CODEX_HOME'])\n"
        "assert os.path.isdir(os.environ['CLAUDE_CONFIG_DIR'])\n"
        "print('2.1.308 (Claude Code)')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UNUSUAL_CREDENTIAL_NAME", "synthetic-private")
    monkeypatch.setenv("HTTPS_PROXY", "synthetic-private")
    before = dict(os.environ)
    assert harness.detect_version([sys.executable, str(script)]) == (2, 1, 308)
    assert os.environ == before


@pytest.mark.parametrize("mode", ["nonzero", "oversized", "malformed"])
def test_invalid_version_is_unknown(tmp_path, mode):
    script = tmp_path / "fixture.py"
    script.write_text({
        "nonzero": "print('2.1.308 (Claude Code)'); raise SystemExit(1)",
        "oversized": "print('x' * 4096)",
        "malformed": "print('private diagnostic text')",
    }[mode], encoding="utf-8")
    assert harness.detect_version([sys.executable, str(script)]) is None


@pytest.mark.parametrize("error", [OSError("synthetic-private"), subprocess.TimeoutExpired("private", 3)])
def test_failed_or_timed_out_probe_returns_unknown(monkeypatch, error):
    def fail(*args, **kwargs):
        assert kwargs["timeout"] == 3
        raise error
    monkeypatch.setattr(harness.subprocess, "run", fail)
    assert harness.detect_version(["nonexistent-fixture"]) is None


def test_probe_cache_refreshes_when_cli_entry_changes(tmp_path):
    script = tmp_path / "fixture.py"
    script.write_text("print('2.1.308 (Claude Code)')", encoding="utf-8")
    prefix = [sys.executable, str(script)]
    assert harness.detect_version(prefix) == (2, 1, 308)
    script.write_text("print('a different release output, not verified')", encoding="utf-8")
    assert harness.detect_version(prefix) is None


def test_known_policy_has_no_warning(monkeypatch):
    monkeypatch.setattr(harness, "detect_version", lambda _: (2, 1, 308))
    with warnings.catch_warnings(record=True) as caught:
        settings = harness.cross_protocol_environment(["fixture"])
    assert not caught
    assert settings["ENABLE_TOOL_SEARCH"] == "false"
    assert settings["CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS"] == "1"


@pytest.mark.parametrize("version", [None, (9, 9, 9), (0, 0, 1)])
def test_unknown_version_warns_without_echoing_cli_output_or_enabling_features(monkeypatch, version):
    monkeypatch.setattr(harness, "detect_version", lambda _: version)
    with pytest.warns(RuntimeWarning, match="not verified"):
        settings = harness.cross_protocol_environment(["fixture"])
    assert settings["ENABLE_TOOL_SEARCH"] == "false"
