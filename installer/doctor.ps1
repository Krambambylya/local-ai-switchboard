#Requires -Version 5.1
<#
.SYNOPSIS
    Report gateway, Ollama, ComfyUI, firewall, and startup checks.

.DESCRIPTION
    Gateway, Ollama, and ComfyUI readiness are separate from image workflow
    availability. A missing workflow does not mark the gateway unhealthy.
    Image generation is ready only when a manifest is enabled and
    validate_manifest accepts it. That check does not measure VRAM.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot,
    [switch]$AllowOlderWindows
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    Write-Error 'The doctor script runs on Windows.'
    exit 1
}
if (-not $InstallRoot) {
    $InstallRoot = Join-Path $env:USERPROFILE 'AI\local-ai-switchboard'
}

. (Join-Path $PSScriptRoot 'Common.ps1')
$logDir = Join-Path $InstallRoot 'logs'
if (Test-Path -LiteralPath $logDir) {
    $script:LogPath = Join-Path $logDir 'service.log'
}

$script:failed = $false
$script:gatewayReady = $true
$script:ollamaReady = $true
$script:comfyReady = $true
$script:comfyLabel = 'ready'
$script:imageWorkflow = 'unavailable'
$script:imageDetail = 'No enabled workflow manifest is registered.'

function Add-DoctorCheck {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Detail,
        [ValidateSet('gateway', 'ollama', 'comfyui', 'report')]
        [string]$Group = 'report'
    )
    $level = 'INFO'
    if (-not $Ok) {
        $script:failed = $true
        $level = 'ERROR'
        if ($Group -eq 'gateway') {
            $script:gatewayReady = $false
        } elseif ($Group -eq 'ollama') {
            $script:ollamaReady = $false
        } elseif ($Group -eq 'comfyui') {
            $script:comfyReady = $false
        }
    }
    Write-InstallLog "$Name : $Detail" $level
}

function Test-ImagePipelineRecord {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$WorkflowId,
        [Parameter(Mandatory = $true)][string]$ManifestPath
    )
    $recordPath = Join-Path $InstallRoot 'config\image-pipeline.json'
    if (-not (Test-Path -LiteralPath $recordPath)) {
        return $false
    }
    try {
        $record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $false
    }
    if ([string]$record.status -ne 'passed' -or [string]$record.workflow_id -ne $WorkflowId) {
        return $false
    }
    if ($record.vram_fit_claimed -eq $true) {
        return $false
    }
    $hash = (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    return [string]$record.manifest_sha256 -eq $hash
}

function Test-MultimodalRecord {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$ManifestPath
    )
    $recordPath = Join-Path $InstallRoot 'config\multimodal-pipeline.json'
    if (-not (Test-Path -LiteralPath $recordPath)) {
        return $false
    }
    try {
        $record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $false
    }
    if ([string]$record.status -ne 'passed' -or [string]$record.workflow_id -ne 'flux2-klein-edit') {
        return $false
    }
    if ($record.vram_fit_claimed -eq $true) {
        return $false
    }
    $hash = (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    return [string]$record.manifest_sha256 -eq $hash
}

