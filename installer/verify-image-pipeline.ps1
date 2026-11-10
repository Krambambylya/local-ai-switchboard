#Requires -Version 5.1
<#
.SYNOPSIS
    Validate and run the FLUX.2 Klein image pipeline through the production Gateway.

.DESCRIPTION
    Checks this Windows install, then runs one gateway chat, one image job, and
    a second gateway chat. The image request uses the adapted flux2-klein-t2i
    graph. flux2-klein-4b stays disabled. The script does not download models,
    start a second scheduler, or stop Ollama. A failed run does not replace a
    previous config\image-pipeline.json.
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
Write-InstallLog 'This check uses the current user account. It does not request administrator rights.'

$script:ReportRoot = Join-Path $InstallRoot 'data\diagnostics\image-pipeline'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$suffix = 0
do {
    $reportName = $stamp
    if ($suffix -gt 0) {
        $reportName = '{0}-{1}' -f $stamp, $suffix
    }
    $script:ReportDir = Join-Path $script:ReportRoot $reportName
    $suffix += 1
} while (Test-Path -LiteralPath $script:ReportDir)
$script:BackupDir = Join-Path $InstallRoot 'data\diagnostics\image-pipeline\workflow-backup'
$script:CsvPath = Join-Path $script:ReportDir 'gpu-metrics.csv'
$script:Python = Join-Path $InstallRoot 'venv\Scripts\python.exe'
$script:NvidiaSmi = ''
$script:ApiPort = 8787
$script:Pins = $null
$script:Portable = ''
$script:Prompt = ''
$script:WorkflowsDir = Join-Path $InstallRoot 'workflows'
$script:StorageDir = Join-Path $InstallRoot 'data'
$script:LogDir = Join-Path $InstallRoot 'logs'
$script:Staged = $false
$script:GatewayStopped = $false
$script:RecordWritten = $false
$script:HadPreviousRecord = $false
$script:FailureStage = ''
$script:FailureDetail = ''
$script:BlockedReason = ''
$script:ErrorLog = ''
$script:Overlap = $false
$script:LaunchProfile = 'default'
$script:GenerationSeconds = $null
$script:GenerationStarted = ''
$script:GenerationFinished = ''
$script:SamplesBeforeImage = 0
$script:LogStart = [int64]0
$script:Succeeded = $false
$script:Result = ''
$script:GatewayLogState = 'note'
$script:ComfyLogState = 'note'
$script:ModelRecords = @()
$script:Offload = $null
$script:Stages = @{
    A      = 'not run'
    B      = 'not run'
    C      = 'not run'
    D      = 'not run'
    E      = 'not run'
    F      = 'not run'
    record = 'not run'
}
$script:Report = @{
    models_verified = $false
    workflow_validated = $false
    text_before_image = $false
    scheduler_idle_before_image = $false
    comfyui_checked = $false
    image_generated = $false
    dimensions_verified = $false
    image_scheduler_succeeded = $false
    scheduler_idle_after_image = $false
    text_after_image = $false
    scheduler_idle_after_text = $false
    resources_released = $false
    doctor_verified = $false
    peak_vram_mib = $null
    gpu_temperature_c = $null
    peak_utilization_gpu = $null
    peak_power_w = $null
    memory_total_mib = $null
    initial_vram_mib = $null
    final_vram_mib = $null
    generation_seconds = $null
    generation_started_at = ''
    generation_finished_at = ''
    gpu_started_at = $null
    gpu_finished_at = $null
    transition = 'not run'
    ollama_response = ''
    ollama_response_before = ''
    workflow_enabled = $false
    failure_stage = ''
    failure_detail = ''
    blocked_reason = ''
    launch_profile = 'default'
    vram_fit_claimed = $false
    hardware_gpu = ''
    hardware_driver = ''
    hardware_vram_mib = $null
    hardware_memory_used_mib = $null
    hardware_ram_bytes = $null
    hardware_disk_free_bytes = $null
    windows_caption = ''
    windows_build = ''
    comfyui_pin = ''
    comfyui_version_observed = ''
    mac_lan_checked = $false
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

function Set-StageResult {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Status
    )
    $script:Stages[$Name] = $Status
}

function Fail-PipelineStage {
    param(
        [Parameter(Mandatory = $true)][string]$Stage,
        [Parameter(Mandatory = $true)][string]$Detail
    )
    $script:FailureStage = $Stage
    $script:FailureDetail = $Detail
    if ($script:Stages.ContainsKey($Stage)) {
        if ($script:BlockedReason -eq 'active_jobs' -or $script:BlockedReason -eq 'scheduler_unreadable') {
            Set-StageResult -Name $Stage -Status 'stopped'
        } else {
            Set-StageResult -Name $Stage -Status 'fail'
        }
    }
    Add-ErrorLog "$Stage`: $Detail"
    Write-InstallLog "Stage $Stage failed. $Detail" 'ERROR'
    throw $Detail
}

function Invoke-QuietNative {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList
    )
    # Windows PowerShell 5.1 turns native stderr into a terminating error when
    # ErrorActionPreference is Stop. A warning must not abort the check.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $FilePath @ArgumentList 2>&1
        $code = 1
        if ($null -ne $LASTEXITCODE) {
            $code = [int]$LASTEXITCODE
        }
        $text = ((@($output) | ForEach-Object { [string]$_ }) -join "`n").Trim()
        return @{ ExitCode = $code; Text = $text }
    } catch {
        return @{ ExitCode = 1; Text = [string]$_.Exception.Message }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Invoke-PipelinePython {
    param([Parameter(Mandatory = $true)][string]$Code)
    $previous = $env:PYTHONPATH
    $source = $null
    $root = Get-RepositoryRoot
    if ($root) {
        $candidate = Join-Path $root 'src'
        $marker = Join-Path $candidate 'local_ai_switchboard\diagnostics\image_pipeline.py'
        if (Test-Path -LiteralPath $marker) {
            $source = $candidate
        }
    }
    if ($source) {
        if ([string]::IsNullOrWhiteSpace($previous)) {
            $env:PYTHONPATH = $source
        } else {
            $env:PYTHONPATH = $source + [System.IO.Path]::PathSeparator + $previous
        }
    }
    try {
        return (Invoke-QuietNative -FilePath $script:Python -ArgumentList @('-c', $Code))
    } finally {
        if ($null -eq $previous) {
            Remove-Item -Path 'Env:PYTHONPATH' -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONPATH = $previous
        }
    }
}

function Get-ErrorDetail {
    param($ErrorRecord)
    $parts = @()
    $current = $ErrorRecord.Exception
    while ($null -ne $current) {
        $message = [string]$current.Message
        if ($message -and (($parts.Count -eq 0) -or ($parts[$parts.Count - 1] -ne $message))) {
            $parts += $message
        }
        $current = $current.InnerException
    }
    $text = $parts -join ' '
    $line = 0
    if ($ErrorRecord.InvocationInfo) {
        $line = [int]$ErrorRecord.InvocationInfo.ScriptLineNumber
    }
    if ($line -gt 0) {
        $text = "Line ${line}: $text"
    }
    return $text
}

function Get-RunningStage {
    foreach ($name in @('A', 'B', 'C', 'D', 'E', 'F', 'record')) {
        if ($script:Stages[$name] -eq 'running') {
            return $name
        }
    }
    return ''
}

function Add-GpuSample {
    if (-not $script:NvidiaSmi) {
        return
    }
    $sample = Invoke-QuietNative -FilePath $script:NvidiaSmi -ArgumentList @('--query-gpu=timestamp,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw', '--format=csv,noheader,nounits')
    if ($sample.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($sample.Text)) {
        return
    }
    $line = @($sample.Text -split '\r?\n' | Where-Object { $_ -match '^\d' })[0]
    if ($line) {
        Add-Content -LiteralPath $script:CsvPath -Value ([string]$line).Trim() -Encoding UTF8
    }
}

function Get-CsvDataRows {
    if (-not (Test-Path -LiteralPath $script:CsvPath)) {
        return 0
    }
    $rows = @(Get-Content -LiteralPath $script:CsvPath)
    if ($rows.Count -le 1) {
        return 0
    }
    return ($rows.Count - 1)
}

function Test-InferenceOverlap {
        $running = @()
        $models = @()
        try {
            $queue = Invoke-RestMethod -Uri 'http://127.0.0.1:8188/queue' -TimeoutSec 5
            $runningValue = Get-ObjectProperty -Object $queue -Name 'queue_running'
            if ($null -ne $runningValue) {
                $running = @($runningValue)
            }
        } catch {
            return
        }
        try {
            $resident = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/ps' -TimeoutSec 5
            $residentModels = Get-ObjectProperty -Object $resident -Name 'models'
            if ($null -ne $residentModels) {
                $models = @($residentModels)
            }
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

function Get-InstallEnvMap {
    $map = @{}
    $envFile = Join-Path $InstallRoot '.env'
    if (-not (Test-Path -LiteralPath $envFile)) {
        return $map
    }
    foreach ($line in @(Get-Content -LiteralPath $envFile)) {
        if ($line -match '^\s*#' -or $line -notmatch '=') {
            continue
        }
        $parts = $line.Split('=', 2)
        $name = $parts[0].Trim()
        if ($name) {
            $map[$name] = $parts[1]
        }
    }
    return $map
}

function Test-PathInsideRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )
    $full = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $base = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ([string]::Compare($full, $base, [System.StringComparison]::OrdinalIgnoreCase) -eq 0) {
        return $true
    }
    $prefix = $base + '\'
    return $full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-SwitchboardLogPath {
    return (Join-Path $script:LogDir 'switchboard.log')
}

function Read-FileFromOffset {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int64]$Offset = 0
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return ''
    }
    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::ReadWrite
    )
    $reader = $null
    try {
        if ($Offset -lt 0 -or $Offset -gt $stream.Length) {
            $Offset = 0
        }
        [void]$stream.Seek($Offset, [System.IO.SeekOrigin]::Begin)
        $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
        return $reader.ReadToEnd()
    } finally {
        if ($null -ne $reader) {
            $reader.Dispose()
        } else {
            $stream.Dispose()
        }
    }
}

