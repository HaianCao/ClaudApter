"""Real Windows venv/pip fault injection; only synthetic wheels and temporary roots."""

import json
import os
import subprocess
import sys
import time
import tomllib
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows installer")
ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["name"]
MODULE, COMMAND = {"codexapter": ("codex_adapter", "codex-ca"), "claudapter": ("claudapter", "claude-ca")}[PROJECT]
HELPER = ROOT / "scripts" / "install-environment.ps1"

HARNESS = r"""
$ErrorActionPreference = 'Stop'
. $env:TEST_HELPER
$lock = Open-AdapterInstallLock $env:TEST_ROOT
try {
    if ($env:TEST_ACTION -eq 'hold') {
        [IO.File]::WriteAllText($env:TEST_MARKER, 'locked')
        Start-Sleep -Seconds 60
        exit 0
    }
    if ($env:TEST_ACTION -eq 'resolve') {
        Write-Output ('PYTHON:' + (Get-AdapterInstalledPython $env:TEST_ROOT))
        exit 0
    }
    if ($env:TEST_ACTION -eq 'rollback') {
        Set-AdapterCurrentEnvironment $env:TEST_ROOT $env:TEST_GENERATION
        exit 0
    }
    $candidate = New-AdapterEnvironment -Root $env:TEST_ROOT -Python $env:TEST_PYTHON -Target $env:TEST_WHEEL -Module $env:TEST_MODULE -Command $env:TEST_COMMAND
    $held = $null
    try {
        if ($env:TEST_ACTION -eq 'block_activation') {
            $held = [IO.File]::Open((Join-Path $env:TEST_ROOT 'current.txt'), 'Open', 'Read', 'Read')
        }
        Set-AdapterCurrentEnvironment $env:TEST_ROOT $candidate.Id
    }
    finally { if ($held) { $held.Dispose() } }
}
finally { $lock.Dispose() }
"""


def wheel(directory, version, *, broken=False, slow=False):
    directory.mkdir(exist_ok=True)
    path = directory / f"{PROJECT}-{version}-py3-none-any.whl"
    info = f"{PROJECT}-{version}.dist-info"
    main = "import json, os, sys, time\n"
    if slow:
        main += "open(os.environ['TEST_MARKER'], 'w').write('candidate')\ntime.sleep(60)\n"
    main += "raise SystemExit(9)\n" if broken else f"print(json.dumps({{'version': '{version}', 'args': sys.argv[1:]}}))\nif '--exit-seven' in sys.argv: raise SystemExit(7)\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{MODULE}/__init__.py", "")
        archive.writestr(f"{MODULE}/cli.py", main)
        archive.writestr(f"{info}/METADATA", f"Metadata-Version: 2.1\nName: {PROJECT}\nVersion: {version}\n")
        archive.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nGenerator: synthetic-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{info}/RECORD", "")
    return path


