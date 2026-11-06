#Requires -Version 5.1
<#
.SYNOPSIS
    Reinstall the gateway package from a local source tree.

.DESCRIPTION
    Keeps .env, models, data, and logs. Refuses a source version
    older than the installed package unless -AllowDowngrade is set.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$InstallRoot,
    [string]$SourceRoot,
    [switch]$AllowDowngrade
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    Write-Error 'The update script runs on Windows.'
    exit 1
}
if (-not $InstallRoot) {
    $InstallRoot = Join-Path $env:USERPROFILE 'AI\local-ai-switchboard'
}

. (Join-Path $PSScriptRoot 'Common.ps1')
$DryRun = [bool]$WhatIfPreference
Initialize-InstallLog -Directory (Join-Path $InstallRoot 'logs') -DryRun:$DryRun

if (-not $SourceRoot) {
    $parent = Split-Path -Parent $PSScriptRoot
    if (Test-Path -LiteralPath (Join-Path $parent 'pyproject.toml')) {
        $SourceRoot = $parent
    } else {
        $marker = Join-Path $InstallRoot 'config\source-root.txt'
        if (Test-Path -LiteralPath $marker) {
            $SourceRoot = ([System.IO.File]::ReadAllText($marker)).Trim()
        }
    }
}
if (-not $SourceRoot -or -not (Test-Path -LiteralPath (Join-Path $SourceRoot 'pyproject.toml'))) {
    Write-InstallLog 'The source tree was not found. Pass -SourceRoot.' 'ERROR'
    exit 1
}

$python = Join-Path $InstallRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-InstallLog 'The gateway virtual environment is missing.' 'ERROR'
    exit 1
}
$sourceVersion = Read-ProjectVersion -SourceRoot $SourceRoot
$installedOutput = @(& $python -c 'import importlib.metadata as metadata; print(metadata.version("local-ai-switchboard"))')
if ($LASTEXITCODE -ne 0) {
    Write-InstallLog 'The installed gateway version could not be read.' 'ERROR'
    exit 1
}
$installedVersion = [string]@($installedOutput)[-1]
Write-InstallLog "Installed $installedVersion. Source $sourceVersion."
if ((Compare-DottedVersion -Left $sourceVersion -Right $installedVersion) -lt 0 -and -not $AllowDowngrade) {
    Write-InstallLog 'The source version is older than the installed version. Pass -AllowDowngrade to continue.' 'ERROR'
    exit 1
}
if ($DryRun) {
    Write-InstallLog 'Would stop the gateway, reinstall the local project, and leave models, data, and .env in place.'
    Write-InstallLog 'Dry run finished. No component was installed.'
    exit 0
}

$wasRunning = (Test-HttpStatus -Url 'http://127.0.0.1:8787/health/live' -TimeoutSeconds 3) -eq 200
if ($wasRunning) {
    & (Join-Path $PSScriptRoot 'stop.ps1') -InstallRoot $InstallRoot
}
& $python -m pip install --disable-pip-version-check --no-input $SourceRoot
if ($LASTEXITCODE -ne 0) {
    Write-InstallLog 'pip could not install the source tree.' 'ERROR'
    exit 1
}
$updatedOutput = @(& $python -c 'import importlib.metadata as metadata; print(metadata.version("local-ai-switchboard"))')
if ($LASTEXITCODE -ne 0) {
    Write-InstallLog 'The gateway package could not be imported after the update.' 'ERROR'
    exit 1
}
$updatedVersion = [string]@($updatedOutput)[-1]
if ($updatedVersion -ne $sourceVersion) {
    Write-InstallLog "The imported version is $updatedVersion." 'ERROR'
    exit 1
}
$service = Join-Path $InstallRoot 'service'
New-Item -ItemType Directory -Force -Path $service | Out-Null
$scriptHome = if (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) 'pyproject.toml')) {
    $PSScriptRoot
} else {
    $PSScriptRoot
}
foreach ($name in @('Common.ps1', 'pins.json', 'start.ps1', 'stop.ps1', 'doctor.ps1', 'uninstall.ps1', 'update.ps1')) {
    $source = Join-Path $scriptHome $name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $service $name) -Force
    }
}
Write-Utf8File -Path (Join-Path $InstallRoot 'config\source-root.txt') -Text $SourceRoot
if ($wasRunning) {
    & (Join-Path $PSScriptRoot 'start.ps1') -InstallRoot $InstallRoot
    if ($LASTEXITCODE -ne 0) {
        Write-InstallLog 'The package updated and the gateway did not return to /health/live.' 'ERROR'
        exit 1
    }
}
Write-InstallLog "Gateway package $updatedVersion is imported. Models, data, and .env were left in place."
exit 0