function Update-ImageWorkflowStatus {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$WorkflowsDir,
        [string]$Python,
        [Parameter(Mandatory = $true)][string]$ProfileName
    )
    if ($ProfileName -ne 'Design') {
        $script:imageWorkflow = 'not in the Basic profile'
        $script:imageDetail = 'The Basic profile does not register an image workflow.'
        return
    }
    if (-not (Test-Path -LiteralPath $WorkflowsDir)) {
        $script:imageWorkflow = 'unavailable'
        $script:imageDetail = "Workflow directory is missing: $WorkflowsDir"
        return
    }
    $files = @(Get-ChildItem -LiteralPath $WorkflowsDir -Filter *.json -ErrorAction SilentlyContinue)
    if ($files.Count -eq 0) {
        $script:imageWorkflow = 'unavailable'
        $script:imageDetail = 'The workflow directory has no manifest.'
        return
    }
    $reason = 'No enabled workflow manifest is registered.'
    $verifiedImage = $null
    $pendingImage = $null
    $editFile = $null
    foreach ($file in $files) {
        try {
            $payload = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            $reason = "$($file.Name) is not valid JSON."
            continue
        }
        if (Test-RawComfyApiExport -Payload $payload) {
            $reason = "$($file.Name) is a raw ComfyUI API export, not a gateway manifest."
            continue
        }
        if (-not $Python -or -not (Test-Path -LiteralPath $Python)) {
            $reason = 'The gateway interpreter is not available to validate a workflow manifest.'
            continue
        }
        $state = Get-WorkflowManifestState -Python $Python -ManifestPath $file.FullName
        if ($state.Ok -and $state.State -eq 'enabled' -and $state.Graph -eq 'yes') {
            if ($state.Id -eq 'flux2-klein-edit') {
                $editFile = $file.FullName
                continue
            }
            if (Test-ImagePipelineRecord -InstallRoot $InstallRoot -WorkflowId $state.Id -ManifestPath $file.FullName) {
                $verifiedImage = $state
            } elseif ($null -eq $pendingImage) {
                $pendingImage = $state
            }
            continue
        }
        if (-not $state.Ok) {
            $reason = "$($file.Name) was rejected by validate_manifest."
        }
    }
    $editNote = ''
    if ($editFile) {
        if (Test-MultimodalRecord -InstallRoot $InstallRoot -ManifestPath $editFile) {
            $editNote = ' flux2-klein-edit passed verify-multimodal-pipeline.ps1.'
        } else {
            $editNote = ' flux2-klein-edit is registered and has not passed verify-multimodal-pipeline.ps1.'
        }
    }
    if ($null -ne $verifiedImage) {
        $script:imageWorkflow = 'verified on this machine'
        $script:imageDetail = "$($verifiedImage.Id) passed verify-image-pipeline.ps1. Peak VRAM in that report is a measurement and does not claim the model fits fully in VRAM.$editNote"
        return
    }
    if ($null -ne $pendingImage) {
        $script:imageWorkflow = 'registered, not tested'
        $script:imageDetail = "$($pendingImage.Id) passed validate_manifest and has not been tested on this machine.$editNote"
        return
    }
    if ($editFile) {
        $script:imageWorkflow = 'registered, not tested'
        $script:imageDetail = $editNote.Trim()
        return
    }
    $script:imageWorkflow = 'unavailable'
    $script:imageDetail = $reason
}

$manifestPath = Join-Path $InstallRoot 'config\installed.json'
$manifest = $null
if (-not (Test-Path -LiteralPath $manifestPath)) {
    Add-DoctorCheck -Name 'manifest' -Ok $false -Detail 'config\installed.json is missing.' -Group report
} else {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Add-DoctorCheck -Name 'manifest' -Ok $true -Detail "profile $($manifest.profile)" -Group report
}

$os = Get-CimInstance -ClassName Win32_OperatingSystem
$buildOk = [int]$os.BuildNumber -ge 22000
if ((-not $buildOk) -and $AllowOlderWindows) {
    Write-InstallLog "windows : $($os.Caption) build $($os.BuildNumber). -AllowOlderWindows was set." 'WARN'
} else {
    Add-DoctorCheck -Name 'windows' -Ok $buildOk -Detail "$($os.Caption) build $($os.BuildNumber)" -Group report
}

$gpu = Get-NvidiaReport
Add-DoctorCheck -Name 'nvidia-smi' -Ok $gpu.Available -Detail $gpu.Detail -Group report

$python = Find-Python312 -InstallRoot $InstallRoot
$pythonOk = $null -ne $python -and $python.Executable -notlike '*python_embeded*'
$pythonDetail = 'Python 3.12 or newer was not found.'
if ($pythonOk) {
    $pythonDetail = "$($python.Version) $($python.Executable)"
}
Add-DoctorCheck -Name 'python' -Ok $pythonOk -Detail $pythonDetail -Group report