function Get-SchedulerState {
    param([Parameter(Mandatory = $true)][string]$Text)
    $excerpt = Join-Path $script:ReportDir 'scheduler-excerpt.txt'
    Write-Utf8File -Path $excerpt -Text $Text
    $env:LAS_LOG_EXCERPT = $excerpt
    $parsed = Invoke-PipelinePython -Code 'import os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import latest_scheduler_state; value = latest_scheduler_state(Path(os.environ["LAS_LOG_EXCERPT"]).read_text(encoding="utf-8", errors="replace")); print("" if value is None else value)'
    if ($parsed.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($parsed.Text)) {
        return @{ Known = $false; State = '' }
    }
    return @{ Known = $true; State = $parsed.Text.Trim() }
}

function Wait-SchedulerIdle {
    param(
        [int64]$Offset,
        [int]$TimeoutSeconds = 30
    )
    $state = @{ Known = $false; State = '' }
    $text = ''
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $text = Read-FileFromOffset -Path (Get-SwitchboardLogPath) -Offset $Offset
        $state = Get-SchedulerState -Text $text
        if ($state.Known -and $state.State -eq 'IDLE') {
            return @{ Ok = $true; State = 'IDLE'; Text = $text }
        }
        if ($state.Known -and ($state.State -eq 'ERROR' -or $state.State -eq 'STOPPING')) {
            return @{ Ok = $false; State = $state.State; Text = $text }
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    $finalState = 'missing'
    if ($state.Known) {
        $finalState = $state.State
    }
    return @{ Ok = $false; State = $finalState; Text = $text }
}

function Get-GatewayOwnership {
    $live = Test-HttpStatus -Url "http://127.0.0.1:$($script:ApiPort)/health/live" -TimeoutSeconds 3
    if ($live -ne 200) {
        return 'down'
    }
    $processId = Get-GatewayProcessId -InstallRoot $InstallRoot
    if ($processId -eq 0) {
        return 'foreign'
    }
    $command = Get-ProcessCommandLine -ProcessId $processId
    $root = [System.IO.Path]::GetFullPath($InstallRoot)
    if ($command -notlike "*$root*") {
        return 'foreign'
    }
    return 'owned'
}

function Assert-OwnedGatewayQuiet {
    $readyNow = Test-HttpStatus -Url "http://127.0.0.1:$($script:ApiPort)/health/ready" -TimeoutSeconds 5
    if ($readyNow -ne 200) {
        Fail-PipelineStage -Stage 'A' -Detail 'The gateway for this install is not ready. It was not stopped.'
    }
    $dbPath = Join-Path $script:StorageDir 'switchboard.db'
    $env:LAS_JOB_DB = $dbPath
    $inspected = Invoke-PipelinePython -Code 'import json, os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import inspect_image_jobs; print(json.dumps(inspect_image_jobs(Path(os.environ["LAS_JOB_DB"]))))'
    if ($inspected.ExitCode -ne 0 -or -not $inspected.Text) {
        $script:BlockedReason = 'scheduler_unreadable'
        Fail-PipelineStage -Stage 'A' -Detail 'The image job database could not be inspected. The gateway was not stopped.'
    }
    $jobState = $inspected.Text | ConvertFrom-Json
    $jobsOk = Get-ObjectProperty -Object $jobState -Name 'ok'
    if (-not $jobsOk) {
        $script:BlockedReason = 'scheduler_unreadable'
        Fail-PipelineStage -Stage 'A' -Detail (([string](Get-ObjectProperty -Object $jobState -Name 'detail')) + ' The gateway was not stopped.')
    }
    $openJobs = [int](Get-ObjectProperty -Object $jobState -Name 'count')
    if ($openJobs -gt 0) {
        $script:BlockedReason = 'active_jobs'
        Fail-PipelineStage -Stage 'A' -Detail "SQLite has $openJobs queued or running image job(s). Wait for them to finish. The gateway was not stopped."
    }
    $listeners = @(Get-ListeningEntries -Port 8188)
    if ($listeners.Count -gt 0) {
        foreach ($entry in $listeners) {
            if (-not (Test-LoopbackAddress -Address ([string]$entry.LocalAddress))) {
                Fail-PipelineStage -Stage 'A' -Detail 'Port 8188 is listening outside 127.0.0.1. That process was not stopped.'
            }
        }
        try {
            $queue = Invoke-RestMethod -Uri 'http://127.0.0.1:8188/queue' -TimeoutSec 5
            $runningCount = 0
            $pendingCount = 0
            $runningValue = Get-ObjectProperty -Object $queue -Name 'queue_running'
            $pendingValue = Get-ObjectProperty -Object $queue -Name 'queue_pending'
            if ($null -ne $runningValue) {
                $runningCount = @($runningValue).Count
            }
            if ($null -ne $pendingValue) {
                $pendingCount = @($pendingValue).Count
            }
            if ($runningCount -gt 0 -or $pendingCount -gt 0) {
                $script:BlockedReason = 'active_jobs'
                Fail-PipelineStage -Stage 'A' -Detail 'ComfyUI still has a running or pending prompt. Wait for it to finish. No process was stopped.'
            }
        } catch {
            $script:BlockedReason = 'scheduler_unreadable'
            Fail-PipelineStage -Stage 'A' -Detail 'ComfyUI is listening but /queue could not be read. No process was stopped.'
        }
        $portableIds = @(Get-ComfyProcessIds -PortableRoot $script:Portable)
        if ($portableIds.Count -eq 0) {
            Fail-PipelineStage -Stage 'A' -Detail 'Port 8188 is in use by a process that is not the portable ComfyUI. A second ComfyUI process was not started.'
        }
    }
    $existingLog = ''
    if (Test-Path -LiteralPath (Get-SwitchboardLogPath)) {
        $existingLog = Read-FileFromOffset -Path (Get-SwitchboardLogPath) -Offset 0
    }
    $currentState = Get-SchedulerState -Text $existingLog
    if ($currentState.Known -and $currentState.State -eq 'ERROR') {
        Fail-PipelineStage -Stage 'A' -Detail 'The GPU scheduler state is ERROR. This check will not stop the gateway.'
    }
    if ($currentState.Known -and $currentState.State -eq 'STOPPING') {
        Fail-PipelineStage -Stage 'A' -Detail 'The GPU scheduler state is STOPPING. This check will not stop the gateway.'
    }
    if ($currentState.Known -and $currentState.State -ne 'IDLE') {
        $script:BlockedReason = 'active_jobs'
        Fail-PipelineStage -Stage 'A' -Detail "The GPU scheduler state is $($currentState.State). Wait until it returns to IDLE. No process was stopped."
    }
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
    $script:Report.comfyui_checked = $true
    Set-StageResult -Name 'C' -Status 'pass'
    Write-InstallLog 'ComfyUI object info matches the adapted graph.'
}

function Save-ComfySystemStats {
    $target = Join-Path $script:ReportDir 'system-stats.json'
    try {
        Invoke-WebRequest -Uri 'http://127.0.0.1:8188/system_stats' -OutFile $target -UseBasicParsing -TimeoutSec 15 | Out-Null
        return $true
    } catch {
        Add-ErrorLog "ComfyUI system_stats was not readable. $($_.Exception.Message)"
        return $false
    }
}

function Update-OffloadObservation {
    param([string]$LogText = '')
    $statsPath = Join-Path $script:ReportDir 'system-stats.json'
    $logPath = Join-Path $script:ReportDir 'offload-log.txt'
    Write-Utf8File -Path $logPath -Text $LogText
    $env:LAS_STATS = $statsPath
    $env:LAS_OFFLOAD_LOG = $logPath
    $observed = Invoke-PipelinePython -Code @'
import json, os
from pathlib import Path
from local_ai_switchboard.diagnostics.image_pipeline import describe_offload
stats_path = Path(os.environ["LAS_STATS"])
stats = None
if stats_path.is_file():
    stats = json.loads(stats_path.read_text(encoding="utf-8-sig"))
log_text = Path(os.environ["LAS_OFFLOAD_LOG"]).read_text(encoding="utf-8")
print(json.dumps(describe_offload(stats if isinstance(stats, dict) else None, log_text)))
'@
    if ($observed.ExitCode -eq 0 -and $observed.Text) {
        $script:Offload = $observed.Text | ConvertFrom-Json
        $observedVersion = Get-ObjectProperty -Object $script:Offload -Name 'comfyui_version'
        if ($observedVersion) {
            $script:Report.comfyui_version_observed = [string]$observedVersion
        }
    }
}

function Invoke-ImageAttempt {
    $bodyObject = @{
        workflow_id = 'flux2-klein-t2i'
        prompt      = $script:Prompt
        width       = $Width
        height      = $Height
        seed        = $Seed
        steps       = 4
    }
    $body = $bodyObject | ConvertTo-Json -Compress
    if (-not $script:GenerationStarted) {
        $script:GenerationStarted = Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'
    }
    $startedAt = Get-Date
    $logOffset = [int64]0
    $logPath = Get-SwitchboardLogPath
    if (Test-Path -LiteralPath $logPath) {
        $logOffset = [int64](Get-Item -LiteralPath $logPath).Length
    }
    Add-GpuSample
    try {
        $created = Invoke-WebRequest -Uri "http://127.0.0.1:$($script:ApiPort)/v1/assets" -Method POST -Body $body -ContentType 'application/json; charset=utf-8' -UseBasicParsing -TimeoutSec 60
    } catch {
        $detail = [string]$_.Exception.Message
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $detail = [string]$_.ErrorDetails.Message
        }
        Add-GpuSample
        $script:GenerationFinished = Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'
        $script:GenerationSeconds = [int]((Get-Date) - $startedAt).TotalSeconds
        return @{ status = 'failed'; error = $detail; id = ''; logOffset = $logOffset }
    }
    $createdJob = $created.Content | ConvertFrom-Json
    $jobId = [string](Get-ObjectProperty -Object $createdJob -Name 'id')
    if (-not $jobId) {
        Add-GpuSample
        $script:GenerationFinished = Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'
        $script:GenerationSeconds = [int]((Get-Date) - $startedAt).TotalSeconds
        return @{ status = 'failed'; error = 'The gateway did not return a job id.'; id = ''; logOffset = $logOffset }
    }
    $deadline = (Get-Date).AddMinutes(8)
    $last = $null
    do {
        Add-GpuSample
        Test-InferenceOverlap
        if (-not $script:Report.comfyui_checked) {
            $listening = Test-HttpStatus -Url 'http://127.0.0.1:8188/system_stats' -TimeoutSeconds 3
            if ($listening -eq 200) {
                Save-ComfySystemStats | Out-Null
            }
        }
        try {
            $last = Invoke-RestMethod -Uri "http://127.0.0.1:$($script:ApiPort)/v1/jobs/$jobId" -TimeoutSec 30
        } catch {
            $script:GenerationFinished = Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'
            $script:GenerationSeconds = [int]((Get-Date) - $startedAt).TotalSeconds
            return @{ status = 'failed'; error = [string]$_.Exception.Message; id = $jobId; logOffset = $logOffset }
        }
        $status = [string](Get-ObjectProperty -Object $last -Name 'status')
        if ($status -eq 'succeeded' -or $status -eq 'failed' -or $status -eq 'cancelled') {
            break
        }
        Start-Sleep -Seconds $SampleIntervalSeconds
    } while ((Get-Date) -lt $deadline)
    Add-GpuSample
    $script:GenerationFinished = Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'
    $script:GenerationSeconds = [int]((Get-Date) - $startedAt).TotalSeconds
    if ($null -eq $last) {
        return @{ status = 'failed'; error = 'The image job was not readable.'; id = $jobId; logOffset = $logOffset }
    }
    $finalStatus = [string](Get-ObjectProperty -Object $last -Name 'status')
    if ($finalStatus -ne 'succeeded' -and $finalStatus -ne 'failed' -and $finalStatus -ne 'cancelled') {
        $finalStatus = 'failed'
        $lastError = 'The image job did not finish before the verifier timeout.'
    } else {
        $lastError = [string](Get-ObjectProperty -Object $last -Name 'error')
    }
    return @{ status = $finalStatus; error = $lastError; id = $jobId; logOffset = $logOffset }
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

function Invoke-GatewayChat {
    param([Parameter(Mandatory = $true)][string]$ContentText)
    Add-Type -AssemblyName System.Net.Http
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromMinutes(5)
    $logOffset = [int64]0
    $logPath = Get-SwitchboardLogPath
    if (Test-Path -LiteralPath $logPath) {
        $logOffset = [int64](Get-Item -LiteralPath $logPath).Length
    }
    try {
        $chat = @{
            model    = 'qwen3.5:4b-ctx8192'
            stream   = $false
            messages = @(@{ role = 'user'; content = $ContentText })
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
            return @{ Ok = $false; Text = [string]$raw; LogOffset = $logOffset }
        }
        $parsed = $raw | ConvertFrom-Json
        $choice = Get-ObjectProperty -Object $parsed -Name 'choices'
        if ($choice -is [System.Array]) {
            $choice = $choice[0]
        }
        $message = Get-ObjectProperty -Object $choice -Name 'message'
        $reply = [string](Get-ObjectProperty -Object $message -Name 'content')
        return @{ Ok = $true; Text = $reply.Trim(); LogOffset = $logOffset }
    } catch {
        $detail = Get-ErrorDetail -ErrorRecord $_
        return @{ Ok = $false; Text = $detail; LogOffset = $logOffset }
    } finally {
        $client.Dispose()
    }
}

function Test-OllamaResident {
    try {
        $resident = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/ps' -TimeoutSec 15
    } catch {
        return $false
    }
    $models = @()
    $residentModels = Get-ObjectProperty -Object $resident -Name 'models'
    if ($null -ne $residentModels) {
        $models = @($residentModels)
    }
    foreach ($model in $models) {
        $modelName = [string](Get-ObjectProperty -Object $model -Name 'name')
        if ($modelName -like 'qwen3.5:4b-ctx8192*') {
            return $true
        }
    }
    return $false
}

function Restore-LiveWorkflows {
    $env:LAS_PIPELINE_SOURCE = $script:WorkflowsDir
    $env:LAS_PIPELINE_BACKUP = $script:BackupDir
    $restored = Invoke-PipelinePython -Code 'import os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import restore_workflow_snapshot; restore_workflow_snapshot(Path(os.environ["LAS_PIPELINE_SOURCE"]), Path(os.environ["LAS_PIPELINE_BACKUP"]))'
    if ($restored.ExitCode -ne 0) {
        Add-ErrorLog $restored.Text
        return $false
    }
    return $true
}

function Restore-VerificationRecord {
    $recordPath = Join-Path $InstallRoot 'config\image-pipeline.json'
    $backupPath = Join-Path $script:ReportDir 'previous-image-pipeline.json'
    if ($script:HadPreviousRecord -and (Test-Path -LiteralPath $backupPath)) {
        Copy-Item -LiteralPath $backupPath -Destination $recordPath -Force
        return
    }
    if (Test-Path -LiteralPath $recordPath) {
        Remove-Item -LiteralPath $recordPath -Force
    }
}

function Save-GatewayLog {
    $destination = Join-Path $script:ReportDir 'gateway.log'
    $path = Get-SwitchboardLogPath
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Utf8File -Path $destination -Text "gateway log was not available at $path`r`n"
        $script:GatewayLogState = 'note'
        return
    }
    $text = Read-FileFromOffset -Path $path -Offset $script:LogStart
    $stderr = Join-Path $script:LogDir 'gateway.stderr.log'
    if (Test-Path -LiteralPath $stderr) {
        $errText = Read-FileFromOffset -Path $stderr -Offset 0
        if ($errText) {
            $text = $text + "`r`n--- gateway.stderr.log ---`r`n" + $errText
        }
    }
    if ([string]::IsNullOrWhiteSpace($text)) {
        Write-Utf8File -Path $destination -Text "gateway log had no new lines during this run.`r`n"
        $script:GatewayLogState = 'note'
        return
    }
    Write-Utf8File -Path $destination -Text $text
    $script:GatewayLogState = 'written'
}

