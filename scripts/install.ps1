param([switch]$SkipClaudeInstall, [switch]$NonInteractive, [switch]$WithGateway, [string]$ProtovertaWheel, [switch]$NoPathChanges, [string]$SourceBranch = 'main')
$ErrorActionPreference = 'Stop'
$sourceTempRoot = $null
$installLock = $null

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Test-ClaudApterSource([string]$Directory) {
    $project = Join-Path $Directory 'pyproject.toml'
    if (-not (Test-Path -LiteralPath $project)) { return $false }
    return [bool]([regex]::IsMatch((Get-Content -LiteralPath $project -Raw), '(?m)^name\s*=\s*"claudapter"\s*$'))
}

function Get-SupportedPython {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $installed = & $py.Source -0p 2>$null
        foreach ($line in $installed) {
            $match = [regex]::Match([string]$line, '^\s*-\S+\s+(?:\*\s+)?(.+?pythonw?\.exe)\s*$', 'IgnoreCase')
            if ($match.Success) {
                $pythonPath = $match.Groups[1].Value.Trim()
                if (Test-Path -LiteralPath $pythonPath) {
                    & $pythonPath -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>$null
                    if ($LASTEXITCODE -eq 0) { return $pythonPath }
                }
            }
        }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        & $python.Source -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>$null
        if ($LASTEXITCODE -eq 0) { return $python.Source }
    }
    return $null
}

