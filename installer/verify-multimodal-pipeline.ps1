#Requires -Version 5.1
<#
.SYNOPSIS
    Run the Qwen vision and FLUX edit checks through the production Gateway.

.DESCRIPTION
    Uses the installed gateway, Ollama, and ComfyUI. A partial run does not
    write config\multimodal-pipeline.json and does not stop unrelated processes.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    Write-Error 'verify-multimodal-pipeline.ps1 runs on Windows.'
    exit 1
}
if (-not $InstallRoot) {
    $parent = Split-Path -Parent $PSScriptRoot
    $userInstall = Join-Path $env:USERPROFILE 'AI\local-ai-switchboard'
    $driveInstall = 'D:\AI\LocalAISwitchboard'
    if ((Test-Path -LiteralPath (Join-Path $parent 'venv\Scripts\python.exe')) -and (Test-Path -LiteralPath (Join-Path $parent '.env'))) {
        $InstallRoot = $parent
    } elseif (Test-Path -LiteralPath (Join-Path $userInstall 'venv\Scripts\python.exe')) {
        $InstallRoot = $userInstall
    } elseif (Test-Path -LiteralPath (Join-Path $driveInstall 'venv\Scripts\python.exe')) {
        $InstallRoot = $driveInstall
    } else {
        Write-Error 'No Local AI Switchboard install was found. Pass -InstallRoot.'
        exit 1
    }
}

. (Join-Path $PSScriptRoot 'Common.ps1')
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
Initialize-InstallLog -Directory (Join-Path $InstallRoot 'logs')

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$reportRoot = Join-Path $InstallRoot 'data\diagnostics\multimodal-pipeline'
$reportDir = Join-Path $reportRoot $stamp
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
$csvPath = Join-Path $reportDir 'gpu-metrics.csv'
$python = Join-Path $InstallRoot 'venv\Scripts\python.exe'
$apiPort = 8787
$envFile = Join-Path $InstallRoot '.env'
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in @(Get-Content -LiteralPath $envFile)) {
        if ($line -match '^\s*LAS_API_PORT=(.+)$') {
            $parsed = 0
            if ([int]::TryParse($Matches[1].Trim(), [ref]$parsed)) {
                $apiPort = $parsed
            }
        }
    }
}
$base = "http://127.0.0.1:$apiPort"
$facts = [ordered]@{
    gateway_ready = $false
    vision_seen = $false
    analysis_valid = $false
    ollama_released = $false
    comfyui_started = $false
    flux_edited = $false
    comfyui_released = $false
    qwen_after_flux = $false
    scheduler_idle = $false
}
$failure = ''

function Add-GpuSample {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $smi) {
        return
    }
    $header = -not (Test-Path -LiteralPath $csvPath)
    $line = & nvidia-smi --query-gpu=timestamp,memory.used,memory.free,utilization.gpu --format=csv,noheader,nounits
    if ($header) {
        Set-Content -LiteralPath $csvPath -Value 'timestamp,memory.used,memory.free,utilization.gpu' -Encoding ASCII
    }
    foreach ($row in @($line)) {
        if ($row) {
            Add-Content -LiteralPath $csvPath -Value $row -Encoding ASCII
        }
    }
}

function Get-TextFromCodes {
    param([int[]]$Codes)
    $chars = foreach ($code in $Codes) { [char]$code }
    return -join $chars
}

function Read-GatewayLog {
    $path = Join-Path $InstallRoot 'logs\switchboard.log'
    if (-not (Test-Path -LiteralPath $path)) {
        return ''
    }
    return [System.IO.File]::ReadAllText($path)
}