function Save-ComfyLog {
    $extraPath = Join-Path $script:ReportDir 'comfy-source.log'
    $extraFound = $false
    if ($script:Portable) {
        foreach ($relative in @('ComfyUI\user\comfyui.log', 'ComfyUI\comfyui.log')) {
            $candidate = Join-Path $script:Portable $relative
            if (Test-Path -LiteralPath $candidate) {
                Write-Utf8File -Path $extraPath -Text (Read-FileFromOffset -Path $candidate -Offset 0)
                $extraFound = $true
                break
            }
        }
    }
    if (-not $extraFound) {
        Write-Utf8File -Path $extraPath -Text ''
    }
    $gatewayLog = Join-Path $script:ReportDir 'gateway.log'
    $env:LAS_COMFY_EXTRA = $extraPath
    $env:LAS_COMFY_GATEWAY = $gatewayLog
    $env:LAS_COMFY_OUT = Join-Path $script:ReportDir 'comfyui.log'
    $written = Invoke-PipelinePython -Code @'
import os
from pathlib import Path
from local_ai_switchboard.diagnostics.image_pipeline import comfyui_log_text
extra_path = Path(os.environ["LAS_COMFY_EXTRA"])
gateway_path = Path(os.environ["LAS_COMFY_GATEWAY"])
extra = extra_path.read_text(encoding="utf-8", errors="replace") if extra_path.is_file() else ""
gateway = gateway_path.read_text(encoding="utf-8", errors="replace") if gateway_path.is_file() else ""
document = comfyui_log_text(gateway, extra)
Path(os.environ["LAS_COMFY_OUT"]).write_text(document["text"], encoding="utf-8")
print("yes" if document["collected"] else "no")
'@
    if ($written.ExitCode -eq 0 -and $written.Text -eq 'yes') {
        $script:ComfyLogState = 'written'
    } else {
        $script:ComfyLogState = 'note'
        if (-not (Test-Path -LiteralPath (Join-Path $script:ReportDir 'comfyui.log'))) {
            Write-Utf8File -Path (Join-Path $script:ReportDir 'comfyui.log') -Text "ComfyUI log was not available.`r`n"
        }
    }
}

