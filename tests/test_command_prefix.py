import json
import subprocess
import sys

import pytest

from claudapter.command import command_prefix

COMMAND = "claude"
PACKAGE = "@anthropic-ai/claude-code"


def npm_fixture(tmp_path):
    shim = tmp_path / (COMMAND + ".cmd")
    shim.write_text("@echo off\necho SHIM_EXECUTED\n", encoding="ascii")
    root = tmp_path / "node_modules" / PACKAGE
    root.mkdir(parents=True)
    entry = root / "entry.js"
    entry.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    metadata = root / "package.json"
    metadata.write_text(json.dumps({"name": PACKAGE, "bin": {COMMAND: "entry.js"}}), encoding="utf-8")
    return shim, root, entry, metadata


def test_native_command_remains_direct():
    assert command_prefix(COMMAND + ".exe", COMMAND, PACKAGE) == [COMMAND + ".exe"]


@pytest.mark.parametrize("extension", [".cmd", ".bat"])
def test_unknown_batch_shim_is_refused_without_execution(tmp_path, extension):
    shim = tmp_path / (COMMAND + extension)
    shim.write_text("@echo off\necho SHIM_EXECUTED\n", encoding="ascii")
    with pytest.raises(ValueError, match="Cannot safely run"):
        command_prefix(str(shim), COMMAND, PACKAGE)


@pytest.mark.parametrize("binding", ["entry.js", {COMMAND: "entry.js"}])
def test_supported_npm_entry_preserves_real_subprocess_argv(tmp_path, monkeypatch, binding):
    shim, _root, entry, metadata = npm_fixture(tmp_path)
    metadata.write_text(json.dumps({"name": PACKAGE, "bin": binding}), encoding="utf-8")
    monkeypatch.setattr("claudapter.command.shutil.which", lambda name: sys.executable)
    prefix = command_prefix(str(shim), COMMAND, PACKAGE)
    assert prefix == [sys.executable, str(entry.resolve())]
    # Native Python stands in for the trusted Node interpreter. This tests the
    # actual OS argument transport without needing Node or executing the shim.
    args = ["model&echo.CMD_META_EXECUTED", 'quotes " & | < > ^', "%COMSPEC%", "!EXPAND!",
            "spaces and Unicode 🌏", "line\nbreak", "--", "prompt & echo.SHOULD_NOT_RUN"]
    result = subprocess.run([*prefix, *args], capture_output=True, text=True, timeout=5, check=True)
    assert json.loads(result.stdout) == args
    assert "SHIM_EXECUTED" not in result.stdout


@pytest.mark.parametrize("mutation", ["wrong_package", "escape", "batch_entry", "missing_entry",
                                      "bad_json", "oversized_json", "missing_node", "batch_node"])
def test_unverified_or_unsafe_npm_layout_is_refused(tmp_path, monkeypatch, mutation):
    shim, root, _entry, metadata = npm_fixture(tmp_path)
    body = {"name": PACKAGE, "bin": {COMMAND: "entry.js"}}
    node = sys.executable
    if mutation == "wrong_package":
        body["name"] = "wrong-package"
    elif mutation == "escape":
        (root.parent / "outside.js").write_text("fixture", encoding="ascii")
        body["bin"][COMMAND] = "../outside.js"
    elif mutation == "batch_entry":
        (root / "entry.cmd").write_text("@echo off", encoding="ascii")
        body["bin"][COMMAND] = "entry.cmd"
    elif mutation == "missing_entry":
        body["bin"][COMMAND] = "missing.js"
    elif mutation == "missing_node":
        node = None
    elif mutation == "batch_node":
        node = str(tmp_path / "node.cmd")
    metadata.write_text("{" if mutation == "bad_json" else " " * 65537 if mutation == "oversized_json"
                        else json.dumps(body), encoding="utf-8")
    monkeypatch.setattr("claudapter.command.shutil.which", lambda name: node)
    with pytest.raises(ValueError, match="Cannot safely run"):
        command_prefix(str(shim), COMMAND, PACKAGE)


