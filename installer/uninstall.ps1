#Requires -Version 5.1
<#
.SYNOPSIS
    Remove the gateway task, firewall rule, and virtual environment.

.DESCRIPTION
    Models, data, logs, and the ComfyUI portable tree stay in place
    unless the matching removal switch is set. The gateway has no credential file.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$InstallRoot,
    [switch]$Silent,
    [switch]$RemoveData,
    [switch]$RemoveModels,
    [switch]$RemoveFirewallOnly
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    Write-Error 'The uninstall script runs on Windows.'
    exit 1
}
if (-not $InstallRoot) {
    $InstallRoot = Join-Path $env:USERPROFILE 'AI\local-ai-switchboard'
}

. (Join-Path $PSScriptRoot 'Common.ps1')
$DryRun = [bool]$WhatIfPreference

if ($RemoveFirewallOnly) {
    Initialize-InstallLog -Directory (Join-Path $InstallRoot 'logs') -DryRun:$DryRun
    $apiPort = Get-ConfiguredApiPort -InstallRoot $InstallRoot -DefaultPort 8787
    Remove-GatewayFirewall -ApiPort $apiPort -DryRun:$DryRun
    exit 0
}

Initialize-InstallLog -Directory (Join-Path $InstallRoot 'logs') -DryRun:$DryRun
if (-not $Silent -and -not $DryRun) {
    $proceed = Confirm-InstallStep -Silent $false -DryRun $false -Prompt 'Remove the gateway virtual environment, logon task, and firewall rule' -DefaultNo
    if (-not $proceed) {
        Write-InstallLog 'Uninstall was left unchanged.'
        exit 1
    }
    if (-not $RemoveData) {
        $RemoveData = Confirm-InstallStep -Silent $false -DryRun $false -Prompt 'Delete gateway data and logs' -DefaultNo
    }
    if (-not $RemoveModels) {
        $RemoveModels = Confirm-InstallStep -Silent $false -DryRun $false -Prompt 'Delete Ollama models stored under the install root' -DefaultNo
    }
}

Write-InstallLog 'Stopping the gateway.'
& (Join-Path $PSScriptRoot 'stop.ps1') -InstallRoot $InstallRoot
Remove-GatewayLogonTask -DryRun:$DryRun

try {
    $apiPort = Get-ConfiguredApiPort -InstallRoot $InstallRoot -DefaultPort 8787
    Remove-GatewayFirewall -ApiPort $apiPort -DryRun:$DryRun
} catch {
    if ($DryRun) {
        throw
    }
    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath,
        '-RemoveFirewallOnly', '-InstallRoot', $InstallRoot
    )
    try {
        $elevatedCode = Start-ElevatedPowerShell -ArgumentList $arguments
        if ($elevatedCode -ne 0) {
            Write-InstallLog "The elevated firewall removal exited $elevatedCode." 'ERROR'
            exit 1
        }
    } catch {
        Write-InstallLog 'The firewall approval prompt was dismissed.' 'ERROR'
        exit 1
    }
}

$paths = @(
    (Join-Path $InstallRoot 'venv'),
    (Join-Path $InstallRoot 'service'),
    (Join-Path $InstallRoot 'cache')
)
if ($RemoveData) {
    $paths += (Join-Path $InstallRoot 'data')
    $paths += (Join-Path $InstallRoot 'logs')
}
if ($RemoveModels) {
    $paths += (Join-Path $InstallRoot 'models')
}
foreach ($path in $paths) {
    if ($DryRun) {
        Write-InstallLog "Would remove $path."
        continue
    }
    if ((Split-Path -Leaf $path) -eq 'logs') {
        $script:LogPath = $null
    }
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
        Write-InstallLog "Removed $path."
    }
}
if ($RemoveModels -and -not $DryRun) {
    $pins = Get-InstallerPins
    $portable = Find-ComfyPortableRoot -Path (Join-Path $InstallRoot 'ComfyUI_windows_portable')
    if ($portable) {
        foreach ($model in @($pins.comfy_models)) {
            $file = Join-Path $portable "ComfyUI\models\$($model.folder)\$($model.filename)"
            if (-not (Test-Path -LiteralPath $file)) {
                continue
            }
            if (Test-PinnedFileHash -Path $file -Algorithm $model.algorithm -Checksum $model.checksum -ExpectedBytes ([int64]$model.expected_bytes)) {
                Remove-Item -LiteralPath $file -Force
                Write-InstallLog "Removed pinned model file $($model.filename)."
            } else {
                Write-InstallLog "Left $($model.filename) in place because its checksum differs from the pin." 'WARN'
            }
        }
    }
}
if (-not $RemoveData) {
    Write-InstallLog 'Data and logs were kept.'
}
if (-not $RemoveModels) {
    Write-InstallLog 'Models and the ComfyUI portable tree were kept.'
}
if ($DryRun) {
    Write-InstallLog 'Dry run finished. No component was installed.'
    exit 0
}
Write-InstallLog 'Uninstall finished.'
exit 0