function Write-FallbackReport {
    $result = 'FAIL'
    if ($script:BlockedReason -eq 'active_jobs' -or $script:BlockedReason -eq 'scheduler_unreadable') {
        $result = 'PARTIAL'
    }
    $script:Result = $result
    $text = @(
        $result,
        '',
        'The structured report helper failed.',
        "Stage: $($script:FailureStage)",
        $script:FailureDetail
    ) -join "`r`n"
    Write-Utf8File -Path (Join-Path $script:ReportDir 'report.md') -Text ($text + "`r`n")
    Write-Utf8File -Path (Join-Path $script:ReportDir 'environment.json') -Text "{`r`n  `"available`": false`r`n}`r`n"
}

function Write-PipelineReport {
    try {
        Save-GatewayLog
        Save-ComfyLog
        if (Test-Path -LiteralPath $script:CsvPath) {
            $env:LAS_GPU_CSV = $script:CsvPath
            $summary = Invoke-PipelinePython -Code 'import json, os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import summarize_gpu_csv; print(json.dumps(summarize_gpu_csv(Path(os.environ["LAS_GPU_CSV"]).read_text(encoding="utf-8-sig"))))'
            if ($summary.ExitCode -eq 0 -and $summary.Text) {
                $parsed = $summary.Text | ConvertFrom-Json
                $script:Report.peak_vram_mib = $parsed.peak_vram_mib
                $script:Report.gpu_temperature_c = $parsed.peak_temperature_c
                $script:Report.initial_vram_mib = $parsed.initial_vram_mib
                $script:Report.final_vram_mib = $parsed.final_vram_mib
                $script:Report.memory_total_mib = $parsed.memory_total_mib
                $script:Report.peak_utilization_gpu = $parsed.peak_utilization_gpu
                $script:Report.peak_power_w = $parsed.peak_power_w
                $script:Report.gpu_started_at = $parsed.started_at
                $script:Report.gpu_finished_at = $parsed.finished_at
            }
        }
        $script:Report.generation_seconds = $script:GenerationSeconds
        $script:Report.generation_started_at = $script:GenerationStarted
        $script:Report.generation_finished_at = $script:GenerationFinished
        $script:Report.failure_stage = $script:FailureStage
        $script:Report.failure_detail = $script:FailureDetail
        $script:Report.blocked_reason = $script:BlockedReason
        $script:Report.launch_profile = $script:LaunchProfile
        $script:Report.vram_fit_claimed = $false
        $script:Report.stages = $script:Stages
        $script:Report.offload = $script:Offload
        $imagePath = Join-Path $script:ReportDir 'generated-image.png'
        $gatewayLabel = 'note'
        if ($script:GatewayLogState -eq 'written') {
            $gatewayLabel = $true
        }
        $comfyLabel = 'note'
        if ($script:ComfyLogState -eq 'written') {
            $comfyLabel = $true
        }
        $script:Report.files = @{
            report_md         = $true
            report_json       = $true
            gpu_metrics_csv   = (Test-Path -LiteralPath $script:CsvPath)
            gateway_log       = $gatewayLabel
            comfyui_log       = $comfyLabel
            generated_image   = (Test-Path -LiteralPath $imagePath)
            environment_json  = $true
        }
        $script:Report.environment = @{
            available              = $true
            windows_caption        = $script:Report.windows_caption
            windows_build          = $script:Report.windows_build
            gpu                    = $script:Report.hardware_gpu
            driver                 = $script:Report.hardware_driver
            vram_mib               = $script:Report.hardware_vram_mib
            memory_used_mib        = $script:Report.hardware_memory_used_mib
            ram_bytes              = $script:Report.hardware_ram_bytes
            disk_free_bytes        = $script:Report.hardware_disk_free_bytes
            comfyui_pin            = $script:Report.comfyui_pin
            comfyui_version_observed = $script:Report.comfyui_version_observed
            python                 = $script:Python
            install_root           = $InstallRoot
            report_dir             = $script:ReportDir
            models                 = @($script:ModelRecords)
            workflow_id            = 'flux2-klein-t2i'
            disabled_slot          = 'flux2-klein-4b'
            mac_lan_checked        = $false
        }
        $inputPath = Join-Path $script:ReportDir 'report-input.json'
        $script:Report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $inputPath -Encoding UTF8
        $env:LAS_REPORT_INPUT = $inputPath
        $env:LAS_REPORT_DIR = $script:ReportDir
        $env:LAS_ERROR_LOG = $script:ErrorLog
        $env:LAS_GPU_CSV = $script:CsvPath
        $written = Invoke-PipelinePython -Code @'
import json, os
from pathlib import Path
from local_ai_switchboard.diagnostics.image_pipeline import write_pipeline_report
report = json.loads(Path(os.environ["LAS_REPORT_INPUT"]).read_text(encoding="utf-8-sig"))
csv_path = Path(os.environ["LAS_GPU_CSV"])
csv_text = csv_path.read_text(encoding="utf-8-sig") if csv_path.is_file() else ""
print(write_pipeline_report(Path(os.environ["LAS_REPORT_DIR"]), report, csv_text=csv_text, error_log=os.environ.get("LAS_ERROR_LOG", "")))
'@
        if ($written.ExitCode -eq 0 -and $written.Text) {
            $script:Result = ($written.Text -split '\r?\n' | Select-Object -Last 1).Trim()
        } else {
            Add-ErrorLog $written.Text
            Write-FallbackReport
        }
    } catch {
        Add-ErrorLog $_.Exception.Message
        Write-InstallLog "The report could not be written. $($_.Exception.Message)" 'ERROR'
        Write-FallbackReport
    }
}

function Publish-LatestReport {
    $latest = Join-Path $InstallRoot 'data\diagnostics\image-pipeline\latest'
    if (Test-Path -LiteralPath $latest) {
        Remove-Item -LiteralPath $latest -Recurse -Force
    }
    Copy-Item -LiteralPath $script:ReportDir -Destination $latest -Recurse
}

New-Item -ItemType Directory -Force -Path $script:ReportDir | Out-Null
Set-Content -LiteralPath $script:CsvPath -Value 'timestamp,memory_used_mib,memory_total_mib,utilization_gpu,temperature_c,power_w' -Encoding ASCII
$logAtStart = Get-SwitchboardLogPath
if (Test-Path -LiteralPath $logAtStart) {
    $script:LogStart = [int64](Get-Item -LiteralPath $logAtStart).Length
}

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

    Write-InstallLog 'Stage A: preflight.'
    Set-StageResult -Name 'A' -Status 'running'
    $osInfo = Get-CimInstance -ClassName Win32_OperatingSystem
    $script:Report.windows_caption = [string]$osInfo.Caption
    $script:Report.windows_build = [string]$osInfo.BuildNumber
    if ([int]$osInfo.BuildNumber -lt 22000) {
        Fail-PipelineStage -Stage 'A' -Detail "Windows build $($osInfo.BuildNumber) is older than Windows 11."
    }
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
    $used = Invoke-QuietNative -FilePath $script:NvidiaSmi -ArgumentList @('--query-gpu=memory.used', '--format=csv,noheader,nounits')
    $usedValue = $null
    if ($used.ExitCode -eq 0) {
        $usedText = @($used.Text -split '\r?\n' | Where-Object { $_ -match '^\d' })[0]
        $parsedUsed = 0
        if ($usedText -and [int]::TryParse($usedText.Trim(), [ref]$parsedUsed)) {
            $usedValue = $parsedUsed
        }
    }
    Add-GpuSample
    $ramBytes = [int64]$osInfo.TotalVisibleMemorySize * 1024
    $freeBytes = Get-FreeBytesForPath -Path $InstallRoot
    $script:Report.hardware_gpu = [string]$gpu.Name
    $script:Report.hardware_driver = [string]$gpu.Driver
    $script:Report.hardware_vram_mib = [int]$gpu.MemoryMiB
    $script:Report.hardware_memory_used_mib = $usedValue
    $script:Report.hardware_ram_bytes = $ramBytes
    $script:Report.hardware_disk_free_bytes = $freeBytes
    Write-InstallLog "Windows $($osInfo.Caption) build $($osInfo.BuildNumber). GPU $($gpu.Name); driver $($gpu.Driver); VRAM $($gpu.MemoryMiB) MiB; used $usedValue MiB. This does not claim the model fits fully in VRAM."

    if (-not (Test-Path -LiteralPath $script:Python)) {
        Fail-PipelineStage -Stage 'A' -Detail "The gateway interpreter is missing at $($script:Python)."
    }
    $package = Invoke-QuietNative -FilePath $script:Python -ArgumentList @('-c', "import importlib.metadata as metadata; print(metadata.version('local-ai-switchboard'))")
    if ($package.ExitCode -ne 0) {
        Fail-PipelineStage -Stage 'A' -Detail "The installed local-ai-switchboard package could not be imported. This check does not reinstall it. $($package.Text)"
    }
    Write-InstallLog "Installed gateway package $(([string]$package.Text).Trim())."

    $script:Pins = Get-InstallerPins
    $script:Report.comfyui_pin = [string]$script:Pins.comfyui.version
    $script:Portable = Find-ComfyPortableRoot -Path (Join-Path $InstallRoot 'ComfyUI_windows_portable')
    if (-not $script:Portable) {
        Fail-PipelineStage -Stage 'A' -Detail 'The ComfyUI portable tree was not found.'
    }
    $expectedModels = @($script:Pins.comfy_models)
    if ($expectedModels.Count -ne 3) {
        Fail-PipelineStage -Stage 'A' -Detail 'pins.json does not list the three FLUX.2 Klein model files.'
    }
    foreach ($model in $expectedModels) {
        $modelPath = Join-Path $script:Portable "ComfyUI\models\$($model.folder)\$($model.filename)"
        if (-not (Test-Path -LiteralPath $modelPath)) {
            $wrongEncoder = Join-Path $script:Portable 'ComfyUI\models\text_encoders\qwen_3_4b.safetensors'
            if ([string]$model.filename -eq 'qwen_3_4b_fp4_flux2.safetensors' -and (Test-Path -LiteralPath $wrongEncoder)) {
                Fail-PipelineStage -Stage 'A' -Detail 'qwen_3_4b.safetensors is present, but it is not the FP4 encoder qwen_3_4b_fp4_flux2.safetensors. This check does not download models.'
            }
            Fail-PipelineStage -Stage 'A' -Detail "Missing model file $($model.filename). This check does not download models."
        }
        $matched = Test-PinnedFileHash -Path $modelPath -Algorithm ([string]$model.algorithm) -Checksum ([string]$model.checksum) -ExpectedBytes ([int64]$model.expected_bytes)
        if (-not $matched) {
            Fail-PipelineStage -Stage 'A' -Detail "The checksum or size for $($model.filename) does not match the pin. This check does not download models."
        }
        $record = @{
            filename = [string]$model.filename
            folder   = [string]$model.folder
            sha256   = ([string]$model.checksum).ToLowerInvariant()
        }
        $revision = Get-ObjectProperty -Object $model -Name 'revision'
        if ($revision) {
            $record.revision = [string]$revision
        }
        $script:ModelRecords += $record
        Write-InstallLog "Verified $($model.filename)."
    }
    $script:Report.models_verified = $true

    $envMap = Get-InstallEnvMap
    $envFile = Join-Path $InstallRoot '.env'
    if (-not (Test-Path -LiteralPath $envFile)) {
        Fail-PipelineStage -Stage 'A' -Detail '.env is missing from this install.'
    }
    $configuredRoot = [string]$envMap['LAS_INSTALL_ROOT']
    if (-not $configuredRoot) {
        Fail-PipelineStage -Stage 'A' -Detail '.env does not set LAS_INSTALL_ROOT for this install.'
    }
    $left = [System.IO.Path]::GetFullPath($configuredRoot).TrimEnd('\')
    $right = [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
    if ([string]::Compare($left, $right, [System.StringComparison]::OrdinalIgnoreCase) -ne 0) {
        Fail-PipelineStage -Stage 'A' -Detail "LAS_INSTALL_ROOT is $left. This check was started for $right."
    }
    $ollamaUrl = [string]$envMap['LAS_OLLAMA_URL']
    if (-not $ollamaUrl) {
        $ollamaUrl = 'http://127.0.0.1:11434'
    }
    if ($ollamaUrl -notlike 'http://127.0.0.1:11434*') {
        Fail-PipelineStage -Stage 'A' -Detail 'LAS_OLLAMA_URL is not the loopback pin.'
    }
    $comfyUrl = [string]$envMap['LAS_COMFYUI_URL']
    if (-not $comfyUrl) {
        $comfyUrl = 'http://127.0.0.1:8188'
    }
    if ($comfyUrl -notlike 'http://127.0.0.1:8188*') {
        Fail-PipelineStage -Stage 'A' -Detail 'LAS_COMFYUI_URL is not the loopback pin.'
    }
    $installedPath = Join-Path $InstallRoot 'config\installed.json'
    if (-not (Test-Path -LiteralPath $installedPath)) {
        Fail-PipelineStage -Stage 'A' -Detail 'config\installed.json is missing.'
    }
    $installed = Get-Content -LiteralPath $installedPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $profileName = [string](Get-ObjectProperty -Object $installed -Name 'profile')
    if ($profileName -ne 'Design') {
        Fail-PipelineStage -Stage 'A' -Detail 'The image pipeline belongs to the Design profile. This install is not Design.'
    }
    if ($envMap.ContainsKey('LAS_WORKFLOWS_DIR') -and $envMap['LAS_WORKFLOWS_DIR']) {
        if (-not (Test-PathInsideRoot -Path ([string]$envMap['LAS_WORKFLOWS_DIR']) -Root $InstallRoot)) {
            Fail-PipelineStage -Stage 'A' -Detail 'LAS_WORKFLOWS_DIR is outside this install.'
        }
        $script:WorkflowsDir = [System.IO.Path]::GetFullPath([string]$envMap['LAS_WORKFLOWS_DIR'])
    }
    if ($envMap.ContainsKey('LAS_STORAGE_DIR') -and $envMap['LAS_STORAGE_DIR']) {
        if (-not (Test-PathInsideRoot -Path ([string]$envMap['LAS_STORAGE_DIR']) -Root $InstallRoot)) {
            Fail-PipelineStage -Stage 'A' -Detail 'LAS_STORAGE_DIR is outside this install.'
        }
        $script:StorageDir = [System.IO.Path]::GetFullPath([string]$envMap['LAS_STORAGE_DIR'])
    }
    if ($envMap.ContainsKey('LAS_LOG_DIR') -and $envMap['LAS_LOG_DIR']) {
        if (-not (Test-PathInsideRoot -Path ([string]$envMap['LAS_LOG_DIR']) -Root $InstallRoot)) {
            Fail-PipelineStage -Stage 'A' -Detail 'LAS_LOG_DIR is outside this install.'
        }
        $script:LogDir = [System.IO.Path]::GetFullPath([string]$envMap['LAS_LOG_DIR'])
    }
    $script:ApiPort = Get-ConfiguredApiPort -InstallRoot $InstallRoot -DefaultPort 8787

    $candidate = Get-CandidateManifest
    if (-not $candidate) {
        Fail-PipelineStage -Stage 'A' -Detail 'The flux2-klein-t2i manifest was not found.'
    }
    $validated = Get-WorkflowManifestState -Python $script:Python -ManifestPath $candidate
    if (-not $validated.Ok -or $validated.Id -ne 'flux2-klein-t2i' -or $validated.State -ne 'enabled' -or $validated.Graph -ne 'yes') {
        Fail-PipelineStage -Stage 'A' -Detail "validate_manifest rejected the candidate. $($validated.Detail)"
    }
    $env:LAS_MANIFEST = $candidate
    $graphProblems = Invoke-PipelinePython -Code 'import os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import manifest_configuration_problems; print("\n".join(manifest_configuration_problems(Path(os.environ["LAS_MANIFEST"])))); raise SystemExit(1 if manifest_configuration_problems(Path(os.environ["LAS_MANIFEST"])) else 0)'
    if ($graphProblems.ExitCode -ne 0) {
        Fail-PipelineStage -Stage 'A' -Detail "The adapted workflow does not match the FLUX.2 Klein configuration. $($graphProblems.Text)"
    }
    $disabledSlot = Join-Path $InstallRoot 'workflows\flux2-klein-4b.json'
    if (Test-Path -LiteralPath $disabledSlot) {
        $slot = Get-WorkflowManifestState -Python $script:Python -ManifestPath $disabledSlot
        if ($slot.Ok -and $slot.State -eq 'enabled') {
            Fail-PipelineStage -Stage 'A' -Detail 'flux2-klein-4b is enabled. This verifier does not modify that disabled slot.'
        }
    }
    $script:Report.workflow_validated = $true

    $ownership = Get-GatewayOwnership
    if ($ownership -eq 'foreign') {
        Fail-PipelineStage -Stage 'A' -Detail "A gateway on port $($script:ApiPort) is not the local-ai-switchboard process for this install. It was not stopped."
    }
    if ($ownership -eq 'owned') {
        Assert-OwnedGatewayQuiet
    } else {
        $downListeners = @(Get-ListeningEntries -Port 8188)
        if ($downListeners.Count -gt 0) {
            foreach ($entry in $downListeners) {
                if (-not (Test-LoopbackAddress -Address ([string]$entry.LocalAddress))) {
                    Fail-PipelineStage -Stage 'A' -Detail 'Port 8188 is listening outside 127.0.0.1. That process was not stopped.'
                }
            }
            try {
                $downQueue = Invoke-RestMethod -Uri 'http://127.0.0.1:8188/queue' -TimeoutSec 5
                $downRunning = 0
                $downPending = 0
                $downRunningValue = Get-ObjectProperty -Object $downQueue -Name 'queue_running'
                $downPendingValue = Get-ObjectProperty -Object $downQueue -Name 'queue_pending'
                if ($null -ne $downRunningValue) {
                    $downRunning = @($downRunningValue).Count
                }
                if ($null -ne $downPendingValue) {
                    $downPending = @($downPendingValue).Count
                }
                if ($downRunning -gt 0 -or $downPending -gt 0) {
                    $script:BlockedReason = 'active_jobs'
                    Fail-PipelineStage -Stage 'A' -Detail 'ComfyUI still has a running or pending prompt. Wait for it to finish. No process was stopped.'
                }
            } catch {
                $script:BlockedReason = 'scheduler_unreadable'
                Fail-PipelineStage -Stage 'A' -Detail 'ComfyUI is listening but /queue could not be read. No process was stopped.'
            }
        }
    }
    Set-StageResult -Name 'A' -Status 'pass'
    Write-InstallLog 'Stage A passed. No model file was downloaded.'

    Write-InstallLog 'Stage B: Ollama model and gateway text, after the candidate is loaded.'
    Set-StageResult -Name 'B' -Status 'running'
    $ollama = Find-OllamaExecutable
    if ($null -eq $ollama) {
        Fail-PipelineStage -Stage 'B' -Detail 'ollama was not found. The text model is checked separately from the image files.'
    }
    $ollamaHttp = Test-HttpStatus -Url 'http://127.0.0.1:11434/api/version' -TimeoutSeconds 5
    if ($ollamaHttp -ne 200) {
        Fail-PipelineStage -Stage 'B' -Detail "Ollama /api/version returned $ollamaHttp."
    }
    $baseModel = Invoke-CapturedCommand -FilePath $ollama.Executable -ArgumentList @('show', 'qwen3.5:4b')
    if ($baseModel.ExitCode -ne 0) {
        Fail-PipelineStage -Stage 'B' -Detail 'ollama show qwen3.5:4b failed. This check does not pull the model.'
    }
    $show = Invoke-CapturedCommand -FilePath $ollama.Executable -ArgumentList @('show', 'qwen3.5:4b-ctx8192')
    if ($show.ExitCode -ne 0 -or $show.Text -notmatch 'num_ctx\s+8192') {
        Fail-PipelineStage -Stage 'B' -Detail 'ollama show qwen3.5:4b-ctx8192 did not report num_ctx 8192.'
    }

    if ($ownership -eq 'owned') {
        $ownership = Get-GatewayOwnership
        if ($ownership -ne 'owned') {
            Fail-PipelineStage -Stage 'B' -Detail 'The gateway changed before it could be stopped. No second gateway was started.'
        }
        Assert-OwnedGatewayQuiet
        if (-not (Stop-ProductionGateway)) {
            Fail-PipelineStage -Stage 'B' -Detail 'The production gateway could not be stopped before loading the candidate.'
        }
        $stillLive = Test-HttpStatus -Url "http://127.0.0.1:$($script:ApiPort)/health/live" -TimeoutSeconds 3
        if ($stillLive -eq 200) {
            Fail-PipelineStage -Stage 'B' -Detail 'The gateway is still answering /health/live. A second gateway was not started.'
        }
    }
    $promptResult = Invoke-PipelinePython -Code 'from local_ai_switchboard.diagnostics.flux2_klein import DIAGNOSTIC_PROMPT; print(DIAGNOSTIC_PROMPT)'
    if ($promptResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($promptResult.Text)) {
        Fail-PipelineStage -Stage 'B' -Detail 'The diagnostic prompt could not be read from the gateway package.'
    }
    $script:Prompt = $promptResult.Text
    New-Item -ItemType Directory -Force -Path $script:BackupDir | Out-Null
    New-Item -ItemType Directory -Force -Path $script:WorkflowsDir | Out-Null
    $env:LAS_PIPELINE_SOURCE = $script:WorkflowsDir
    $env:LAS_PIPELINE_BACKUP = $script:BackupDir
    $snapshot = Invoke-PipelinePython -Code 'import os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import save_workflow_snapshot; save_workflow_snapshot(Path(os.environ["LAS_PIPELINE_SOURCE"]), Path(os.environ["LAS_PIPELINE_BACKUP"]))'
    if ($snapshot.ExitCode -ne 0) {
        Fail-PipelineStage -Stage 'B' -Detail $snapshot.Text
    }
    $env:LAS_PIPELINE_MANIFEST = $candidate
    $staged = Invoke-PipelinePython -Code 'import os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import stage_live_manifest; stage_live_manifest(Path(os.environ["LAS_PIPELINE_SOURCE"]), Path(os.environ["LAS_PIPELINE_MANIFEST"]))'
    if ($staged.ExitCode -ne 0) {
        Fail-PipelineStage -Stage 'B' -Detail $staged.Text
    }
    $script:Staged = $true
    if (-not (Start-ProductionGateway)) {
        Fail-PipelineStage -Stage 'B' -Detail 'The production gateway did not become ready after staging the candidate.'
    }
    $ready = Test-HttpStatus -Url "http://127.0.0.1:$($script:ApiPort)/health/ready" -TimeoutSeconds 5
    if ($ready -ne 200) {
        Fail-PipelineStage -Stage 'B' -Detail "Gateway /health/ready returned $ready after staging."
    }
    $firstChat = Invoke-GatewayChat -ContentText 'Reply with the single word ready.'
    Add-GpuSample
    if (-not $firstChat.Ok -or [string]::IsNullOrWhiteSpace($firstChat.Text)) {
        Fail-PipelineStage -Stage 'B' -Detail $(if ($firstChat.Text) { [string]$firstChat.Text } else { 'The gateway chat before the image was empty.' })
    }
    $idleBefore = Wait-SchedulerIdle -Offset ([int64]$firstChat.LogOffset) -TimeoutSeconds 30
    if (-not $idleBefore.Ok) {
        Fail-PipelineStage -Stage 'B' -Detail "The gateway log did not show scheduler state IDLE after the text request. Last state: $($idleBefore.State)."
    }
    if (-not (Test-OllamaResident)) {
        Fail-PipelineStage -Stage 'B' -Detail 'Ollama /api/ps does not show qwen3.5:4b-ctx8192 after the gateway chat.'
    }
    $beforeReply = [string]$firstChat.Text
    if ($beforeReply.Length -gt 500) {
        $beforeReply = $beforeReply.Substring(0, 500)
    }
    $script:Report.ollama_response_before = $beforeReply
    $script:Report.text_before_image = $true
    $script:Report.scheduler_idle_before_image = $true
    Set-StageResult -Name 'B' -Status 'pass'
    Write-InstallLog 'Stage B passed. The text request went through the gateway.'

    Write-InstallLog 'Stage C: ComfyUI.'
    Set-StageResult -Name 'C' -Status 'running'
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
        Save-ComfySystemStats | Out-Null
    } else {
        Set-StageResult -Name 'C' -Status 'deferred'
        Write-InstallLog 'ComfyUI is not listening. The gateway provider starts the portable process and checks object info before it submits the prompt. This script does not start a second ComfyUI.'
    }

    Write-InstallLog 'Stage D: image generation through the gateway scheduler.'
    Set-StageResult -Name 'D' -Status 'running'
    $script:SamplesBeforeImage = Get-CsvDataRows
    $attempt = Invoke-ImageAttempt
    if ($attempt.status -ne 'succeeded') {
        Add-ErrorLog $attempt.error
        $failureStage = 'D'
        $env:LAS_JOB_ERROR = [string]$attempt.error
        $classified = Invoke-PipelinePython -Code 'import os; from local_ai_switchboard.diagnostics.image_pipeline import image_failure_stage; print(image_failure_stage(os.environ.get("LAS_JOB_ERROR", "")))'
        if ($classified.ExitCode -eq 0 -and $classified.Text) {
            $failureStage = $classified.Text.Trim()
        }
        if ($failureStage -eq 'C') {
            Fail-PipelineStage -Stage 'C' -Detail $(if ($attempt.error) { $attempt.error } else { 'ComfyUI rejected the adapted workflow.' })
        }
        $retryMb = Get-RetryReserve -ErrorText $attempt.error
        if ($null -eq $retryMb) {
            Fail-PipelineStage -Stage 'D' -Detail $(if ($attempt.error) { $attempt.error } else { 'The image job failed.' })
        }
        Write-InstallLog "CUDA out of memory. Stopping the managed ComfyUI process and retrying once with reserve VRAM $retryMb MiB."
        if (-not (Stop-ProductionGateway -IncludeComfyUI)) {
            Fail-PipelineStage -Stage 'D' -Detail 'The managed ComfyUI process could not be stopped after CUDA out of memory.'
        }
        $oomLive = Test-HttpStatus -Url "http://127.0.0.1:$($script:ApiPort)/health/live" -TimeoutSeconds 3
        if ($oomLive -eq 200) {
            Fail-PipelineStage -Stage 'D' -Detail 'The gateway was still running after the out-of-memory stop.'
        }
        if (-not (Start-ProductionGateway -Reserve $retryMb)) {
            Fail-PipelineStage -Stage 'D' -Detail 'The gateway did not restart with the reserve-vram profile.'
        }
        $script:LaunchProfile = "reserve-vram:$retryMb"
        $attempt = Invoke-ImageAttempt
        if ($attempt.status -ne 'succeeded') {
            Add-ErrorLog $attempt.error
            Fail-PipelineStage -Stage 'D' -Detail $(if ($attempt.error) { $attempt.error } else { 'The reserve-vram retry failed.' })
        }
    }
    $imageIdle = Wait-SchedulerIdle -Offset ([int64]$attempt.logOffset) -TimeoutSeconds 30
    if (-not $imageIdle.Ok) {
        Fail-PipelineStage -Stage 'D' -Detail "The image job finished but the gateway log did not show scheduler state IDLE. Last state: $($imageIdle.State)."
    }
    $script:Report.image_scheduler_succeeded = $true
    $script:Report.scheduler_idle_after_image = $true
    $imagePath = Join-Path $script:ReportDir 'generated-image.png'
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$($script:ApiPort)/v1/jobs/$($attempt.id)/download" -OutFile $imagePath -UseBasicParsing -TimeoutSec 60 | Out-Null
    } catch {
        Fail-PipelineStage -Stage 'D' -Detail "The gateway did not return the image. $($_.Exception.Message)"
    }
    $imageItem = Get-Item -LiteralPath $imagePath
    if ($imageItem.Length -le 0) {
        Fail-PipelineStage -Stage 'D' -Detail 'The downloaded image file is empty.'
    }
    $script:Report.image_generated = $true
    $env:LAS_IMAGE = $imagePath
    $dimensions = Invoke-PipelinePython -Code 'import os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import png_dimensions; width, height = png_dimensions(Path(os.environ["LAS_IMAGE"]).read_bytes()); print(str(width) + " " + str(height))'
    if ($dimensions.ExitCode -ne 0) {
        Fail-PipelineStage -Stage 'D' -Detail $dimensions.Text
    }
    $parts = @($dimensions.Text.Split(' '))
    if ($parts.Count -ne 2 -or [int]$parts[0] -ne $Width -or [int]$parts[1] -ne $Height) {
        Fail-PipelineStage -Stage 'D' -Detail "The PNG is $($dimensions.Text), expected $Width x $Height."
    }
    $script:Report.dimensions_verified = $true
    Set-StageResult -Name 'D' -Status 'pass'
    if (-not $script:Report.comfyui_checked) {
        Invoke-ObjectInfoCheck -ManifestPath $candidate
    }
    Save-ComfySystemStats | Out-Null
    Update-OffloadObservation -LogText ''

    Write-InstallLog 'Stage E: GPU samples.'
    Set-StageResult -Name 'E' -Status 'running'
    if ((Get-CsvDataRows) -le $script:SamplesBeforeImage) {
        Fail-PipelineStage -Stage 'E' -Detail 'nvidia-smi did not return a sample during image generation. Missing counters were not invented.'
    }
    Set-StageResult -Name 'E' -Status 'pass'
    Write-InstallLog 'Stage E passed. Peak VRAM is recorded and is not a claim that the model fits fully in VRAM.'

    Write-InstallLog 'Stage F: switch the GPU scheduler back to Ollama through the gateway.'
    Set-StageResult -Name 'F' -Status 'running'
    $chat = Invoke-GatewayChat -ContentText 'Reply with the single word again.'
    Add-GpuSample
    if (-not $chat.Ok -or [string]::IsNullOrWhiteSpace($chat.Text)) {
        Fail-PipelineStage -Stage 'F' -Detail $(if ($chat.Text) { [string]$chat.Text } else { 'The Ollama chat through the gateway was empty.' })
    }
    $idleAfter = Wait-SchedulerIdle -Offset ([int64]$chat.LogOffset) -TimeoutSeconds 30
    if (-not $idleAfter.Ok) {
        Fail-PipelineStage -Stage 'F' -Detail "The gateway log did not show scheduler state IDLE after the switch back to Ollama. Last state: $($idleAfter.State)."
    }
    if ($script:Overlap) {
        Fail-PipelineStage -Stage 'F' -Detail 'ComfyUI and Ollama were both active in one sample.'
    }
    if (-not (Test-OllamaResident)) {
        Fail-PipelineStage -Stage 'F' -Detail 'Ollama /api/ps does not show qwen3.5:4b-ctx8192 after the chat.'
    }
    $queuePath = Join-Path $script:ReportDir 'comfy-queue.json'
    $queueText = ''
    try {
        Invoke-WebRequest -Uri 'http://127.0.0.1:8188/queue' -OutFile $queuePath -UseBasicParsing -TimeoutSec 15 | Out-Null
        $queueText = Read-FileFromOffset -Path $queuePath -Offset 0
    } catch {
        Add-ErrorLog "ComfyUI /queue was not readable after the chat. $($_.Exception.Message)"
    }
    Save-ComfySystemStats | Out-Null
    $releaseLog = Join-Path $script:ReportDir 'release-log.txt'
    Write-Utf8File -Path $releaseLog -Text ([string]$idleAfter.Text)
    $env:LAS_QUEUE = $queuePath
    $env:LAS_STATS = Join-Path $script:ReportDir 'system-stats.json'
    $env:LAS_RELEASE_LOG = $releaseLog
    $release = Invoke-PipelinePython -Code @'
import json, os
from pathlib import Path
from local_ai_switchboard.diagnostics.image_pipeline import release_observation
queue_path = Path(os.environ["LAS_QUEUE"])
stats_path = Path(os.environ["LAS_STATS"])
queue = json.loads(queue_path.read_text(encoding="utf-8-sig")) if queue_path.is_file() else None
stats = json.loads(stats_path.read_text(encoding="utf-8-sig")) if stats_path.is_file() else None
log_text = Path(os.environ["LAS_RELEASE_LOG"]).read_text(encoding="utf-8")
print(json.dumps(release_observation(queue if isinstance(queue, dict) else None, stats if isinstance(stats, dict) else None, log_text)))
'@
    $released = $false
    if ($release.ExitCode -eq 0 -and $release.Text) {
        $releaseState = $release.Text | ConvertFrom-Json
        $released = [bool](Get-ObjectProperty -Object $releaseState -Name 'confirmed')
    }
    if (-not $released) {
        Fail-PipelineStage -Stage 'F' -Detail 'ComfyUI resource release was not confirmed by the gateway log or by ComfyUI memory counters.'
    }
    Update-OffloadObservation -LogText ([string]$idleAfter.Text)
    $reply = [string]$chat.Text
    if ($reply.Length -gt 500) {
        $reply = $reply.Substring(0, 500)
    }
    $script:Report.ollama_response = $reply
    $script:Report.text_after_image = $true
    $script:Report.scheduler_idle_after_text = $true
    $script:Report.resources_released = $true
    $script:Report.transition = 'passed'
    Set-StageResult -Name 'F' -Status 'pass'
    Write-InstallLog 'Stage F passed. The scheduler returned to Ollama.'

    Write-InstallLog 'Recording the verified workflow.'
    Set-StageResult -Name 'record' -Status 'running'
    $env:LAS_REPORT_INPUT = Join-Path $script:ReportDir 'commit-check.json'
    $script:Report.transition = 'passed'
    $script:Report.failure_stage = ''
    $script:Report.blocked_reason = ''
    $script:Report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $env:LAS_REPORT_INPUT -Encoding UTF8
    $allowed = Invoke-PipelinePython -Code 'import json, os; from pathlib import Path; from local_ai_switchboard.diagnostics.image_pipeline import commit_ready; report = json.loads(Path(os.environ["LAS_REPORT_INPUT"]).read_text(encoding="utf-8-sig")); raise SystemExit(0 if commit_ready(report) else 1)'
    if ($allowed.ExitCode -ne 0) {
        Fail-PipelineStage -Stage 'record' -Detail 'The run is not complete, so the workflow was not registered.'
    }
    $liveManifest = Join-Path $script:WorkflowsDir 'flux2-klein-t2i.json'
    $hash = (Get-FileHash -LiteralPath $liveManifest -Algorithm SHA256).Hash.ToLowerInvariant()
    $recordPath = Join-Path $InstallRoot 'config\image-pipeline.json'
    $previousPath = Join-Path $script:ReportDir 'previous-image-pipeline.json'
    if (Test-Path -LiteralPath $recordPath) {
        Copy-Item -LiteralPath $recordPath -Destination $previousPath -Force
        $script:HadPreviousRecord = $true
    }
    $details = @{
        comfyui_version        = $(if ($script:Report.comfyui_version_observed) { [string]$script:Report.comfyui_version_observed } else { [string]$script:Report.comfyui_pin })
        comfyui_version_source = $(if ($script:Report.comfyui_version_observed) { 'system_stats' } else { 'pin' })
        gpu                    = [string]$script:Report.hardware_gpu
        driver                 = [string]$script:Report.hardware_driver
        vram_mib               = [int]$script:Report.hardware_vram_mib
        width                  = $Width
        height                 = $Height
        seed                   = $Seed
        steps                  = 4
        cfg                    = 1.0
        sampler                = 'euler'
        batch_size             = 1
        models                 = @($script:ModelRecords)
        workflow_parameters    = @{
            workflow_id = 'flux2-klein-t2i'
            width       = $Width
            height      = $Height
            steps       = 4
            cfg         = 1.0
            sampler     = 'euler'
            batch_size  = 1
            seed        = $Seed
        }
    }
    $detailsPath = Join-Path $script:ReportDir 'verification-details.json'
    $details | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $detailsPath -Encoding UTF8
    $env:LAS_VERIFY_HASH = $hash
    $env:LAS_VERIFY_PROFILE = $script:LaunchProfile
    $env:LAS_VERIFY_REPORT = $script:ReportDir
    $env:LAS_VERIFY_OUT = $recordPath
    $env:LAS_VERIFY_DETAILS = $detailsPath
    $record = Invoke-PipelinePython -Code @'
import json, os
from pathlib import Path
from local_ai_switchboard.diagnostics.image_pipeline import commit_verification
details = json.loads(Path(os.environ["LAS_VERIFY_DETAILS"]).read_text(encoding="utf-8-sig"))
document = commit_verification(
    passed=True,
    manifest_sha256=os.environ["LAS_VERIFY_HASH"],
    launch_profile=os.environ["LAS_VERIFY_PROFILE"],
    report_dir=os.environ["LAS_VERIFY_REPORT"],
    details=details,
)
Path(os.environ["LAS_VERIFY_OUT"]).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
'@
    if ($record.ExitCode -ne 0) {
        Fail-PipelineStage -Stage 'record' -Detail $record.Text
    }
    $script:RecordWritten = $true
    $liveAfter = Test-HttpStatus -Url "http://127.0.0.1:$($script:ApiPort)/health/live" -TimeoutSeconds 5
    $readyAfter = Test-HttpStatus -Url "http://127.0.0.1:$($script:ApiPort)/health/ready" -TimeoutSeconds 5
    if ($liveAfter -ne 200 -or $readyAfter -ne 200) {
        Fail-PipelineStage -Stage 'record' -Detail "Gateway readiness after verification was live $liveAfter and ready $readyAfter."
    }
    $doctorScript = Join-Path $PSScriptRoot 'doctor.ps1'
    $doctorPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $doctorText = & $doctorScript -InstallRoot $InstallRoot *>&1 | Out-String
    } finally {
        $ErrorActionPreference = $doctorPreference
    }
    $doctorPath = Join-Path $script:ReportDir 'doctor.txt'
    Write-Utf8File -Path $doctorPath -Text $doctorText
    if ($doctorText -notmatch 'Image workflow: verified on this machine') {
        Fail-PipelineStage -Stage 'record' -Detail 'doctor.ps1 did not report the workflow as verified on this machine. The previous verification record was kept.'
    }
    $script:Report.doctor_verified = $true
    $script:Report.workflow_enabled = $true
    Set-StageResult -Name 'record' -Status 'pass'
    $script:Succeeded = $true
    Write-InstallLog 'flux2-klein-t2i is verified on this machine. The report does not claim that the model fits fully in VRAM. This run does not show that the Gateway is reachable from a Mac.'
} catch {
    if (-not $script:FailureStage) {
        $running = Get-RunningStage
        if ($running) {
            $script:FailureStage = $running
            Set-StageResult -Name $running -Status 'fail'
        } else {
            $script:FailureStage = 'unhandled'
        }
        $script:FailureDetail = Get-ErrorDetail -ErrorRecord $_
        Add-ErrorLog $script:FailureDetail
        if ($_.ScriptStackTrace) {
            Add-ErrorLog ([string]$_.ScriptStackTrace)
        }
        Write-InstallLog $script:FailureDetail 'ERROR'
    }
    $script:Report.workflow_enabled = $false
    $script:Report.doctor_verified = $false
    if ($script:RecordWritten) {
        Write-InstallLog 'Restoring the previous image-pipeline.json.'
        Restore-VerificationRecord
        $script:RecordWritten = $false
    }
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
    try {
        Publish-LatestReport
    } catch {
        Add-ErrorLog "The latest report copy could not be written. $($_.Exception.Message)"
        Write-InstallLog "The latest report copy could not be written. $($_.Exception.Message)" 'ERROR'
    }
}

Write-InstallLog "Report: $($script:ReportDir)"
Write-InstallLog "Result: $($script:Result)"
if ($script:Result -eq 'PASS' -and $script:Succeeded) {
    exit 0
}
$summaryWord = 'failed'
if ($script:Result -eq 'PARTIAL') {
    $summaryWord = 'partial'
}
$summary = "Verification is $summaryWord at stage $($script:FailureStage)."
if ($script:FailureDetail) {
    $summary = $summary + ' ' + $script:FailureDetail
}
$summary = $summary + ' The workflow was not newly verified.'
Write-InstallLog $summary 'ERROR'
if ($script:Result -eq 'PARTIAL') {
    exit 2
}
exit 1