def test_npm_native_binary_entry_avoids_node(tmp_path):
    shim, root, _entry, metadata = npm_fixture(tmp_path)
    native = root / "fixture.exe"
    native.write_bytes(b"fixture-only-not-executed")
    metadata.write_text(json.dumps({"name": PACKAGE, "bin": {COMMAND: "fixture.exe"}}), encoding="utf-8")
    assert command_prefix(str(shim), COMMAND, PACKAGE) == [str(native.resolve())]


def test_standard_local_node_modules_bin_layout(tmp_path, monkeypatch):
    shim, _root, entry, _metadata = npm_fixture(tmp_path)
    local = tmp_path / "node_modules" / ".bin" / shim.name
    local.parent.mkdir()
    shim.rename(local)
    monkeypatch.setattr("claudapter.command.shutil.which", lambda name: sys.executable)
    assert command_prefix(str(local), COMMAND, PACKAGE) == [sys.executable, str(entry.resolve())]

@pytest.mark.parametrize("gateway", [False, True])
def test_real_invocation_builder_preserves_model_and_prompt_without_batch_shell(tmp_path, monkeypatch, gateway):
    from claudapter import launcher
    from claudapter.config import Config, Provider

    shim, _root, entry, _metadata = npm_fixture(tmp_path)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: str(shim) if name == COMMAND else sys.executable)
    monkeypatch.setattr(launcher, "get_token", lambda *args, **kwargs: "synthetic-only")
    provider = Provider("fixture", "Fixture", "https://example.invalid/v1")
    provider.execution_mode = "gateway" if gateway else "direct"
    config = Config(path=tmp_path / "settings.toml")
    model = "model&echo.CMD_META_EXECUTED"
    extra = ["--", 'prompt "quoted" & echo.SHOULD_NOT_RUN %COMSPEC% !VAR! 🌏']
    options = {"gateway_url": "http://127.0.0.1:8765", "gateway_token": "synthetic-local"} if gateway else {}
    argv, env = launcher.build_invocation(config, provider, model, "default", extra, **options)
    assert argv[:2] == [sys.executable, str(entry.resolve())]
    result = subprocess.run(argv, env=env, capture_output=True, text=True, check=True, timeout=5)
    received = json.loads(result.stdout)
    assert received[received.index("--model") + 1] == model
    assert received[-2:] == extra
    assert "SHIM_EXECUTED" not in result.stdout


def test_unknown_batch_shim_is_a_typed_launch_failure(tmp_path, monkeypatch):
    from claudapter import launcher
    from claudapter.config import Config, Provider
    from claudapter.errors import ClaudapterError

    shim = tmp_path / (COMMAND + ".cmd")
    shim.write_text("@echo off\necho SHIM_EXECUTED\n", encoding="ascii")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: str(shim))
    with pytest.raises(ClaudapterError, match="Cannot safely run"):
        launcher.build_invocation(Config(), Provider("fixture", "Fixture", "https://example.invalid/v1"),
                                  "model", "default", [])

@pytest.mark.parametrize("model,extra", [("bad\0model", []), ("valid", ["bad\0prompt"])])
def test_nul_arguments_are_typed_errors_before_starting_process(tmp_path, monkeypatch, model, extra):

    from claudapter import launcher
    from claudapter.config import Config, Provider
    from claudapter.errors import ClaudapterError

    monkeypatch.setattr(launcher.shutil, "which", lambda name: COMMAND + ".exe")
    monkeypatch.setattr(launcher, "get_token", lambda *args, **kwargs: "synthetic-only")
    provider = Provider("fixture", "Fixture", "https://example.invalid/v1")
    config = Config(path=tmp_path / "settings.toml")
    with pytest.raises(ClaudapterError, match="without NUL"):
        launcher.build_invocation(config, provider, model, "default", extra)


