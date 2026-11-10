#Requires -Version 5.1
<#
.SYNOPSIS
    Validate and run the FLUX.2 Klein image pipeline through the production Gateway.

.DESCRIPTION
    Checks the local GPU and pinned model files, stages the flux2-klein-t2i
    manifest, restarts the existing Gateway, and submits one image job and one
    chat through that Gateway. It does not open ComfyUI, export a graph, start
    a second scheduler, or download model files. The manifest stays out of the
    live registry unless every stage passes.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [int]$Width = 768,
    [int]$Height = 768,
    [int]$Seed = 42,
    [int]$SampleIntervalSeconds = 1,
    [int]$LowVramReserveMb = -1
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    Write-Error 'verify-image-pipeline.ps1 runs on Windows.'
    exit 1
}

. (Join-Path $PSScriptRoot 'Common.ps1')

$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
Initialize-InstallLog -Directory (Join-Path $InstallRoot 'logs')

$script:ReportDir = Join-Path $InstallRoot 'data\diagnostics\image-pipeline\latest'
$script:BackupDir = Join-Path $InstallRoot 'data\diagnostics\image-pipeline\workflow-backup'
$script:CsvPath = Join-Path $script:ReportDir 'gpu-metrics.csv'
$script:Python = Join-Path $InstallRoot 'venv\Scripts\python.exe'
$script:NvidiaSmi = ''
$script:ApiPort = 8787
$script:Pins = $null
$script:Portable = ''
$script:Prompt = ''
$script:Staged = $false
$script:GatewayStopped = $false
$script:FailureStage = ''
$script:FailureDetail = ''
$script:ErrorLog = ''
$script:Overlap = $false
$script:LaunchProfile = 'default'
$script:GenerationSeconds = $null
$script:Succeeded = $false
$script:Report = @{
    models_verified = $false
    workflow_validated = $false
    image_generated = $false
    dimensions_verified = $false
    peak_vram_mib = $null
    gpu_temperature_c = $null
    initial_vram_mib = $null
    final_vram_mib = $null
    generation_seconds = $null
    transition = 'not run'
    ollama_response = ''
    workflow_enabled = $false
    failure_stage = ''
    failure_detail = ''
    launch_profile = 'default'
    vram_fit_claimed = $false
    hardware_gpu = ''
    hardware_vram_mib = $null
    hardware_memory_used_mib = $null
    hardware_ram_bytes = $null
    hardware_disk_free_bytes = $null
    comfyui_pin = ''
}

function Add-ErrorLog {
    param([string]$Message)
    if ([string]::IsNullOrWhiteSpace($Message)) {
        return
    }
    if ($script:ErrorLog) {
        $script:ErrorLog += "`n"
    }
    $script:ErrorLog += $Message
}

function Fail-PipelineStage {
    param(
        [Parameter(Mandatory = $true)][string]$Stage,
        [Parameter(Mandatory = $true)][string]$Detail
    )
    $script:FailureStage = $Stage
    $script:FailureDetail = $Detail
    Add-ErrorLog "$Stage: $Detail"
    Write-InstallLog "Stage $Stage failed. $Detail" 'ERROR'
    throw $Detail
}

function Invoke-PipelinePython {
    param([Parameter(Mandatory = $true)][string]$Code)
    $output = & $script:Python -c $Code 2>&1
    $text = ((@($output) | ForEach-Object { [string]$_ }) -join "`n").Trim()
    return @{ ExitCode = [int]$LASTEXITCODE; Text = $text }
}

function Add-GpuSample {
    if (-not $script:NvidiaSmi) {
        return
    }
    $output = & $script:NvidiaSmi --query-gpu=timestamp,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw --format=csv,noheader,nounits
    if ($LASTEXITCODE -ne 0) {
        return
    }
    $line = @($output)[0]
    if ($line) {
        Add-Content -LiteralPath $script:CsvPath -Value ([string]$line).Trim() -Encoding UTF8
    }
}

