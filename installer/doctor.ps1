#Requires -Version 5.1
<#
.SYNOPSIS
    Report gateway, Ollama, ComfyUI, firewall, and startup checks.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot
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

$failed = $false
function Add-DoctorCheck {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Detail
    )
    $level = 'INFO'
    if (-not $Ok) {
        $script:failed = $true
        $level = 'ERROR'
    }
    Write-InstallLog "$Name : $Detail" $level
}

$manifestPath = Join-Path $InstallRoot 'config\installed.json'
$manifest = $null
if (-not (Test-Path -LiteralPath $manifestPath)) {
    Add-DoctorCheck -Name 'manifest' -Ok $false -Detail 'config\installed.json is missing.'
} else {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Add-DoctorCheck -Name 'manifest' -Ok $true -Detail "profile $($manifest.profile)"
}

$os = Get-CimInstance -ClassName Win32_OperatingSystem
$buildOk = [int]$os.BuildNumber -ge 22000
Add-DoctorCheck -Name 'windows' -Ok $buildOk -Detail "$($os.Caption) build $($os.BuildNumber)"

$gpu = Get-NvidiaReport
Add-DoctorCheck -Name 'nvidia-smi' -Ok $gpu.Available -Detail $gpu.Detail

$python = Find-Python312
$pythonOk = $null -ne $python -and $python.Executable -notlike '*python_embeded*'
$pythonDetail = 'Python 3.12 or newer was not found.'
if ($pythonOk) {
    $pythonDetail = "$($python.Version) $($python.Executable)"
}
Add-DoctorCheck -Name 'python' -Ok $pythonOk -Detail $pythonDetail

$ollama = Find-OllamaExecutable
if ($null -eq $ollama) {
    Add-DoctorCheck -Name 'ollama' -Ok $false -Detail 'ollama --version failed.'
} else {
    Add-DoctorCheck -Name 'ollama' -Ok $true -Detail "$($ollama.Version) $($ollama.Executable)"
    $listeners = @(Get-ListeningEntries -Port 11434)
    $loopback = $true
    foreach ($entry in $listeners) {
        if (-not (Test-LoopbackAddress -Address ([string]$entry.LocalAddress))) {
            $loopback = $false
        }
    }
    $http = Test-HttpStatus -Url 'http://127.0.0.1:11434/api/version' -TimeoutSeconds 5
    Add-DoctorCheck -Name 'ollama-api' -Ok ($http -eq 200 -and $loopback) -Detail "HTTP $http loopback=$loopback"
    $show = @(& $ollama.Executable show 'qwen3.5:4b-ctx8192' 2>&1) -join ' '
    $profileOk = $LASTEXITCODE -eq 0 -and $show -match 'num_ctx\s+8192'
    Add-DoctorCheck -Name 'ollama-profile' -Ok $profileOk -Detail 'qwen3.5:4b-ctx8192'
}

$profileName = 'Design'
if ($manifest -and $manifest.profile) {
    $profileName = [string]$manifest.profile
}
if ($profileName -eq 'Design') {
    $portable = Find-ComfyPortableRoot -Path (Join-Path $InstallRoot 'ComfyUI_windows_portable')
    Add-DoctorCheck -Name 'comfyui-files' -Ok ($null -ne $portable) -Detail $(if ($portable) { $portable } else { 'python_embeded\python.exe or ComfyUI\main.py is missing.' })
    $comfyListeners = @(Get-ListeningEntries -Port 8188)
    if ($comfyListeners.Count -eq 0) {
        Add-DoctorCheck -Name 'comfyui-bind' -Ok $true -Detail 'ComfyUI is not listening. The gateway starts it for an image job.'
    } else {
        $comfyLoopback = $true
        foreach ($entry in $comfyListeners) {
            if (-not (Test-LoopbackAddress -Address ([string]$entry.LocalAddress))) {
                $comfyLoopback = $false
            }
        }
        Add-DoctorCheck -Name 'comfyui-bind' -Ok $comfyLoopback -Detail "listeners=$($comfyListeners.Count) loopback=$comfyLoopback"
    }
}

$venvPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    $imported = & $venvPython -c 'import importlib.metadata as metadata; print(metadata.version("local-ai-switchboard"))'
    Add-DoctorCheck -Name 'gateway-package' -Ok ($LASTEXITCODE -eq 0) -Detail ((@($imported) -join ' ').Trim())
} else {
    Add-DoctorCheck -Name 'gateway-package' -Ok $false -Detail 'The gateway virtual environment is missing.'
}

$apiPort = 8787
$envFile = Join-Path $InstallRoot '.env'
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in @(Get-Content -LiteralPath $envFile)) {
        if ($line -like 'LAS_API_PORT=*') {
            $parsedPort = 0
            if ([int]::TryParse($line.Split('=', 2)[1], [ref]$parsedPort)) {
                $apiPort = $parsedPort
            }
        }
        if ($line -like 'LAS_OLLAMA_URL=*' -and $line -notlike 'LAS_OLLAMA_URL=http://127.0.0.1:11434*') {
            Add-DoctorCheck -Name 'ollama-url' -Ok $false -Detail 'LAS_OLLAMA_URL is not the loopback pin.'
        }
        if ($line -like 'LAS_COMFYUI_URL=*' -and $line -notlike 'LAS_COMFYUI_URL=http://127.0.0.1:8188*') {
            Add-DoctorCheck -Name 'comfyui-url' -Ok $false -Detail 'LAS_COMFYUI_URL is not the loopback pin.'
        }
    }
} else {
    Add-DoctorCheck -Name 'env' -Ok $false -Detail '.env is missing.'
}

$live = Test-HttpStatus -Url "http://127.0.0.1:$apiPort/health/live" -TimeoutSeconds 5
$ready = Test-HttpStatus -Url "http://127.0.0.1:$apiPort/health/ready" -TimeoutSeconds 5
Add-DoctorCheck -Name 'gateway-health' -Ok ($live -eq 200 -and $ready -eq 200) -Detail "live $live ready $ready"

$firewall = Get-GatewayFirewallState -ApiPort $apiPort
Add-DoctorCheck -Name 'firewall' -Ok $firewall.Verified -Detail $firewall.Detail

$task = Get-GatewayTaskState -InstallRoot $InstallRoot
Add-DoctorCheck -Name 'startup' -Ok $task.Verified -Detail $task.Detail

$tokenStatus = Get-TokenProtectionStatus -LiteralPath (Join-Path $InstallRoot 'secrets\token.txt')
Add-DoctorCheck -Name 'token' -Ok ($tokenStatus.Present -and $tokenStatus.Length -ge 16 -and $tokenStatus.AclOk) -Detail "present=$($tokenStatus.Present) length=$($tokenStatus.Length) acl=$($tokenStatus.AclOk)"

$db = Join-Path $InstallRoot 'data\switchboard.db'
if (Test-Path -LiteralPath $db) {
    $headerBytes = [System.IO.File]::ReadAllBytes($db)
    $header = [System.Text.Encoding]::ASCII.GetString($headerBytes, 0, [Math]::Min(15, $headerBytes.Length))
    Add-DoctorCheck -Name 'sqlite' -Ok ($header -eq 'SQLite format 3') -Detail $db
} else {
    Add-DoctorCheck -Name 'sqlite' -Ok $false -Detail 'data\switchboard.db is missing.'
}

$addresses = @(Get-LanIPv4Addresses)
if ($addresses.Count -eq 0) {
    Write-InstallLog "Gateway bind port $apiPort. No LAN IPv4 address was found."
} else {
    foreach ($address in $addresses) {
        Write-InstallLog "Gateway URL: http://${address}:$apiPort/v1"
    }
}
Write-InstallLog "The API token stays in $(Join-Path $InstallRoot 'secrets\mac-client.json')."
if ($failed) {
    exit 1
}
exit 0