def environment(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    return dict(os.environ, TEST_HELPER=str(HELPER), TEST_ROOT=str(tmp_path / "adapter with spaces"),
                TEST_PYTHON=sys.executable, TEST_MODULE=MODULE, TEST_COMMAND=COMMAND,
                TEST_MARKER=str(tmp_path / "marker"), TEST_ACTION="install",
                APPDATA=str(profile / "roaming"), LOCALAPPDATA=str(profile / "local"), USERPROFILE=str(profile),
                HOME=str(profile), CODEX_HOME=str(profile / "codex"), CLAUDE_CONFIG_DIR=str(profile / "claude"),
                PIP_NO_INDEX="1", PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1")


def install(env, target, *, action="install"):
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", HARNESS],
                          env=dict(env, TEST_WHEEL=str(target), TEST_ACTION=action), capture_output=True,
                          text=True, errors="replace", timeout=90, check=False)


def launch(env, *args):
    command = str(Path(env["TEST_ROOT"]) / "bin" / f"{COMMAND}.exe")
    result = subprocess.run([command, *args], env=env,
                            capture_output=True, text=True, errors="replace", timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def assert_ok(result):
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("failure", ["pip", "entrypoint", "activation"])
def test_failed_update_preserves_pointer_old_environment_and_working_launcher(tmp_path, failure):
    env = environment(tmp_path)
    assert_ok(install(env, wheel(tmp_path, "9.0.1")))
    root = Path(env["TEST_ROOT"])
    pointer = (root / "current.txt").read_bytes()
    previous = root / "environments" / pointer.decode().strip()
    if failure == "pip":
        candidate = tmp_path / f"{PROJECT}-9.0.2-py3-none-any.whl"
        candidate.write_bytes(b"not a wheel")
    else:
        candidate = wheel(tmp_path, "9.0.2", broken=failure == "entrypoint")
    result = install(env, candidate, action="block_activation" if failure == "activation" else "install")
    assert result.returncode != 0
    assert (root / "current.txt").read_bytes() == pointer
    assert (previous / "Scripts" / "python.exe").is_file()
    assert launch(env, "--help")["version"] == "9.0.1"
    assert_ok(install(env, wheel(tmp_path, "9.0.3")))
    arguments = ["hello world", "bang!value", "%PATH%", "a&b|c^d", "", 'a"b', "Tiếng Việt"]
    assert launch(env, *arguments)["args"] == arguments
    assert launch(env, "--help")["version"] == "9.0.3"
    assert previous.is_dir()  # Existing sessions can still use their old interpreter.
    assert_ok(install(dict(env, TEST_GENERATION=pointer.decode().strip()), "unused", action="rollback"))
    assert launch(env, "--help")["version"] == "9.0.1"


def wait_marker(env, process):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and process.poll() is None:
        if Path(env["TEST_MARKER"]).exists():
            return
        time.sleep(0.05)
    raise AssertionError("isolated worker did not reach fault-injection checkpoint")


def kill_tree(process):
    if process.poll() is None:
        # Only the tree of the process created by this test; never enumerate user processes.
        subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, timeout=15, check=False)
    process.wait(timeout=15)


@pytest.mark.parametrize("action", ["hold", "install"])
def test_killed_candidate_or_lock_holder_cannot_damage_active_generation(tmp_path, action):
    env = environment(tmp_path)
    assert_ok(install(env, wheel(tmp_path, "9.0.1")))
    pointer = Path(env["TEST_ROOT"]) / "current.txt"
    original = pointer.read_bytes()
    target = wheel(tmp_path, "9.0.2", slow=True)
    with (tmp_path / "worker.log").open("w") as log:
        worker = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", HARNESS],
                                  env=dict(env, TEST_ACTION=action, TEST_WHEEL=str(target)), stdout=log, stderr=log)
        try:
            wait_marker(env, worker)
            concurrent = install(env, wheel(tmp_path, "9.0.3"))
            assert concurrent.returncode != 0
            assert "installation is running" in concurrent.stderr
            assert launch(env, "--help")["version"] == "9.0.1"
        finally:
            kill_tree(worker)
    assert pointer.read_bytes() == original
    assert_ok(install(env, wheel(tmp_path, "9.0.4")))
    assert launch(env, "--help")["version"] == "9.0.4"


def test_legacy_environment_is_detected_and_never_modified(tmp_path):
    env = environment(tmp_path)
    legacy = Path(env["TEST_ROOT"]) / "venv" / "Scripts" / "python.exe"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy-sentinel-not-executed")
    result = install(env, "unused", action="resolve")
    assert_ok(result)
    assert str(legacy) in result.stdout
    assert_ok(install(env, wheel(tmp_path, "9.0.1")))
    assert legacy.read_bytes() == b"legacy-sentinel-not-executed"
    assert launch(env, "--help")["version"] == "9.0.1"
    result = subprocess.run([str(Path(env["TEST_ROOT"]) / "bin" / f"{COMMAND}.exe"), "--exit-seven"],
                            env=env, capture_output=True, timeout=15, check=False)
    assert result.returncode == 7
    executable = str(Path(env["TEST_ROOT"]) / "bin" / f"{COMMAND}.exe")
    for arguments in (["cmd.exe", "/d", "/c", executable, "--help"],
                      ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "& $env:TEST_LAUNCHER --help"]):
        result = subprocess.run(arguments, env=dict(env, TEST_LAUNCHER=executable),
                                capture_output=True, text=True, errors="replace", timeout=20, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["version"] == "9.0.1"


def test_invalid_pointer_is_rejected_without_replacing_it(tmp_path):
    env = environment(tmp_path)
    root = Path(env["TEST_ROOT"])
    root.mkdir()
    pointer = root / "current.txt"
    pointer.write_text("../../other", encoding="ascii")
    result = install(env, "unused", action="resolve")
    assert result.returncode != 0
    assert "Invalid adapter environment pointer" in result.stderr
    assert pointer.read_text() == "../../other"