function Test-InferenceOverlap {
    $running = @()
    $models = @()
    try {
        $queue = Invoke-RestMethod -Uri 'http://127.0.0.1:8188/queue' -TimeoutSec 5
        $running = @($queue.queue_running)
    } catch {
        return
    }
    try {
        $resident = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/ps' -TimeoutSec 5
        $models = @($resident.models)
    } catch {
        return
    }
    if ($running.Count -gt 0 -and $models.Count -gt 0) {
        $script:Overlap = $true
        Add-ErrorLog 'ComfyUI queue_running and an Ollama model were present in the same sample.'
    }
}

function Get-RepositoryRoot {
    $parent = Split-Path -Parent $PSScriptRoot
    if (Test-Path -LiteralPath (Join-Path $parent 'pyproject.toml')) {
        return $parent
    }
    $marker = Join-Path $InstallRoot 'config\source-root.txt'
    if (Test-Path -LiteralPath $marker) {
        $recorded = (Get-Content -LiteralPath $marker -Raw).Trim()
        if (Test-Path -LiteralPath (Join-Path $recorded 'pyproject.toml')) {
            return $recorded
        }
    }
    return $null
}

function Get-CandidateManifest {
    $root = Get-RepositoryRoot
    if ($root) {
        $path = Join-Path $root 'workflows\flux2-klein-t2i\manifest.json'
        if (Test-Path -LiteralPath $path) {
            return $path
        }
    }
    $staged = Join-Path $InstallRoot 'workflows\candidates\flux2-klein-t2i.json'
    if (Test-Path -LiteralPath $staged) {
        return $staged
    }
    return $null
}

function Stop-ProductionGateway {
    param([switch]$IncludeComfyUI)
    $stop = Join-Path $PSScriptRoot 'stop.ps1'
    if ($IncludeComfyUI) {
        & $stop -InstallRoot $InstallRoot -IncludeComfyUI
    } else {
        & $stop -InstallRoot $InstallRoot
    }
    $script:GatewayStopped = $true
    return ($LASTEXITCODE -eq 0)
}

function Start-ProductionGateway {
    param([int]$Reserve = -1)
    $start = Join-Path $PSScriptRoot 'start.ps1'
    if ($Reserve -ge 0) {
        & $start -InstallRoot $InstallRoot -ReserveVramMb $Reserve
    } else {
        & $start -InstallRoot $InstallRoot
    }
    if ($LASTEXITCODE -eq 0) {
        $script:GatewayStopped = $false
    }
    return ($LASTEXITCODE -eq 0)
}