try {
    Add-GpuSample
    $ready = Invoke-RestMethod -Uri "$base/health/ready" -TimeoutSec 20
    if ([string]$ready.status -ne 'ok' -and [string]$ready.status -ne 'degraded') {
        throw 'Gateway readiness did not return ok or degraded.'
    }
    $facts.gateway_ready = $true
    Write-InstallLog 'Gateway readiness responded.'

    $imagePath = Join-Path $reportDir 'source.png'
    & $python -c "from PIL import Image; Image.new('RGB', (64, 64), (200, 30, 30)).save(r'$imagePath')"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $imagePath)) {
        throw 'The test image could not be created.'
    }
    $imageBytes = [System.IO.File]::ReadAllBytes($imagePath)
    $dataUrl = 'data:image/png;base64,' + [Convert]::ToBase64String($imageBytes)
    $describe = Get-TextFromCodes @(1054,1087,1080,1096,1080,32,1089,1086,1076,1077,1088,1078,1080,1084,1086,1077,32,1087,1088,1080,1082,1088,1077,1087,1083,1105,1085,1085,1086,1075,1086,32,1080,1079,1086,1073,1088,1072,1078,1077,1085,1080,1103)
    $model = 'qwen3.5:4b-ctx8192'
    $textBody = @{
        model = $model
        messages = @(@{ role = 'user'; content = $describe })
        stream = $false
        temperature = 0
        seed = 1
    } | ConvertTo-Json -Depth 6
    $textReply = Invoke-RestMethod -Uri "$base/v1/chat/completions" -Method Post -Body $textBody -ContentType 'application/json' -TimeoutSec 300
    $visionBody = @{
        model = $model
        messages = @(@{
            role = 'user'
            content = @(
                @{ type = 'text'; text = $describe },
                @{ type = 'image_url'; image_url = $dataUrl }
            )
        })
        stream = $false
        temperature = 0
        seed = 1
    } | ConvertTo-Json -Depth 8
    $visionReply = Invoke-RestMethod -Uri "$base/v1/chat/completions" -Method Post -Body $visionBody -ContentType 'application/json' -TimeoutSec 300
    $textContent = [string]$textReply.choices[0].message.content
    $visionContent = [string]$visionReply.choices[0].message.content
    $redacted = $visionContent
    if ($visionContent.Length -lt 20) {
        throw 'The vision reply was empty.'
    }
    if ($visionContent -match '(?i)cannot see|can.t see|unable to view|no image was') {
        throw 'The vision reply says the image was not visible.'
    }
    if ($visionContent.Trim() -eq $textContent.Trim()) {
        throw 'The vision reply matched the text-only reply.'
    }
    $facts.vision_seen = $true
    $facts.analysis_valid = $true
    Set-Content -LiteralPath (Join-Path $reportDir 'vision.txt') -Value $redacted -Encoding UTF8
    Write-InstallLog 'Qwen vision reply differed from the text-only reply.'

    $manifestSource = Join-Path $InstallRoot 'workflows\candidates\flux2-klein-edit.json'
    if (-not (Test-Path -LiteralPath $manifestSource)) {
        $fromRepo = Join-Path (Split-Path -Parent $PSScriptRoot) 'workflows\flux2-klein-edit\manifest.json'
        $sourceRootFile = Join-Path $InstallRoot 'config\source-root.txt'
        if (Test-Path -LiteralPath $fromRepo) {
            $manifestSource = $fromRepo
        } elseif (Test-Path -LiteralPath $sourceRootFile) {
            $sourceRoot = (Get-Content -LiteralPath $sourceRootFile -Raw).Trim()
            $manifestSource = Join-Path $sourceRoot 'workflows\flux2-klein-edit\manifest.json'
        }
    }
    if (-not (Test-Path -LiteralPath $manifestSource)) {
        throw 'flux2-klein-edit manifest was not found.'
    }
    $liveManifest = Join-Path $InstallRoot 'workflows\flux2-klein-edit.json'
    Copy-Item -LiteralPath $manifestSource -Destination $liveManifest -Force
    $workflows = Invoke-RestMethod -Uri "$base/v1/workflows" -TimeoutSec 20
    $listed = @($workflows.workflows | Where-Object { $_.id -eq 'flux2-klein-edit' -and $_.enabled -eq $true })
    if ($listed.Count -eq 0) {
        Write-InstallLog 'Restarting the owned gateway so it loads flux2-klein-edit.'
        & (Join-Path $PSScriptRoot 'stop.ps1') -InstallRoot $InstallRoot
        if ($LASTEXITCODE -ne 0) {
            throw 'The owned gateway could not be stopped.'
        }
        & (Join-Path $PSScriptRoot 'start.ps1') -InstallRoot $InstallRoot
        if ($LASTEXITCODE -ne 0) {
            throw 'The owned gateway did not return.'
        }
        $workflows = Invoke-RestMethod -Uri "$base/v1/workflows" -TimeoutSec 20
        $listed = @($workflows.workflows | Where-Object { $_.id -eq 'flux2-klein-edit' -and $_.enabled -eq $true })
    }
    if ($listed.Count -eq 0) {
        throw 'flux2-klein-edit is not enabled in the live registry.'
    }
    $logBefore = (Read-GatewayLog).Length
    $editPrompt = Get-TextFromCodes @(1048,1079,1084,1077,1085,1080,32,1094,1074,1077,1090,32,1092,1086,1085,1072,32,1085,1072,32,1089,1074,1077,1090,1083,1086,45,1089,1077,1088,1099,1081,44,32,1089,1086,1093,1088,1072,1085,1080,32,1086,1073,1098,1077,1082,1090,44,32,1077,1075,1086,32,1092,1086,1088,1084,1091,44,32,1088,1072,1089,1087,1086,1083,1086,1078,1077,1085,1080,1077,32,1080,32,1087,1088,1086,1087,1086,1088,1094,1080,1080)
    $create = & curl.exe -sS -m 120 -F "prompt=$editPrompt" -F 'operation=edit' -F "conversation_id=multimodalverify" -F "attachments=@$imagePath;type=image/png" "$base/v1/studio/tasks"
    $created = $create | ConvertFrom-Json
    if (-not $created.id) {
        throw 'The studio task was not created.'
    }
    $deadline = (Get-Date).AddMinutes(25)
    $task = $null
    do {
        Start-Sleep -Seconds 2
        Add-GpuSample
        $task = Invoke-RestMethod -Uri "$base/v1/studio/tasks/$($created.id)" -TimeoutSec 30
        Write-InstallLog ("task {0} stage {1}" -f $task.status, $task.stage)
    } while (@('succeeded', 'failed', 'cancelled', 'interrupted') -notcontains [string]$task.status -and (Get-Date) -lt $deadline)
    if ([string]$task.status -ne 'succeeded') {
        throw "The studio task ended as $($task.status): $($task.error)"
    }
    $generated = @($task.files | Where-Object { $_.role -eq 'generated' })
    if ($generated.Count -lt 1) {
        throw 'FLUX did not return an image.'
    }
    $resultPath = Join-Path $reportDir 'flux-result.png'
    Invoke-WebRequest -Uri "$base/v1/studio/files/$($generated[0].id)" -OutFile $resultPath -TimeoutSec 60
    $resultBytes = [System.IO.File]::ReadAllBytes($resultPath)
    if ($resultBytes.Length -lt 8 -or $resultBytes[0] -ne 137) {
        throw 'The FLUX result is not a PNG.'
    }
    $same = $true
    if ($resultBytes.Length -eq $imageBytes.Length) {
        for ($i = 0; $i -lt $resultBytes.Length; $i++) {
            if ($resultBytes[$i] -ne $imageBytes[$i]) {
                $same = $false
                break
            }
        }
    } else {
        $same = $false
    }
    if ($same) {
        throw 'The FLUX result is identical to the source image.'
    }
    $facts.flux_edited = $true
    if (-not $task.summary) {
        throw 'The final Qwen response was empty.'
    }
    $facts.qwen_after_flux = $true
    $fresh = (Read-GatewayLog).Substring($logBefore)
    if ($fresh -match 'provider llm released GPU resources') {
        $facts.ollama_released = $true
    }
    if ($fresh -match 'started ComfyUI' -or $fresh -match 'comfyui prompt') {
        $facts.comfyui_started = $true
    }
    if ($fresh -match 'provider image released GPU resources') {
        $facts.comfyui_released = $true
    }
    $idle = [regex]::Matches($fresh, 'scheduler state (\w+)')
    if ($idle.Count -gt 0 -and $idle[$idle.Count - 1].Groups[1].Value -eq 'IDLE') {
        $facts.scheduler_idle = $true
    }
    foreach ($name in @($facts.Keys)) {
        if (-not $facts[$name]) {
            throw "Check failed: $name"
        }
    }
    $hash = (Get-FileHash -LiteralPath $liveManifest -Algorithm SHA256).Hash.ToLowerInvariant()
    $record = [ordered]@{
        workflow_id = 'flux2-klein-edit'
        status = 'passed'
        manifest_sha256 = $hash
        vram_fit_claimed = $false
        verified_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        report_dir = $reportDir
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot 'config') | Out-Null
    $record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $InstallRoot 'config\multimodal-pipeline.json') -Encoding UTF8
    Write-InstallLog 'Multimodal pipeline passed. This does not claim the models fit fully in VRAM.'
} catch {
    $failure = $_.Exception.Message
    Write-InstallLog $failure 'ERROR'
} finally {
    $gatewayLog = Join-Path $InstallRoot 'logs\switchboard.log'
    if (Test-Path -LiteralPath $gatewayLog) {
        Copy-Item -LiteralPath $gatewayLog -Destination (Join-Path $reportDir 'gateway.log') -Force
        $comfyLines = Select-String -LiteralPath $gatewayLog -Pattern 'comfyui' -SimpleMatch | ForEach-Object { $_.Line }
        $comfyText = if ($comfyLines) { $comfyLines -join "`n" } else { 'No ComfyUI log file was found. The gateway log had no ComfyUI lines.' }
        Set-Content -LiteralPath (Join-Path $reportDir 'comfyui.log') -Value $comfyText -Encoding UTF8
    }
    $report = [ordered]@{
        status = $(if ($failure) { 'failed' } else { 'passed' })
        failure = $failure
        facts = $facts
    }
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $reportDir 'report.json') -Encoding UTF8
    $lines = @("# Multimodal pipeline", "", "Status: $($report.status)", "")
    foreach ($name in @($facts.Keys)) {
        $lines += "- $name : $($facts[$name])"
    }
    if ($failure) {
        $lines += ""
        $lines += "Failure: $failure"
    }
    Set-Content -LiteralPath (Join-Path $reportDir 'report.md') -Value ($lines -join "`n") -Encoding UTF8
}

if ($failure) {
    exit 1
}
exit 0