$ollama = Find-OllamaExecutable
if ($null -eq $ollama) {
    Add-DoctorCheck -Name 'ollama' -Ok $false -Detail 'ollama --version failed.' -Group ollama
} else {
    Add-DoctorCheck -Name 'ollama' -Ok $true -Detail "$($ollama.Version) $($ollama.Executable)" -Group ollama
    $listeners = @(Get-ListeningEntries -Port 11434)
    $loopback = $true
    foreach ($entry in $listeners) {
        if (-not (Test-LoopbackAddress -Address ([string]$entry.LocalAddress))) {
            $loopback = $false
        }
    }
    $null = Wait-OllamaApi -OllamaExecutable $ollama.Executable
    $http = Test-HttpStatus -Url 'http://127.0.0.1:11434/api/version' -TimeoutSeconds 5
    Add-DoctorCheck -Name 'ollama-api' -Ok ($http -eq 200 -and $loopback) -Detail "HTTP $http loopback=$loopback" -Group ollama
    $show = Invoke-CapturedCommand -FilePath $ollama.Executable -ArgumentList @('show', 'qwen3.5:4b-ctx8192')
    $profileOk = $show.ExitCode -eq 0 -and $show.Text -match 'num_ctx\s+8192'
    Add-DoctorCheck -Name 'ollama-profile' -Ok $profileOk -Detail 'qwen3.5:4b-ctx8192' -Group ollama
}

$profileName = 'Design'
if ($manifest -and $manifest.profile) {
    $profileName = [string]$manifest.profile
}
if ($profileName -eq 'Design') {
    $portable = Find-ComfyPortableRoot -Path (Join-Path $InstallRoot 'ComfyUI_windows_portable')
    Add-DoctorCheck -Name 'comfyui-files' -Ok ($null -ne $portable) -Detail $(if ($portable) { $portable } else { 'python_embeded\python.exe or ComfyUI\main.py is missing.' }) -Group comfyui
    $comfyListeners = @(Get-ListeningEntries -Port 8188)
    if ($comfyListeners.Count -eq 0) {
        Add-DoctorCheck -Name 'comfyui-bind' -Ok $true -Detail 'ComfyUI is not listening. The gateway starts it for an image job.' -Group comfyui
    } else {
        $comfyLoopback = $true
        foreach ($entry in $comfyListeners) {
            if (-not (Test-LoopbackAddress -Address ([string]$entry.LocalAddress))) {
                $comfyLoopback = $false
            }
        }
        Add-DoctorCheck -Name 'comfyui-bind' -Ok $comfyLoopback -Detail "listeners=$($comfyListeners.Count) loopback=$comfyLoopback" -Group comfyui
    }
    if (-not $script:comfyReady) {
        $script:comfyLabel = 'not ready'
    }
} else {
    $script:comfyLabel = 'not in the Basic profile'
}

$venvPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    $imported = Invoke-CapturedCommand -FilePath $venvPython -ArgumentList @('-c', "import importlib.metadata as metadata; print(metadata.version('local-ai-switchboard'))")
    $importedDetail = [string]@($imported.Text -split '\r?\n' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })[-1]
    Add-DoctorCheck -Name 'gateway-package' -Ok ($imported.ExitCode -eq 0) -Detail $importedDetail.Trim() -Group gateway
} else {
    Add-DoctorCheck -Name 'gateway-package' -Ok $false -Detail 'The gateway virtual environment is missing.' -Group gateway
}

$apiPort = 8787
$workflowsDir = Join-Path $InstallRoot 'workflows'
$envFile = Join-Path $InstallRoot '.env'
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in @(Get-Content -LiteralPath $envFile)) {
        if ($line -like 'LAS_API_PORT=*') {
            $parsedPort = 0
            if ([int]::TryParse($line.Split('=', 2)[1], [ref]$parsedPort)) {
                $apiPort = $parsedPort
            }
        }
        if ($line -like 'LAS_WORKFLOWS_DIR=*') {
            $workflowsDir = $line.Split('=', 2)[1]
        }
        if ($line -like 'LAS_OLLAMA_URL=*' -and $line -notlike 'LAS_OLLAMA_URL=http://127.0.0.1:11434*') {
            Add-DoctorCheck -Name 'ollama-url' -Ok $false -Detail 'LAS_OLLAMA_URL is not the loopback pin.' -Group ollama
        }
        if ($line -like 'LAS_COMFYUI_URL=*' -and $line -notlike 'LAS_COMFYUI_URL=http://127.0.0.1:8188*') {
            Add-DoctorCheck -Name 'comfyui-url' -Ok $false -Detail 'LAS_COMFYUI_URL is not the loopback pin.' -Group comfyui
        }
    }
} else {
    Add-DoctorCheck -Name 'env' -Ok $false -Detail '.env is missing.' -Group gateway
}

