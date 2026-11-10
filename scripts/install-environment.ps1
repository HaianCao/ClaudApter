# Shared verbatim by the two independent adapters. No user data or PATH writes.
# Environments are built at their final paths: Windows venv entry points are not relocatable.
function Assert-AdapterInstallDirectory([string]$Directory) {
    if (-not [IO.Path]::IsPathRooted($Directory)) { throw 'Installation directory must be absolute.' }
    if ((Test-Path -LiteralPath $Directory) -and ((Get-Item -LiteralPath $Directory -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Refusing to install through a linked adapter directory.'
    }
}

function Open-AdapterInstallLock([string]$Root) {
    Assert-AdapterInstallDirectory $Root
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
    foreach ($name in @('environments', 'bin', 'current.txt', 'install.lock')) {
        Assert-AdapterInstallDirectory (Join-Path $Root $name)
    }
    try { return [IO.File]::Open((Join-Path $Root 'install.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { throw 'Another installation is running, or the adapter installation is locked. Close it and retry.' }
}

function Get-AdapterInstalledPython([string]$Root) {
    $pointer = Join-Path $Root 'current.txt'
    if (Test-Path -LiteralPath $pointer) {
        Assert-AdapterInstallDirectory $pointer
        $id = [IO.File]::ReadAllText($pointer).Trim()
        if ($id -notmatch '^[a-f0-9]{32}$') { throw 'Invalid adapter environment pointer; installation was preserved.' }
        $directory = Join-Path (Join-Path $Root 'environments') $id
        Assert-AdapterInstallDirectory (Join-Path $Root 'environments')
        Assert-AdapterInstallDirectory $directory
        $python = Join-Path $directory 'Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Active adapter environment is missing; installation was preserved.' }
        return $python
    }
    $legacy = Join-Path $Root 'venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $legacy -PathType Leaf) { return $legacy }
    return $null
}

function Set-AdapterCurrentEnvironment([string]$Root, [string]$Id) {
    if ($Id -notmatch '^[a-f0-9]{32}$') { throw 'Invalid environment ID.' }
    $environments = Join-Path $Root 'environments'
    $directory = Join-Path $environments $Id
    Assert-AdapterInstallDirectory $Root
    Assert-AdapterInstallDirectory $environments
    Assert-AdapterInstallDirectory $directory
    if (-not (Test-Path -LiteralPath (Join-Path $directory 'Scripts\python.exe') -PathType Leaf)) {
        throw 'Cannot activate a missing candidate environment.'
    }
    $current = Join-Path $Root 'current.txt'
    Assert-AdapterInstallDirectory $current
    $pending = Join-Path $Root ("current-{0}.tmp" -f [guid]::NewGuid().ToString('N'))
    try {
        $bytes = [Text.Encoding]::ASCII.GetBytes($Id + "`r`n")
        $file = [IO.File]::Open($pending, 'CreateNew', 'Write', 'None')
        try { $file.Write($bytes, 0, $bytes.Length); $file.Flush($true) }
        finally { $file.Dispose() }
        # Same-volume replace is the only publication point. Never delete current first.
        if (Test-Path -LiteralPath $current) { [IO.File]::Replace($pending, $current, [NullString]::Value) }
        else { [IO.File]::Move($pending, $current) }
    }
    finally { if (Test-Path -LiteralPath $pending) { Remove-Item -LiteralPath $pending -Force } }
}

function Get-AdapterVerifiedWheel {
    param([string]$Uri, [string]$Sha256, [string]$Directory)
    # Download the pinned Protoverta wheel and refuse it unless its SHA-256
    # matches the hash this installer was released with.
    if ($Sha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'This installer has no pinned Protoverta hash (unreleased build). Pass -ProtovertaWheel PATH with a locally built wheel of the same version.'
    }
    $name = Split-Path -Leaf ([Uri]$Uri).AbsolutePath
    if ($name -notmatch '^protoverta-[0-9A-Za-z.]+-py3-none-any\.whl$') { throw 'Unexpected Protoverta wheel name.' }
    $path = Join-Path $Directory $name
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $path -MaximumRedirection 5 -TimeoutSec 180
    }
    catch {
        throw "The pinned Protoverta release asset could not be downloaded ($Uri): $($_.Exception.Message.TrimEnd('.')). Previous installation was preserved. Retry later, or pass -ProtovertaWheel PATH with a locally built wheel of the same version."
    }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        throw "The downloaded Protoverta wheel does not match its pinned SHA-256 (expected $Sha256, got $actual). Installation stopped; previous installation was preserved."
    }
    return $path
}

function New-AdapterEnvironment {
    param([string]$Root, [string]$Python, [string]$Target, [string[]]$GatewayPackages,
          [string]$Module, [string]$Command)
    if ($Module -notin @('codexapter', 'claudapter') -or $Command -notin @('codex-ca', 'claude-ca')) {
        throw 'Unknown adapter entry point.'
    }
    $id = [guid]::NewGuid().ToString('N')
    $environments = Join-Path $Root 'environments'
    Assert-AdapterInstallDirectory $environments
    New-Item -ItemType Directory -Path $environments -Force | Out-Null
    $candidate = Join-Path $environments $id
    if (Test-Path -LiteralPath $candidate) { throw 'Candidate directory already exists; installation was preserved.' }
    # Never move this venv: pip-created Windows executables contain absolute paths.
    & $Python -I -m venv $candidate | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the candidate Python environment. Previous installation was preserved.' }
    $candidatePython = Join-Path $candidate 'Scripts\python.exe'
    & $candidatePython -I -m pip install --disable-pip-version-check $Target @GatewayPackages | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Candidate package installation failed. Previous installation was preserved.' }
    & $candidatePython -I -m pip check | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Candidate dependency check failed. Previous installation was preserved.' }
    & $candidatePython -I -m "${Module}.cli" --help | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Candidate entry point check failed. Previous installation was preserved.' }
    if ($GatewayPackages.Count) {
        # install.ps1 always passes the pinned Protoverta wheel.
        & $candidatePython -I -c 'import protoverta.server' | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'Candidate gateway check failed. Previous installation was preserved.' }
    }

    $bin = Join-Path $Root 'bin'
    Assert-AdapterInstallDirectory $bin
    $launcher = Join-Path $bin ($Command + '.exe')
    $generator = Join-Path $candidate 'make-launcher.py'
    $generatorSource = @'
import hashlib
import json
import os
import pathlib
import sys
from pip._vendor.distlib.scripts import ScriptMaker

root, module, command, generation = sys.argv[1:]
root = pathlib.Path(root)
bin_dir = root / 'bin'
name = command + '.exe'
if bin_dir.exists():
    manifest = json.loads((bin_dir / 'launcher.json').read_text(encoding='utf-8'))
    if (manifest.get('format'), manifest.get('module'), manifest.get('command')) != (1, module, command):
        raise SystemExit('Unknown launcher format; existing installation preserved')
    if hashlib.sha256((bin_dir / name).read_bytes()).hexdigest() != manifest.get('sha256'):
        raise SystemExit('Launcher integrity check failed; existing installation preserved')
    if not pathlib.Path(manifest['interpreter']).is_file():
        raise SystemExit('Launcher interpreter is missing; existing installation preserved')
else:
    pending = root / ('bin-' + generation)
    pending.mkdir()
    # Use a normal pip Windows launcher, with only a stdlib dispatcher as payload.
    # Its first interpreter is retained along with all published generations.
    template = '''import pathlib, re, subprocess, sys
def main():
    try:
        root = pathlib.Path(sys.argv[0]).resolve().parent.parent
        identity = (root / 'current.txt').read_text(encoding='ascii').strip()
        if re.fullmatch('[a-f0-9]{32}', identity) is None:
            raise ValueError('invalid environment pointer')
        python = root / 'environments' / identity / 'Scripts' / 'python.exe'
        process = subprocess.Popen([str(python), '-I', '-m', '__MODULE__.cli', *sys.argv[1:]])
    except (OSError, ValueError):
        print('No usable adapter environment. Run the installer again.', file=sys.stderr)
        return 1
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            # Console Ctrl+C reaches the child too. Keep the terminal attached
            # until it exits or handles the interrupt; never orphan it here.
            continue
if __name__ == '__main__':
    raise SystemExit(main())
'''.replace('__MODULE__', module)
    compile(template, '<adapter-launcher>', 'exec')
    maker = ScriptMaker(None, str(pending))
    maker.executable = sys.executable
    maker.variants = {''}
    maker.script_template = template
    maker.make(command + ' = unused:main')
    manifest = {'format': 1, 'module': module, 'command': command,
                'interpreter': sys.executable,
                'sha256': hashlib.sha256((pending / name).read_bytes()).hexdigest()}
    (pending / 'launcher.json').write_text(json.dumps(manifest), encoding='utf-8')
    os.rename(pending, bin_dir)
'@
    [IO.File]::WriteAllText($generator, $generatorSource, [Text.Encoding]::UTF8)
    & $candidatePython -I $generator $Root $Module $Command $id | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Candidate launcher check failed. Previous installation was preserved.' }
    return @{ Id = $id; Python = $candidatePython; Launcher = $launcher; Bin = $bin }
}