function Invoke-ObjectInfoCheck {
    param([Parameter(Mandatory = $true)][string]$ManifestPath)
    $infoDir = Join-Path $script:ReportDir 'object-info'
    if (Test-Path -LiteralPath $infoDir) {
        Remove-Item -LiteralPath $infoDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $infoDir | Out-Null
    $env:LAS_MANIFEST = $ManifestPath
    $listed = Invoke-PipelinePython -Code 'import os; from pathlib import Path; from local_ai_switchboard.diagnostics.flux2_klein import class_types_in_manifest; print("\n".join(class_types_in_manifest(Path(os.environ["LAS_MANIFEST"]))))'
    if ($listed.ExitCode -ne 0) {
        Fail-PipelineStage -Stage 'C' -Detail $listed.Text
    }
    foreach ($className in @($listed.Text -split '\r?\n' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
        $target = Join-Path $infoDir ($className + '.json')
        try {
            Invoke-WebRequest -Uri "http://127.0.0.1:8188/object_info/$className" -OutFile $target -UseBasicParsing -TimeoutSec 30 | Out-Null
        } catch {
            Set-Content -LiteralPath $target -Value '{}' -Encoding ASCII
        }
    }
    $env:LAS_OBJECT_INFO_DIR = $infoDir
    $checked = Invoke-PipelinePython -Code 'import os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import check_object_info_directory; problems = check_object_info_directory(Path(os.environ["LAS_MANIFEST"]), Path(os.environ["LAS_OBJECT_INFO_DIR"])); print("\n".join(problems)); raise SystemExit(1 if problems else 0)'
    if ($checked.ExitCode -ne 0) {
        $version = [string]$script:Pins.comfyui.version
        Fail-PipelineStage -Stage 'C' -Detail ($checked.Text + " Pinned ComfyUI version is $version. The installation was not updated.")
    }
}

function Invoke-ImageAttempt {
    $bodyObject = @{
        workflow_id = 'flux2-klein-t2i'
        prompt = $script:Prompt
        width = $Width
        height = $Height
        seed = $Seed
        steps = 4
    }
    $body = $bodyObject | ConvertTo-Json -Compress
    $startedAt = Get-Date
    try {
        $created = Invoke-WebRequest -Uri "http://127.0.0.1:$($script:ApiPort)/v1/assets" -Method POST -Body $body -ContentType 'application/json; charset=utf-8' -UseBasicParsing -TimeoutSec 60
    } catch {
        return @{ status = 'failed'; error = [string]$_.Exception.Message; id = '' }
    }
    $createdJob = $created.Content | ConvertFrom-Json
    $jobId = [string]$createdJob.id
    if (-not $jobId) {
        return @{ status = 'failed'; error = 'The gateway did not return a job id.'; id = '' }
    }
    $deadline = (Get-Date).AddMinutes(8)
    $last = $null
    do {
        Add-GpuSample
        Test-InferenceOverlap
        try {
            $last = Invoke-RestMethod -Uri "http://127.0.0.1:$($script:ApiPort)/v1/jobs/$jobId" -TimeoutSec 30
        } catch {
            $script:GenerationSeconds = [int]((Get-Date) - $startedAt).TotalSeconds
            return @{ status = 'failed'; error = [string]$_.Exception.Message; id = $jobId }
        }
        $status = [string]$last.status
        if ($status -eq 'succeeded' -or $status -eq 'failed' -or $status -eq 'cancelled') {
            break
        }
        Start-Sleep -Seconds $SampleIntervalSeconds
    } while ((Get-Date) -lt $deadline)
    $script:GenerationSeconds = [int]((Get-Date) - $startedAt).TotalSeconds
    if ($null -eq $last) {
        return @{ status = 'failed'; error = 'The image job was not readable.'; id = $jobId }
    }
    $finalStatus = [string]$last.status
    if ($finalStatus -ne 'succeeded' -and $finalStatus -ne 'failed' -and $finalStatus -ne 'cancelled') {
        $finalStatus = 'failed'
        $lastError = 'The image job did not finish before the verifier timeout.'
    } else {
        $lastError = [string]$last.error
    }
    return @{ status = $finalStatus; error = $lastError; id = $jobId }
}

function Get-RetryReserve {
    param([string]$ErrorText)
    if ($LowVramReserveMb -lt 0) {
        $env:LAS_RESERVE = ''
    } else {
        $env:LAS_RESERVE = [string]$LowVramReserveMb
    }
    $env:LAS_JOB_ERROR = $ErrorText
    $decision = Invoke-PipelinePython -Code 'import os; from local_ai_switchboard.diagnostics.image_pipeline import retry_reserve_vram_mb; raw = os.environ.get("LAS_RESERVE", ""); reserve = None if raw == "" else int(raw); value = retry_reserve_vram_mb(failures=1, error=os.environ.get("LAS_JOB_ERROR", ""), reserve_vram_mb=reserve); print("" if value is None else value)'
    if ($decision.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($decision.Text)) {
        return $null
    }
    return [int]$decision.Text
}

function Invoke-SwitchbackChat {
    Add-Type -AssemblyName System.Net.Http
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromMinutes(5)
    try {
        $chat = @{
            model = 'qwen3.5:4b-ctx8192'
            stream = $false
            messages = @(@{ role = 'user'; content = 'Reply with the single word ready.' })
        } | ConvertTo-Json -Compress -Depth 4
        $content = New-Object System.Net.Http.StringContent($chat, [Text.Encoding]::UTF8, 'application/json')
        $task = $client.PostAsync("http://127.0.0.1:$($script:ApiPort)/v1/chat/completions", $content)
        while (-not $task.IsCompleted) {
            Add-GpuSample
            Test-InferenceOverlap
            Start-Sleep -Seconds $SampleIntervalSeconds
        }
        $response = $task.Result
        $raw = $response.Content.ReadAsStringAsync().Result
        if (-not $response.IsSuccessStatusCode) {
            return @{ Ok = $false; Text = [string]$raw }
        }
        $parsed = $raw | ConvertFrom-Json
        $choice = $parsed.choices
        if ($choice -is [System.Array]) {
            $choice = $choice[0]
        }
        $reply = [string]$choice.message.content
        return @{ Ok = $true; Text = $reply.Trim() }
    } finally {
        $client.Dispose()
    }
}

function Restore-LiveWorkflows {
    $env:LAS_PIPELINE_SOURCE = Join-Path $InstallRoot 'workflows'
    $env:LAS_PIPELINE_BACKUP = $script:BackupDir
    $restored = Invoke-PipelinePython -Code 'import os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import restore_workflow_snapshot; restore_workflow_snapshot(Path(os.environ["LAS_PIPELINE_SOURCE"]), Path(os.environ["LAS_PIPELINE_BACKUP"]))'
    if ($restored.ExitCode -ne 0) {
        Add-ErrorLog $restored.Text
        return $false
    }
    return $true
}

function Write-PipelineReport {
    try {
        if (Test-Path -LiteralPath $script:CsvPath) {
            $env:LAS_GPU_CSV = $script:CsvPath
            $summary = Invoke-PipelinePython -Code 'import json, os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import summarize_gpu_csv; print(json.dumps(summarize_gpu_csv(Path(os.environ["LAS_GPU_CSV"]).read_text(encoding="utf-8-sig"))))'
            if ($summary.ExitCode -eq 0 -and $summary.Text) {
                $parsed = $summary.Text | ConvertFrom-Json
                $script:Report.peak_vram_mib = $parsed.peak_vram_mib
                $script:Report.gpu_temperature_c = $parsed.peak_temperature_c
                $script:Report.initial_vram_mib = $parsed.initial_vram_mib
                $script:Report.final_vram_mib = $parsed.final_vram_mib
            }
        }
        $script:Report.generation_seconds = $script:GenerationSeconds
        $script:Report.failure_stage = $script:FailureStage
        $script:Report.failure_detail = $script:FailureDetail
        $script:Report.launch_profile = $script:LaunchProfile
        $script:Report.vram_fit_claimed = $false
        $inputPath = Join-Path $script:ReportDir 'report-input.json'
        $script:Report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $inputPath -Encoding UTF8
        $env:LAS_REPORT_INPUT = $inputPath
        $env:LAS_REPORT_DIR = $script:ReportDir
        $env:LAS_ERROR_LOG = $script:ErrorLog
        $env:LAS_GPU_CSV = $script:CsvPath
        [void](Invoke-PipelinePython -Code @'
import json, os
from pathlib import Path
from local_ai_switchboard.diagnostics.image_pipeline import write_pipeline_report
report = json.loads(Path(os.environ["LAS_REPORT_INPUT"]).read_text(encoding="utf-8-sig"))
csv_path = Path(os.environ["LAS_GPU_CSV"])
csv_text = csv_path.read_text(encoding="utf-8-sig") if csv_path.is_file() else ""
write_pipeline_report(Path(os.environ["LAS_REPORT_DIR"]), report, csv_text=csv_text, error_log=os.environ.get("LAS_ERROR_LOG", ""))
'@)
    } catch {
        Write-InstallLog "The report could not be written. $($_.Exception.Message)" 'ERROR'
    }
}

New-Item -ItemType Directory -Force -Path $script:ReportDir | Out-Null
Set-Content -LiteralPath $script:CsvPath -Value 'timestamp,memory_used_mib,memory_total_mib,utilization_gpu,temperature_c,power_w' -Encoding ASCII

try {
    if ($Width -lt 64 -or $Width -gt 2048 -or $Height -lt 64 -or $Height -gt 2048) {
        Fail-PipelineStage -Stage 'parameters' -Detail 'Width and height must be between 64 and 2048.'
    }
    if ($Seed -lt 0) {
        Fail-PipelineStage -Stage 'parameters' -Detail 'Seed must be zero or positive.'
    }
    if ($SampleIntervalSeconds -lt 1) {
        Fail-PipelineStage -Stage 'parameters' -Detail 'SampleIntervalSeconds must be at least 1.'
    }

    Write-InstallLog 'Stage A: hardware.'
    $nvidia = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($null -eq $nvidia) {
        $nvidia = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    }
    if ($null -ne $nvidia) {
        $script:NvidiaSmi = [string]$nvidia.Source
    }
    $gpu = Get-NvidiaReport
    if (-not $gpu.Available) {
        Fail-PipelineStage -Stage 'A' -Detail $gpu.Detail
    }
    $used = & $script:NvidiaSmi --query-gpu=memory.used --format=csv,noheader,nounits
    $usedValue = $null
    if ($LASTEXITCODE -eq 0) {
        $usedText = @($used)[0].ToString().Trim()
        $parsedUsed = 0
        if ([int]::TryParse($usedText, [ref]$parsedUsed)) {
            $usedValue = $parsedUsed
        }
    }
    Add-GpuSample
    $osInfo = Get-CimInstance -ClassName Win32_OperatingSystem
    $ramBytes = [int64]$osInfo.TotalVisibleMemorySize * 1024
    $freeBytes = Get-FreeBytesForPath -Path $InstallRoot
    $script:Report.hardware_gpu = [string]$gpu.Name
    $script:Report.hardware_vram_mib = [int]$gpu.MemoryMiB
    $script:Report.hardware_memory_used_mib = $usedValue
    $script:Report.hardware_ram_bytes = $ramBytes
    $script:Report.hardware_disk_free_bytes = $freeBytes
    Write-InstallLog "GPU $($gpu.Name); VRAM $($gpu.MemoryMiB) MiB; used $usedValue MiB; RAM $ramBytes bytes; free disk $freeBytes bytes. This does not claim the model fits fully in VRAM."

    Write-InstallLog 'Stage B: models.'
    $script:Pins = Get-InstallerPins
    $script:Report.comfyui_pin = [string]$script:Pins.comfyui.version
    $script:Portable = Find-ComfyPortableRoot -Path (Join-Path $InstallRoot 'ComfyUI_windows_portable')
    if (-not $script:Portable) {
        Fail-PipelineStage -Stage 'B' -Detail 'The ComfyUI portable tree was not found.'
    }
    foreach ($model in @($script:Pins.comfy_models)) {
        $modelPath = Join-Path $script:Portable "ComfyUI\models\$($model.folder)\$($model.filename)"
        if (-not (Test-Path -LiteralPath $modelPath)) {
            Fail-PipelineStage -Stage 'B' -Detail "Missing model file $($model.filename)."
        }
        $matched = Test-PinnedFileHash -Path $modelPath -Algorithm ([string]$model.algorithm) -Checksum ([string]$model.checksum) -ExpectedBytes ([int64]$model.expected_bytes)
        if (-not $matched) {
            Fail-PipelineStage -Stage 'B' -Detail "The checksum or size for $($model.filename) does not match the pin."
        }
        Write-InstallLog "Verified $($model.filename)."
    }
    $ollama = Find-OllamaExecutable
    if ($null -eq $ollama) {
        Fail-PipelineStage -Stage 'B' -Detail 'ollama was not found. The text model is checked separately from the image files.'
    }
    $show = Invoke-CapturedCommand -FilePath $ollama.Executable -ArgumentList @('show', 'qwen3.5:4b-ctx8192')
    if ($show.ExitCode -ne 0 -or $show.Text -notmatch 'num_ctx\s+8192') {
        Fail-PipelineStage -Stage 'B' -Detail 'ollama show qwen3.5:4b-ctx8192 did not report num_ctx 8192.'
    }
    $script:Report.models_verified = $true
    Write-InstallLog 'Model files matched their pins. No model file was downloaded.'

    if (-not (Test-Path -LiteralPath $script:Python)) {
        Fail-PipelineStage -Stage 'gateway' -Detail "The gateway interpreter is missing at $($script:Python)."
    }
    $repository = Get-RepositoryRoot
    if (-not $repository) {
        Fail-PipelineStage -Stage 'gateway' -Detail 'The source tree with pyproject.toml was not found.'
    }
    $candidate = Get-CandidateManifest
    if (-not $candidate) {
        Fail-PipelineStage -Stage 'D' -Detail 'The flux2-klein-t2i manifest was not found.'
    }
    Write-InstallLog 'Updating the gateway package from the local source tree. Model files are not downloaded.'
    if (-not (Stop-ProductionGateway)) {
        Fail-PipelineStage -Stage 'gateway' -Detail 'The production gateway could not be stopped before the package update.'
    }
    $script:ApiPort = Get-ConfiguredApiPort -InstallRoot $InstallRoot -DefaultPort 8787
    $stillLive = Test-HttpStatus -Url "http://127.0.0.1:$($script:ApiPort)/health/live" -TimeoutSeconds 3
    if ($stillLive -eq 200) {
        Fail-PipelineStage -Stage 'gateway' -Detail 'The gateway is still answering /health/live. A second gateway was not started.'
    }
    $pipExit = Invoke-PipInstall -Python $script:Python -SourceRoot $repository
    if ($pipExit -ne 0) {
        Fail-PipelineStage -Stage 'gateway' -Detail 'pip could not install the local gateway package.'
    }
    $promptResult = Invoke-PipelinePython -Code 'from local_ai_switchboard.diagnostics.flux2_klein import DIAGNOSTIC_PROMPT; print(DIAGNOSTIC_PROMPT)'
    if ($promptResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($promptResult.Text)) {
        Fail-PipelineStage -Stage 'D' -Detail 'The diagnostic prompt could not be read from the gateway package.'
    }
    $script:Prompt = $promptResult.Text

    Write-InstallLog 'Stage D: manifest validation.'
    $validated = Get-WorkflowManifestState -Python $script:Python -ManifestPath $candidate
    if (-not $validated.Ok -or $validated.Id -ne 'flux2-klein-t2i' -or $validated.State -ne 'enabled' -or $validated.Graph -ne 'yes') {
        Fail-PipelineStage -Stage 'D' -Detail "validate_manifest rejected the candidate. $($validated.Detail)"
    }
    $disabledSlot = Join-Path $InstallRoot 'workflows\flux2-klein-4b.json'
    if (Test-Path -LiteralPath $disabledSlot) {
        $slot = Get-WorkflowManifestState -Python $script:Python -ManifestPath $disabledSlot
        if ($slot.Ok -and $slot.State -eq 'enabled') {
            Fail-PipelineStage -Stage 'D' -Detail 'flux2-klein-4b is enabled. This verifier does not modify that disabled slot.'
        }
    }
    $script:Report.workflow_validated = $true

    $workflowsDir = Join-Path $InstallRoot 'workflows'
    New-Item -ItemType Directory -Force -Path $script:BackupDir | Out-Null
    $env:LAS_PIPELINE_SOURCE = $workflowsDir
    $env:LAS_PIPELINE_BACKUP = $script:BackupDir
    $snapshot = Invoke-PipelinePython -Code 'import os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import save_workflow_snapshot; save_workflow_snapshot(Path(os.environ["LAS_PIPELINE_SOURCE"]), Path(os.environ["LAS_PIPELINE_BACKUP"]))'
    if ($snapshot.ExitCode -ne 0) {
        Fail-PipelineStage -Stage 'D' -Detail $snapshot.Text
    }
    $env:LAS_PIPELINE_MANIFEST = $candidate
    $staged = Invoke-PipelinePython -Code 'import os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import stage_live_manifest; stage_live_manifest(Path(os.environ["LAS_PIPELINE_SOURCE"]), Path(os.environ["LAS_PIPELINE_MANIFEST"]))'
    if ($staged.ExitCode -ne 0) {
        Fail-PipelineStage -Stage 'D' -Detail $staged.Text
    }
    $script:Staged = $true
    if (-not (Start-ProductionGateway)) {
        Fail-PipelineStage -Stage 'gateway' -Detail 'The production gateway did not become ready after staging the candidate.'
    }

    Write-InstallLog 'Stage C: ComfyUI node and model support.'
    $listeners = @(Get-ListeningEntries -Port 8188)
    if ($listeners.Count -gt 0) {
        foreach ($entry in $listeners) {
            if (-not (Test-LoopbackAddress -Address ([string]$entry.LocalAddress))) {
                Fail-PipelineStage -Stage 'C' -Detail 'Port 8188 is listening outside 127.0.0.1.'
            }
        }
        $portableIds = @(Get-ComfyProcessIds -PortableRoot $script:Portable)
        if ($portableIds.Count -eq 0) {
            Fail-PipelineStage -Stage 'C' -Detail 'Port 8188 is in use by a process that is not the portable ComfyUI. A second ComfyUI process was not started.'
        }
        Invoke-ObjectInfoCheck -ManifestPath $candidate
        Write-InstallLog 'ComfyUI object info matches the adapted graph.'
    } else {
        Write-InstallLog 'ComfyUI is not listening. The gateway provider will start the portable process and check object info before submitting the prompt.'
    }

    Write-InstallLog 'Stage E: image generation through the gateway scheduler.'
    $attempt = Invoke-ImageAttempt
    if ($attempt.status -ne 'succeeded') {
        Add-ErrorLog $attempt.error
        if ($attempt.error -like '*cannot run this workflow*') {
            Fail-PipelineStage -Stage 'C' -Detail $attempt.error
        }
        $retryMb = Get-RetryReserve -ErrorText $attempt.error
        if ($null -eq $retryMb) {
            Fail-PipelineStage -Stage 'E' -Detail $(if ($attempt.error) { $attempt.error } else { 'The image job failed.' })
        }
        Write-InstallLog "CUDA out of memory. Stopping the managed ComfyUI process and retrying once with reserve VRAM $retryMb MiB."
        if (-not (Stop-ProductionGateway -IncludeComfyUI)) {
            Fail-PipelineStage -Stage 'E' -Detail 'The managed ComfyUI process could not be stopped after CUDA out of memory.'
        }
        $oomLive = Test-HttpStatus -Url "http://127.0.0.1:$($script:ApiPort)/health/live" -TimeoutSeconds 3
        if ($oomLive -eq 200) {
            Fail-PipelineStage -Stage 'E' -Detail 'The gateway was still running after the out-of-memory stop.'
        }
        if (-not (Start-ProductionGateway -Reserve $retryMb)) {
            Fail-PipelineStage -Stage 'E' -Detail 'The gateway did not restart with the reserve-vram profile.'
        }
        $script:LaunchProfile = "reserve-vram:$retryMb"
        $attempt = Invoke-ImageAttempt
        if ($attempt.status -ne 'succeeded') {
            Add-ErrorLog $attempt.error
            Fail-PipelineStage -Stage 'E' -Detail $(if ($attempt.error) { $attempt.error } else { 'The reserve-vram retry failed.' })
        }
    }
    $imagePath = Join-Path $script:ReportDir 'image.png'
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$($script:ApiPort)/v1/jobs/$($attempt.id)/download" -OutFile $imagePath -UseBasicParsing -TimeoutSec 60 | Out-Null
    } catch {
        Fail-PipelineStage -Stage 'E' -Detail "The gateway did not return the image. $($_.Exception.Message)"
    }
    $script:Report.image_generated = $true
    $env:LAS_IMAGE = $imagePath
    $dimensions = Invoke-PipelinePython -Code 'import os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import png_dimensions; width, height = png_dimensions(Path(os.environ["LAS_IMAGE"]).read_bytes()); print(str(width) + " " + str(height))'
    if ($dimensions.ExitCode -ne 0) {
        Fail-PipelineStage -Stage 'E' -Detail $dimensions.Text
    }
    $parts = @($dimensions.Text.Split(' '))
    if ($parts.Count -ne 2 -or [int]$parts[0] -ne $Width -or [int]$parts[1] -ne $Height) {
        Fail-PipelineStage -Stage 'E' -Detail "The PNG is $($dimensions.Text), expected $Width x $Height."
    }
    $script:Report.dimensions_verified = $true
    if ($listeners.Count -eq 0) {
        Invoke-ObjectInfoCheck -ManifestPath $candidate
    }

    Write-InstallLog 'Stage G: switch back to Ollama through the gateway.'
    $idle = $false
    $idleDeadline = (Get-Date).AddSeconds(60)
    do {
        Add-GpuSample
        try {
            $queue = Invoke-RestMethod -Uri 'http://127.0.0.1:8188/queue' -TimeoutSec 5
            $running = @($queue.queue_running)
            $pending = @($queue.queue_pending)
            if ($running.Count -eq 0 -and $pending.Count -eq 0) {
                $idle = $true
                break
            }
        } catch {
            $idle = $false
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $idleDeadline)
    if (-not $idle) {
        Fail-PipelineStage -Stage 'G' -Detail 'ComfyUI was not idle after the image job.'
    }
    $chat = Invoke-SwitchbackChat
    Add-GpuSample
    if (-not $chat.Ok -or [string]::IsNullOrWhiteSpace($chat.Text)) {
        Fail-PipelineStage -Stage 'G' -Detail $(if ($chat.Text) { [string]$chat.Text } else { 'The Ollama chat through the gateway was empty.' })
    }
    if ($script:Overlap) {
        Fail-PipelineStage -Stage 'G' -Detail 'ComfyUI and Ollama were both active in one sample.'
    }
    $resident = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/ps' -TimeoutSec 15
    $modelReady = $false
    foreach ($model in @($resident.models)) {
        if ([string]$model.name -like 'qwen3.5:4b-ctx8192*') {
            $modelReady = $true
        }
    }
    if (-not $modelReady) {
        Fail-PipelineStage -Stage 'G' -Detail 'Ollama /api/ps does not show qwen3.5:4b-ctx8192 after the chat.'
    }
    $reply = [string]$chat.Text
    if ($reply.Length -gt 500) {
        $reply = $reply.Substring(0, 500)
    }
    $script:Report.ollama_response = $reply
    $script:Report.transition = 'passed'

    Write-InstallLog 'Stage H: record the verified workflow.'
    $liveManifest = Join-Path $workflowsDir 'flux2-klein-t2i.json'
    $hash = (Get-FileHash -LiteralPath $liveManifest -Algorithm SHA256).Hash.ToLowerInvariant()
    $env:LAS_VERIFY_HASH = $hash
    $env:LAS_VERIFY_PROFILE = $script:LaunchProfile
    $env:LAS_VERIFY_REPORT = $script:ReportDir
    $env:LAS_VERIFY_OUT = Join-Path $InstallRoot 'config\image-pipeline.json'
    $record = Invoke-PipelinePython -Code 'import json, os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import commit_verification; document = commit_verification(passed=True, manifest_sha256=os.environ["LAS_VERIFY_HASH"], launch_profile=os.environ["LAS_VERIFY_PROFILE"], report_dir=os.environ["LAS_VERIFY_REPORT"]); Path(os.environ["LAS_VERIFY_OUT"]).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")'
    if ($record.ExitCode -ne 0) {
        Fail-PipelineStage -Stage 'H' -Detail $record.Text
    }
    $script:Report.workflow_enabled = $true
    $script:Succeeded = $true
    Write-InstallLog 'flux2-klein-t2i is enabled. The report does not claim that the model fits fully in VRAM.'
} catch {
    if (-not $script:FailureStage) {
        $script:FailureStage = 'unhandled'
        $script:FailureDetail = [string]$_.Exception.Message
        Add-ErrorLog $script:FailureDetail
    }
    $script:Report.workflow_enabled = $false
    if ($script:Staged) {
        Write-InstallLog 'Restoring the previous workflow registry.'
        if (-not (Restore-LiveWorkflows)) {
            Add-ErrorLog 'The previous workflow registry could not be restored.'
        }
        $script:Staged = $false
        if (-not (Stop-ProductionGateway -IncludeComfyUI)) {
            Add-ErrorLog 'The gateway could not be stopped during rollback.'
        }
        if (-not (Start-ProductionGateway)) {
            Add-ErrorLog 'The gateway did not restart after rollback.'
        }
    } elseif ($script:GatewayStopped) {
        if (-not (Start-ProductionGateway)) {
            Add-ErrorLog 'The gateway did not restart after the failed verification.'
        }
    }
} finally {
    Add-GpuSample
    Write-PipelineReport
}

if ($script:Succeeded) {
    exit 0
}
Write-InstallLog "Verification failed at stage $($script:FailureStage). The workflow was left disabled." 'ERROR'
exit 1
