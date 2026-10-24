"""Exercise real uninstall logic with temporary files and a mocked env backend.

Never write the user's registry/PATH or run a real wrapper uninstallation.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PREFIX = "CLAUDE_CA"
INSTALL = "claudapter"
DATA = "claudapter"

HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:HOTFIX_SCRIPT, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$definitions = $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]}, $false)
foreach ($definition in $definitions) { . ([scriptblock]::Create($definition.Extent.Text)) }
$fake = @{}
$writes = [Collections.Generic.List[object]]::new()
$names = @(($env:HOTFIX_PREFIX + '_CONFIG'), ($env:HOTFIX_PREFIX + '_CACHE'), ($env:HOTFIX_PREFIX + '_ENV_FILE'))
foreach ($name in $names) {
    $fake["User|$name"] = 'fixture-override'
    $fake["Process|$name"] = 'fixture-override'
}
$fake["Machine|$($names[0])"] = 'machine-fixture'
$fake['User|ANTHROPIC_AUTH_TOKEN'] = 'shared-fixture'
$fake['User|OPENAI_API_KEY'] = 'shared-fixture'
$fake['User|PROVIDER_TOKEN'] = 'provider-fixture'
$install = Join-Path (Join-Path $env:LOCALAPPDATA 'Programs') $env:HOTFIX_INSTALL
$wrapper = Join-Path $install 'venv\Scripts'
$config = Join-Path $env:APPDATA $env:HOTFIX_DATA
$cache = Join-Path $env:LOCALAPPDATA $env:HOTFIX_DATA
New-Item -ItemType Directory -Path $wrapper, $config, $cache -Force | Out-Null
$fake['User|Path'] = '"' + $wrapper + '";C:\Keep'
$fake['Process|Path'] = $wrapper.ToUpperInvariant() + '\;C:\ProcessKeep'
$fake['Machine|Path'] = $wrapper
if ($env:HOTFIX_SCENARIO -eq 'no_matches') {
    $fake['User|Path'] = 'C:\Keep'
    $fake['Process|Path'] = 'C:\ProcessKeep'
}

function Get-AdapterVariable([string]$Name, [EnvironmentVariableTarget]$Scope) {
    return $fake["$Scope|$Name"]
}
function Set-AdapterVariable([string]$Name, [AllowNull()][string]$Value, [EnvironmentVariableTarget]$Scope) {
    $writes.Add([pscustomobject]@{Name=$Name; Scope=[string]$Scope})
    if ([string]::IsNullOrEmpty($Value)) { $fake.Remove("$Scope|$Name") }
    else { $fake["$Scope|$Name"] = $Value }
}
function Get-Command { return $null }
function Read-Host([string]$Prompt) {
    if ($Prompt -like 'Continue*') {
        if ($env:HOTFIX_SCENARIO -eq 'cancel') { return 'n' }
        return 'y'
    }
    if ($Prompt -like 'Also reset*') {
        if ($env:HOTFIX_SCENARIO -eq 'interactive_reset') { return 'y' }
        return 'n'
    }
    if ($Prompt -like 'Also permanently*') {
        if ($env:HOTFIX_SCENARIO -eq 'interactive_delete') { return 'DELETE' }
        return 'n'
    }
    throw "Unexpected prompt: $Prompt"
}
$NonInteractive = $env:HOTFIX_SCENARIO -notlike 'interactive_*' -and $env:HOTFIX_SCENARIO -ne 'cancel'
$ResetEnvironment = $env:HOTFIX_SCENARIO -in @('reset', 'no_path')
$DeleteData = $env:HOTFIX_SCENARIO -eq 'blocked_delete'
$NoPathChanges = $env:HOTFIX_SCENARIO -eq 'no_path'
. ([scriptblock]::Create($ast.EndBlock.Statements[-1].Extent.Text))
$remaining = @($names | Where-Object { $fake.ContainsKey("User|$_") -or $fake.ContainsKey("Process|$_") })
$result = @{
    install_exists = Test-Path -LiteralPath $install
    config_exists = Test-Path -LiteralPath $config
    cache_exists = Test-Path -LiteralPath $cache
    overrides_reset = $remaining.Count -eq 0
    user_path = $fake['User|Path']
    process_path = $fake['Process|Path']
    writes = @($writes.ToArray())
    protected_kept = $fake['User|ANTHROPIC_AUTH_TOKEN'] -eq 'shared-fixture' -and $fake['User|OPENAI_API_KEY'] -eq 'shared-fixture' -and $fake['User|PROVIDER_TOKEN'] -eq 'provider-fixture'
}
Write-Output ('RESULT:' + ($result | ConvertTo-Json -Depth 4 -Compress))
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows uninstaller")
@pytest.mark.parametrize("scenario", ["keep", "reset", "no_path", "no_matches", "interactive_keep", "interactive_reset", "interactive_delete", "cancel", "blocked_delete"])
def test_uninstall_choices_are_scoped_and_independent(tmp_path, scenario):
    script = Path(__file__).resolve().parents[1] / "scripts" / "uninstall.ps1"
    custom = tmp_path / "custom" / ".env"
    custom.parent.mkdir()
    custom.write_text("SYNTHETIC=fixture\n", encoding="utf-8")
    env = dict(os.environ, APPDATA=str(tmp_path / "roaming"), LOCALAPPDATA=str(tmp_path / "local"),
               USERPROFILE=str(tmp_path / "profile"), HOTFIX_SCRIPT=str(script), HOTFIX_PREFIX=PREFIX,
               HOTFIX_INSTALL=INSTALL, HOTFIX_DATA=DATA, HOTFIX_SCENARIO=scenario)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", HARNESS],
                            env=env, encoding="utf-8", errors="replace", capture_output=True, timeout=20, check=False)
    if scenario == "blocked_delete":
        assert result.returncode == 1
        assert "requires an interactive DELETE confirmation" in result.stdout
        assert (tmp_path / "local" / "Programs" / INSTALL).exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(next(line[7:] for line in result.stdout.splitlines() if line.startswith("RESULT:")))
        assert report["protected_kept"]
        assert report["install_exists"] == (scenario == "cancel")
        assert report["config_exists"] == (scenario != "interactive_delete")
        assert report["cache_exists"] == (scenario != "interactive_delete")
        assert report["overrides_reset"] == (scenario in {"reset", "no_path", "interactive_reset"})
        if scenario in {"no_path", "cancel"}:
            assert INSTALL.casefold() in report["user_path"].casefold()
        else:
            assert report["user_path"] == r"C:\Keep"
            assert report["process_path"] == r"C:\ProcessKeep"
        assert all(write["Scope"] != "Machine" and write["Name"] in {"Path", PREFIX + "_CONFIG", PREFIX + "_CACHE", PREFIX + "_ENV_FILE"} for write in report["writes"])
        if scenario == "no_matches":
            assert not report["writes"]
            assert "No adapter wrapper entries were found" in result.stdout
        if scenario not in {"no_path", "cancel"}:
            assert "machine PATH" in result.stdout
    assert custom.read_text(encoding="utf-8") == "SYNTHETIC=fixture\n"