$live = Test-HttpStatus -Url "http://127.0.0.1:$apiPort/health/live" -TimeoutSeconds 5
$ready = Test-HttpStatus -Url "http://127.0.0.1:$apiPort/health/ready" -TimeoutSeconds 5
Add-DoctorCheck -Name 'gateway-health' -Ok ($live -eq 200 -and $ready -eq 200) -Detail "live $live ready $ready" -Group gateway

$firewall = Get-GatewayFirewallState -ApiPort $apiPort
Add-DoctorCheck -Name 'firewall' -Ok $firewall.Verified -Detail $firewall.Detail -Group gateway

$task = Get-GatewayTaskState -InstallRoot $InstallRoot
Add-DoctorCheck -Name 'startup' -Ok $task.Verified -Detail $task.Detail -Group gateway

$db = Join-Path $InstallRoot 'data\switchboard.db'
if (Test-Path -LiteralPath $db) {
    try {
        $headerBytes = Read-FilePrefix -Path $db -Count 16
        $header = [System.Text.Encoding]::ASCII.GetString($headerBytes, 0, [Math]::Min(15, $headerBytes.Length))
        Add-DoctorCheck -Name 'sqlite' -Ok ($header -eq 'SQLite format 3') -Detail $db -Group gateway
    } catch {
        Add-DoctorCheck -Name 'sqlite' -Ok $false -Detail $_.Exception.Message -Group gateway
    }
} else {
    Add-DoctorCheck -Name 'sqlite' -Ok $false -Detail 'data\switchboard.db is missing.' -Group gateway
}

$pythonForWorkflow = ''
if (Test-Path -LiteralPath $venvPython) {
    $pythonForWorkflow = $venvPython
}
Update-ImageWorkflowStatus -InstallRoot $InstallRoot -WorkflowsDir $workflowsDir -Python $pythonForWorkflow -ProfileName $profileName

if ($profileName -eq 'Design' -and -not $script:comfyReady) {
    $script:comfyLabel = 'not ready'
}
$gatewayLabel = if ($script:gatewayReady) { 'ready' } else { 'not ready' }
$ollamaLabel = if ($script:ollamaReady) { 'ready' } else { 'not ready' }
Write-InstallLog "Gateway operational: $gatewayLabel"
Write-InstallLog "Ollama operational: $ollamaLabel"
Write-InstallLog "ComfyUI operational: $($script:comfyLabel)"
Write-InstallLog "Image workflow: $($script:imageWorkflow). $($script:imageDetail)"
if ($script:imageWorkflow -eq 'verified on this machine') {
    Write-InstallLog 'Image generation: verified on this machine'
    Write-InstallLog 'The smoke test completed on this PC. The report does not claim that the model fits fully in VRAM.'
} elseif ($script:imageWorkflow -eq 'registered, not tested') {
    Write-InstallLog 'Image generation: ready'
    Write-InstallLog 'The registered manifest passed validation. This check does not measure VRAM and does not prove a generation on an RTX 4060.'
} else {
    Write-InstallLog 'Image generation: not ready'
}
if ($script:imageWorkflow -eq 'unavailable' -or $script:imageWorkflow -eq 'not in the Basic profile') {
    Write-InstallLog 'A missing image workflow does not change gateway readiness.'
}

$addresses = @(Get-LanIPv4Addresses)
if ($addresses.Count -eq 0) {
    Write-InstallLog "Gateway bind port $apiPort. No LAN IPv4 address was found."
} else {
    foreach ($address in $addresses) {
        Write-InstallLog "Gateway URL: http://${address}:$apiPort/v1"
    }
}
Write-InstallLog 'The gateway does not use a password or API token.'
if ($script:failed) {
    exit 1
}
exit 0
