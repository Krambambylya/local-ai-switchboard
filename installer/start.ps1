#Requires -Version 5.1
<#
.SYNOPSIS
    Start the Local AI Switchboard gateway in the current user session.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot,
    [int]$ReserveVramMb = -1
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    Write-Error 'The gateway start script runs on Windows.'
    exit 1
}
if (-not $InstallRoot) {
    $InstallRoot = Join-Path $env:USERPROFILE 'AI\local-ai-switchboard'
}

. (Join-Path $PSScriptRoot 'Common.ps1')
Open-ServiceLog -InstallRoot $InstallRoot

$python = Join-Path $InstallRoot 'venv\Scripts\python.exe'
$envFile = Join-Path $InstallRoot '.env'
$apiPort = 8787
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in @(Get-Content -LiteralPath $envFile)) {
        if ($line -match '^\s*#' -or $line -notmatch '=') {
            continue
        }
        $parts = $line.Split('=', 2)
        $name = $parts[0].Trim()
        if (-not $name -or ($name -notlike 'LAS_*' -and $name -notlike 'OLLAMA_*')) {
            continue
        }
        Set-Item -Path "Env:$name" -Value $parts[1]
        if ($name -eq 'LAS_API_PORT') {
            $parsedPort = 0
            if ([int]::TryParse($parts[1], [ref]$parsedPort)) {
                $apiPort = $parsedPort
            }
        }
    }
}
if ($ReserveVramMb -ge 0) {
    $env:LAS_COMFYUI_RESERVE_VRAM_MB = [string]$ReserveVramMb
    Write-InstallLog "ComfyUI reserve VRAM override is $ReserveVramMb MiB."
}
$models = Join-Path $InstallRoot 'models\ollama'
if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'Process'))) {
    $env:OLLAMA_HOST = '127.0.0.1:11434'
}
if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'Process')) -and (Test-Path -LiteralPath $models)) {
    $env:OLLAMA_MODELS = $models
}

$liveUrl = "http://127.0.0.1:$apiPort/health/live"
if ((Test-HttpStatus -Url $liveUrl -TimeoutSeconds 3) -eq 200) {
    Write-InstallLog 'The gateway is already answering /health/live.'
    exit 0
}

$existing = Get-GatewayProcessId -InstallRoot $InstallRoot
if ($existing -ne 0) {
    Write-InstallLog "Gateway process $existing is recorded and /health/live did not succeed." 'ERROR'
    exit 1
}
if (-not (Test-Path -LiteralPath $python)) {
    Write-InstallLog "The gateway interpreter is missing at $python." 'ERROR'
    exit 1
}

$stdout = Join-Path $InstallRoot 'logs\gateway.stdout.log'
$stderr = Join-Path $InstallRoot 'logs\gateway.stderr.log'
$proc = Start-Process -FilePath $python -ArgumentList @('-m', 'local_ai_switchboard', 'serve') -WorkingDirectory $InstallRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
$data = Join-Path $InstallRoot 'data'
New-Item -ItemType Directory -Force -Path $data | Out-Null
Write-Utf8File -Path (Join-Path $data 'gateway.pid') -Text "$($proc.Id)"
Write-InstallLog "Started gateway process $($proc.Id)."

$live = Wait-HttpStatus -Url $liveUrl -Accept @(200) -TimeoutSeconds 30
$ready = Test-HttpStatus -Url "http://127.0.0.1:$apiPort/health/ready" -TimeoutSeconds 5
if ($live -ne 200 -or $ready -ne 200) {
    Write-InstallLog "Gateway health returned live $live and ready $ready. See logs\gateway.stderr.log." 'ERROR'
    exit 1
}
Write-InstallLog 'Gateway /health/live and /health/ready returned 200.'
exit 0