function Add-UserPath([string]$Directory) {
    if ($NoPathChanges) { return }
    if (-not (Test-Path -LiteralPath $Directory)) { return }
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $entries = @($current -split ';' | Where-Object { $_ })
    if (-not ($entries | Where-Object { $_.TrimEnd('\') -ieq $Directory.TrimEnd('\') })) {
        [Environment]::SetEnvironmentVariable('Path', ((@($Directory) + $entries) -join ';'), 'User')
    }
    if (-not (($env:Path -split ';') | Where-Object { $_.TrimEnd('\') -ieq $Directory.TrimEnd('\') })) {
        $env:Path = "$Directory;$env:Path"
    }
}

function Remove-UserPath([string]$Directory) {
    if ($NoPathChanges) { return }
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $entries = @($current -split ';' | Where-Object { $_ })
    $kept = @($entries | Where-Object { $_.TrimEnd('\') -ine $Directory.TrimEnd('\') })
    if ($kept.Count -ne $entries.Count) {
        [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
    }
    $env:Path = (@($env:Path -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ine $Directory.TrimEnd('\') }) -join ';')
}

try {
    if ($SourceBranch -notmatch '^[A-Za-z0-9][A-Za-z0-9._/-]*$' -or $SourceBranch.Contains('..')) {
        throw 'SourceBranch must be a valid Git branch name.'
    }
    $repoRoot = $null
    if ($PSScriptRoot -and -not $PSBoundParameters.ContainsKey('SourceBranch')) {
        $candidate = Split-Path -Parent $PSScriptRoot
        if (Test-ClaudApterSource $candidate) { $repoRoot = $candidate }
    }
    Write-Host 'This installs or updates ClaudApter (claude-ca) for your Windows user.' -ForegroundColor White
    Write-Host 'Missing Python or the official coding CLI may be installed. No administrator access is requested.'
    if (-not $repoRoot) { Write-Host 'ClaudApter source will be downloaded from its GitHub repository.' }
    if (-not $NonInteractive -and (Read-Host 'Continue? [y/N]') -notmatch '^(y|yes)$') { return }

    if (-not $repoRoot) {
        Write-Step 'Downloading ClaudApter source from GitHub'
        $sourceTempRoot = Join-Path ([IO.Path]::GetTempPath()) ("claudapter-source-{0}" -f [guid]::NewGuid())
        $archivePath = "$sourceTempRoot.zip"
        New-Item -ItemType Directory -Path $sourceTempRoot -Force | Out-Null
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/HaianCao/ClaudApter/archive/refs/heads/${SourceBranch}.zip" -OutFile $archivePath
            Expand-Archive -LiteralPath $archivePath -DestinationPath $sourceTempRoot -Force
        }
        finally {
            Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
        }
        $projectFile = Get-ChildItem -LiteralPath $sourceTempRoot -Filter 'pyproject.toml' -File -Recurse | Where-Object { Test-ClaudApterSource $_.Directory.FullName } | Select-Object -First 1
        if (-not $projectFile) { throw 'Downloaded archive does not contain a ClaudApter project.' }
        $repoRoot = $projectFile.Directory.FullName
    }

    Write-Step 'Checking Python 3.11 or newer'
    $pythonExe = Get-SupportedPython
    if (-not $pythonExe) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if (-not $winget) { throw 'Python 3.11+ is required and winget is unavailable. Install Python from python.org, then rerun this file.' }
        Write-Host 'Installing Python 3.13 for the current Windows user through winget...'
        & $winget.Source install --id Python.Python.3.13 --exact --scope user --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw "Python installation failed (winget exit code $LASTEXITCODE)." }
        $pythonExe = Get-SupportedPython
        if (-not $pythonExe) { throw 'Python was installed, but this terminal cannot discover it yet. Open a new terminal and rerun the installer.' }
    }
    Write-Host "Using Python: $pythonExe"

    $installRoot = Join-Path $env:LOCALAPPDATA 'Programs\claudapter'
    . (Join-Path $repoRoot 'scripts\install-environment.ps1')
    $installLock = Open-AdapterInstallLock $installRoot
    $previousPython = Get-AdapterInstalledPython $installRoot
    $isUpdate = [bool]$previousPython
    if ($isUpdate) { Write-Step 'Updating ClaudApter' }
    else { Write-Step 'Installing ClaudApter' }
    $installTarget = $repoRoot
    $gatewayPackages = @()
    if ($previousPython -and -not $WithGateway -and -not $ProtovertaWheel) {
        $hasGateway = & $previousPython -I -c "import importlib.util; print(int(importlib.util.find_spec('protoverta') is not None))"
        if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the existing gateway installation.' }
        if (([string]$hasGateway).Trim() -eq '1') { $WithGateway = $true }
    }
    if ($ProtovertaWheel) {
        if (-not (Test-Path -LiteralPath $ProtovertaWheel -PathType Leaf) -or [IO.Path]::GetExtension($ProtovertaWheel) -ne '.whl') {
            throw 'ProtovertaWheel must point to a local Protoverta wheel file.'
        }
        $gatewayPackages += (Resolve-Path -LiteralPath $ProtovertaWheel).Path
        $WithGateway = $true
    }
    if ($WithGateway) {
        if (-not [regex]::IsMatch((Get-Content -LiteralPath (Join-Path $repoRoot 'pyproject.toml') -Raw), '(?m)^gateway\s*=')) {
            throw 'Selected source has no gateway extra. For the preview use -SourceBranch integration/protoverta.'
        }
        $installTarget = "${repoRoot}[gateway]"
        if (-not $ProtovertaWheel) {
            $protovertaAsset = 'https://github.com/HaianCao/Protoverta/releases/download/v0.3.0a8/protoverta-0.3.0a8-py3-none-any.whl'
            Assert-AdapterReleaseAsset $protovertaAsset
            $gatewayPackages += $protovertaAsset
        }
    }
    $candidate = New-AdapterEnvironment -Root $installRoot -Python $pythonExe -Target $installTarget -GatewayPackages $gatewayPackages -Module 'claudapter' -Command 'claude-ca'

    Write-Step 'Checking the official Claude Code CLI'
    $claudeCommand = Get-Command claude -ErrorAction SilentlyContinue
    $nativeClaude = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
    if (-not $claudeCommand -and (Test-Path -LiteralPath $nativeClaude)) {
        Add-UserPath (Split-Path -Parent $nativeClaude)
        $claudeCommand = $nativeClaude
    }
    if ($SkipClaudeInstall) { Write-Host 'Skipped official CLI installation (-SkipClaudeInstall).' }
    elseif ($claudeCommand) { Write-Host 'Official CLI is already available; left unchanged.' }
    if (-not $claudeCommand -and -not $SkipClaudeInstall) {
        Write-Step 'Installing the official Claude Code CLI'
        $officialScript = Join-Path ([IO.Path]::GetTempPath()) ("claude-official-{0}.ps1" -f [guid]::NewGuid())
        try {
            Invoke-WebRequest -UseBasicParsing -Uri 'https://claude.ai/install.ps1' -OutFile $officialScript
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $officialScript
            if ($LASTEXITCODE -ne 0) { throw 'Official Claude Code installer failed.' }
        }
        finally { Remove-Item -LiteralPath $officialScript -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $nativeClaude) { Add-UserPath (Split-Path -Parent $nativeClaude) }
    }
    if (-not $SkipClaudeInstall -and -not (Get-Command claude -ErrorAction SilentlyContinue) -and -not (Test-Path -LiteralPath $nativeClaude)) {
        Write-Warning 'Claude Code CLI is not on PATH yet. Install it or open a new terminal and verify: claude --version'
    }
    Set-AdapterCurrentEnvironment $installRoot $candidate.Id
    Add-UserPath $candidate.Bin
    Remove-UserPath (Join-Path $installRoot 'venv\Scripts')
    if ($isUpdate) { Write-Host 'ClaudApter update complete. Provider settings and API keys were preserved.' -ForegroundColor Green }
    else { Write-Host 'ClaudApter installation complete.' -ForegroundColor Green }
    Write-Host 'Open a new CMD, PowerShell, or Windows Terminal tab and run: claude-ca setup'
}
catch {
    Write-Host "`nSetup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
finally {
    if ($installLock) { $installLock.Dispose() }
    if ($sourceTempRoot) {
        $tempParent = [IO.Path]::GetFullPath((Split-Path -Parent $sourceTempRoot)).TrimEnd('\')
        $expectedParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
        if ($tempParent -ieq $expectedParent -and (Split-Path -Leaf $sourceTempRoot) -like 'claudapter-source-*' -and (Test-Path -LiteralPath $sourceTempRoot)) {
            Remove-Item -LiteralPath $sourceTempRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
