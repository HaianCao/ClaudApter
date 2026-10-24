param([switch]$DeleteData, [switch]$ResetEnvironment, [switch]$NonInteractive, [switch]$NoPathChanges)
$ErrorActionPreference = 'Stop'

function Get-AdapterVariable([string]$Name, [EnvironmentVariableTarget]$Scope) {
    return [Environment]::GetEnvironmentVariable($Name, $Scope)
}

function Set-AdapterVariable([string]$Name, [AllowNull()][string]$Value, [EnvironmentVariableTarget]$Scope) {
    [Environment]::SetEnvironmentVariable($Name, $Value, $Scope)
}

function Get-AdapterPath([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    try {
        $expanded = [Environment]::ExpandEnvironmentVariables($Value.Trim().Trim('"'))
        if (-not [IO.Path]::IsPathRooted($expanded)) { return $null }
        return [IO.Path]::GetFullPath($expanded).TrimEnd('\')
    }
    catch { return $null }
}

function Remove-AdapterPathEntries([string[]]$Targets) {
    $normalized = @($Targets | ForEach-Object { Get-AdapterPath $_ } | Where-Object { $_ })
    if (-not $normalized.Count) { throw 'Wrapper PATH targets must be absolute paths.' }
    $changed = $false
    foreach ($scope in @('User', 'Process')) {
        $current = Get-AdapterVariable 'Path' $scope
        if ([string]::IsNullOrEmpty($current)) { continue }
        $kept = @($current -split ';' | Where-Object { $normalized -notcontains (Get-AdapterPath $_) })
        $updated = $kept -join ';'
        if ($updated -cne $current) {
            Set-AdapterVariable 'Path' $updated $scope
            $changed = $true
        }
    }
    $machine = Get-AdapterVariable 'Path' 'Machine'
    if (@($machine -split ';' | Where-Object { $normalized -contains (Get-AdapterPath $_) }).Count) {
        Write-Warning 'Adapter wrapper entries also exist in machine PATH; they were not changed. Ask your administrator to review them.'
    }
    return $changed
}

function Remove-AdapterDirectory([string]$Target, [string]$Parent, [string]$Leaf) {
    $resolved = [IO.Path]::GetFullPath($Target).TrimEnd('\')
    $expectedParent = [IO.Path]::GetFullPath($Parent).TrimEnd('\')
    if ((Split-Path -Parent $resolved).TrimEnd('\') -ine $expectedParent -or (Split-Path -Leaf $resolved) -ine $Leaf) {
        throw 'Safety check failed: unexpected adapter directory.'
    }
    if (Test-Path -LiteralPath $resolved) {
        if ((Get-Item -LiteralPath $resolved -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Refusing to remove a linked adapter directory.'
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
        return $true
    }
    return $false
}

function Invoke-AdapterUninstall {
    param(
        [string]$Name, [string[]]$InstallNames, [string[]]$DataNames,
        [string[]]$EnvironmentNames, [string[]]$PipxNames,
        [switch]$DeleteData, [switch]$ResetEnvironment, [switch]$NonInteractive, [switch]$NoPathChanges
    )
    if ($DeleteData -and $NonInteractive) {
        throw '-DeleteData requires an interactive DELETE confirmation. Omit -NonInteractive.'
    }
    if (-not [IO.Path]::IsPathRooted($env:APPDATA) -or -not [IO.Path]::IsPathRooted($env:LOCALAPPDATA)) {
        throw 'APPDATA and LOCALAPPDATA must be absolute paths before uninstalling.'
    }
    Write-Host "This removes $Name only, not the official coding CLI." -ForegroundColor Cyan
    Write-Host 'Provider settings and API keys are kept unless you confirm a separate data deletion.'
    if (-not $NonInteractive -and (Read-Host 'Continue? [y/N]') -notmatch '^(y|yes)$') {
        Write-Host 'Uninstall cancelled.'
        return
    }

    $programsRoot = Join-Path $env:LOCALAPPDATA 'Programs'
    $removed = $false
    $pathTargets = @()
    foreach ($installName in $InstallNames) {
        $target = Join-Path $programsRoot $installName
        $pathTargets += Join-Path $target 'venv\Scripts'
        if (Remove-AdapterDirectory $target $programsRoot $installName) { $removed = $true }
    }
    if ($removed) { Write-Host "Removed $Name program files." }
    else { Write-Host "No dedicated $Name installation was found." }

    if (-not $NonInteractive) {
        $pipx = Get-Command pipx -ErrorAction SilentlyContinue
        if ($pipx) {
            try {
                $state = (& $pipx.Source list --json 2>$null | Out-String) | ConvertFrom-Json
                foreach ($package in $PipxNames) {
                    if ($state.venvs.PSObject.Properties.Name -contains $package -and (Read-Host "Remove the pipx copy of $package too? [y/N]") -match '^(y|yes)$') {
                        & $pipx.Source uninstall $package
                        if ($LASTEXITCODE -ne 0) { Write-Warning "Could not remove $package from pipx." }
                        else { Write-Host "Removed $package from pipx." }
                    }
                }
            }
            catch { Write-Warning 'Could not inspect pipx installations.' }
        }
    }

    if (-not $NoPathChanges) {
        if (Remove-AdapterPathEntries $pathTargets) {
            Write-Host 'Removed adapter wrapper entries from user and process PATH; other entries were kept.'
        }
        else { Write-Host 'No adapter wrapper entries were found in user or process PATH.' }
    }
    else { Write-Host 'PATH left unchanged (-NoPathChanges).' }

    Write-Host ("Adapter environment overrides: " + ($EnvironmentNames -join ', '))
    $reset = [bool]$ResetEnvironment
    if (-not $reset -and -not $NonInteractive) {
        $reset = (Read-Host 'Also reset these adapter environment overrides? [y/N]') -match '^(y|yes)$'
    }
    if ($reset) {
        foreach ($variable in $EnvironmentNames) {
            Set-AdapterVariable $variable $null 'User'
            Set-AdapterVariable $variable $null 'Process'
            if (Get-AdapterVariable $variable 'Machine') {
                Write-Warning "$variable is also set at machine scope; it was not changed. Ask your administrator to review it."
            }
        }
        Write-Host 'Reset adapter environment overrides. Provider keys, .env files and shared coding CLI variables were not removed.'
    }
    else { Write-Host 'Kept adapter environment overrides.' }

    $delete = $false
    if (-not $NonInteractive) {
        $delete = (Read-Host 'Also permanently delete default provider settings, API keys and cache? Type DELETE') -ceq 'DELETE'
    }
    if ($delete) {
        foreach ($dataName in $DataNames) {
            foreach ($parent in @($env:APPDATA, $env:LOCALAPPDATA)) {
                [void](Remove-AdapterDirectory (Join-Path $parent $dataName) $parent $dataName)
            }
        }
        Write-Host 'Deleted default provider settings, API keys and cache. This cannot be undone by the uninstaller.'
    }
    else { Write-Host 'Kept provider settings, API keys and cache.' }
    Write-Host 'Custom config/key/cache paths are never deleted; environment reset only removes their overrides.'
    Write-Host ("Default settings: " + (Join-Path $env:APPDATA $DataNames[0]))
    Write-Host ($Name + ' uninstall complete. Open a new terminal to refresh PATH and environment variables.') -ForegroundColor Green
}

try {
    Invoke-AdapterUninstall -Name 'ClaudApter' -InstallNames @('claudapter') -DataNames @('claudapter') -EnvironmentNames @('CLAUDE_CA_CONFIG', 'CLAUDE_CA_CACHE', 'CLAUDE_CA_ENV_FILE') -PipxNames @('claudapter') -DeleteData:$DeleteData -ResetEnvironment:$ResetEnvironment -NonInteractive:$NonInteractive -NoPathChanges:$NoPathChanges
}
catch {
    Write-Host ("Uninstall failed: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
