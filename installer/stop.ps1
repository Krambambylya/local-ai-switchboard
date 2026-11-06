#Requires -Version 5.1
<#
.SYNOPSIS
    Stop the gateway. ComfyUI stays running unless -IncludeComfyUI is set.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$InstallRoot,
    [switch]$IncludeComfyUI,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    Write-Error 'The gateway stop script runs on Windows.'
    exit 1
}
if (-not $InstallRoot) {
    $InstallRoot = Join-Path $env:USERPROFILE 'AI\local-ai-switchboard'
}

. (Join-Path $PSScriptRoot 'Common.ps1')
$DryRun = [bool]$WhatIfPreference
Open-ServiceLog -InstallRoot $InstallRoot

$processId = Get-GatewayProcessId -InstallRoot $InstallRoot
if ($processId -eq 0) {
    Write-InstallLog 'No recorded gateway process is running.'
} else {
    $stopped = Stop-OwnedProcess -ProcessId $processId -Label 'gateway' -AllowForce -DryRun:$DryRun
    if (-not $DryRun -and $stopped) {
        Remove-Item -LiteralPath (Join-Path $InstallRoot 'data\gateway.pid') -Force -ErrorAction SilentlyContinue
    }
    if (-not $stopped -and -not $DryRun) {
        exit 1
    }
}

if ($IncludeComfyUI) {
    $portable = Find-ComfyPortableRoot -Path (Join-Path $InstallRoot 'ComfyUI_windows_portable')
    if (-not $portable) {
        Write-InstallLog 'The ComfyUI portable tree was not found.' 'WARN'
        exit 1
    }
    $ids = @(Get-ComfyProcessIds -PortableRoot $portable)
    if ($ids.Count -eq 0) {
        Write-InstallLog 'No ComfyUI portable process is listening with --listen 127.0.0.1.'
        exit 0
    }
    $failed = $false
    foreach ($comfyId in $ids) {
        $stoppedComfy = Stop-OwnedProcess -ProcessId $comfyId -Label 'ComfyUI' -AllowForce:$Force -DryRun:$DryRun
        if (-not $stoppedComfy) {
            $failed = $true
        }
    }
    if ($failed) {
        exit 1
    }
}
exit 0
