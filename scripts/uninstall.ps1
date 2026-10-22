param([switch]$DeleteData, [switch]$NonInteractive)
$ErrorActionPreference = 'Stop'
$installRoot = Join-Path $env:LOCALAPPDATA 'Programs\claudapter'
$scripts = Join-Path $installRoot 'venv\Scripts'
$dataRoot = Join-Path $env:APPDATA 'claudapter'
$cacheRoot = Join-Path $env:LOCALAPPDATA 'claudapter'
Write-Host 'This removes only ClaudApter, not the official Claude Code CLI.' -ForegroundColor Cyan
if (-not $NonInteractive -and (Read-Host 'Continue? Type Y to uninstall [y/N]') -notmatch '^(y|yes)$') { return }
$programsRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Programs')).TrimEnd('\')
$resolvedInstall = [IO.Path]::GetFullPath($installRoot).TrimEnd('\')
if ((Split-Path -Parent $resolvedInstall).TrimEnd('\') -ine $programsRoot -or (Split-Path -Leaf $resolvedInstall) -ine 'claudapter') { throw 'Unexpected installation path.' }
if (Test-Path -LiteralPath $installRoot) {
    if ((Get-Item -LiteralPath $installRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Refusing to remove a linked installation directory.' }
    Remove-Item -LiteralPath $installRoot -Recurse -Force
}
$current = [Environment]::GetEnvironmentVariable('Path', 'User')
$kept = @($current -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ine $scripts.TrimEnd('\') })
[Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
$env:Path = (@($env:Path -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ine $scripts.TrimEnd('\') }) -join ';')
if ($DeleteData) {
    if ((Read-Host 'Permanently delete ClaudApter config, API keys and cache? Type DELETE') -ceq 'DELETE') {
        foreach ($target in @($dataRoot, $cacheRoot)) {
            $parent = if ($target -eq $dataRoot) { $env:APPDATA } else { $env:LOCALAPPDATA }
            $resolvedParent = [IO.Path]::GetFullPath($parent).TrimEnd('\')
            $resolvedTarget = [IO.Path]::GetFullPath($target).TrimEnd('\')
            if ((Split-Path -Parent $resolvedTarget).TrimEnd('\') -ine $resolvedParent -or (Split-Path -Leaf $resolvedTarget) -ine 'claudapter') { throw 'Unexpected data path.' }
            if (Test-Path -LiteralPath $target) {
                if ((Get-Item -LiteralPath $target -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Refusing to remove a linked data directory.' }
                Remove-Item -LiteralPath $target -Recurse -Force
            }
        }
        Write-Host 'ClaudApter config, API keys and cache were deleted.'
    }
    else { Write-Host "Provider config and API keys were preserved at $dataRoot" }
}
else { Write-Host "Provider config and API keys were preserved at $dataRoot" }
Write-Host 'ClaudApter uninstall complete. Open a new terminal to refresh PATH.' -ForegroundColor Green
