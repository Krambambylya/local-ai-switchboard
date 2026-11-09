#Requires -Version 5.1
<#
.SYNOPSIS
    Install the Local AI Switchboard on Windows.

.DESCRIPTION
    Configures a per-user gateway, Ollama, and, for the Design profile, ComfyUI
    Portable NVIDIA. -Root is an alias of -InstallRoot. -WorkflowFile accepts a
    gateway workflow manifest. It is validated with load_manifest and is not a
    raw ComfyUI API export. Downloaded installers are checked against pins.json
    and started from an argument list. This script does not execute a downloaded
    script. A rerun keeps an existing .env, portable tree, and models.
    The gateway does not use a password or API token.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('Basic', 'Design')]
    [string]$Profile = 'Design',
    [Alias('Root')]
    [string]$InstallRoot,
    [string]$SourceRoot,
    [string]$WorkflowFile,
    [int]$ApiPort = 8787,
    [switch]$Silent,
    [switch]$SkipFirewall,
    [switch]$AcceptUnchecksummedModels,
    [switch]$ForceConfig,
    [switch]$UpgradeOllama,
    [switch]$AllowOlderWindows,
    [switch]$AllowLowMemory,
    [switch]$FirewallOnly
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    Write-Error 'This installer runs on Windows.'
    exit 1
}

. (Join-Path $PSScriptRoot 'Common.ps1')

$DryRun = [bool]$WhatIfPreference
if (-not $SourceRoot) {
    $SourceRoot = Split-Path -Parent $PSScriptRoot
}
if (-not $InstallRoot) {
    $InstallRoot = Join-Path $env:USERPROFILE 'AI\local-ai-switchboard'
}
if (-not $ForceConfig) {
    $configuredPort = Get-ConfiguredApiPort -InstallRoot $InstallRoot -DefaultPort $ApiPort
    if ($configuredPort -ne $ApiPort) {
        Write-Host "The existing .env sets LAS_API_PORT $configuredPort. -ApiPort $ApiPort was not applied."
    }
    $ApiPort = $configuredPort
}

if ($FirewallOnly) {
    Initialize-InstallLog -Directory (Join-Path $InstallRoot 'logs') -DryRun:$DryRun
    $firewall = Install-GatewayFirewall -ApiPort $ApiPort -DryRun:$DryRun
    if ($firewall.Verified) { exit 0 }
    exit 1
}

if (-not $Silent -and -not $DryRun -and -not $PSBoundParameters.ContainsKey('Profile')) {
    Write-Host 'Design installs the gateway, Ollama, qwen3.5:4b, ComfyUI Portable NVIDIA, the checksummed FP8 Klein diffusion weights, the FP4 text encoder, and the VAE.'
    Write-Host 'Basic skips ComfyUI and the image model files.'
    $choice = Read-Host 'Profile [Design]'
    if ($choice -eq 'Basic') {
        $Profile = 'Basic'
    } elseif ($choice -ne '' -and $choice -ne 'Design') {
        Write-Error 'Profile must be Basic or Design.'
        exit 1
    }
}
if (-not $Silent -and -not $DryRun -and -not $PSBoundParameters.ContainsKey('InstallRoot')) {
    $entered = Read-Host "Install root [$InstallRoot]"
    if (-not [string]::IsNullOrWhiteSpace($entered)) {
        $InstallRoot = $entered
    }
}

$Pins = Get-InstallerPins
Initialize-InstallLog -Directory (Join-Path $InstallRoot 'logs') -DryRun:$DryRun
Write-InstallLog "Profile $Profile. Install root $InstallRoot. Source $SourceRoot."

function Save-StateManifest {
    param([hashtable]$Components)
    $pythonExecutable = ''
    if ($null -ne $script:ResolvedPythonRuntime -and $script:ResolvedPythonRuntime.Executable) {
        $pythonExecutable = [string]$script:ResolvedPythonRuntime.Executable
    }
    $manifest = [ordered]@{
        schema_version     = 1
        profile            = $Profile
        install_root       = $InstallRoot
        source_root        = $SourceRoot
        python_executable  = $pythonExecutable
        recorded_at        = (Get-Date).ToString('o')
        components         = $Components
    }
    Write-InstallManifest -Path (Join-Path $InstallRoot 'config\installed.json') -Manifest $manifest -DryRun:$DryRun
}

function Add-ComponentResult {
    param(
        [hashtable]$Components,
        [string]$Name,
        $Result
    )
    $Components[$Name] = @{
        verified = [bool]$Result.Verified
        detail   = [string]$Result.Detail
        facts    = $Result.Facts
    }
    Save-StateManifest -Components $Components
}

function Stop-ForStage {
    param(
        [hashtable]$Components,
        $Result
    )
    if ($Result.Verified) {
        return $false
    }
    $dryRunSkip = $false
    if ($DryRun) {
        if ($Result.Detail -eq 'dry run') {
            $dryRunSkip = $true
        } elseif ($Result.Facts.ContainsKey('Skipped') -and $Result.Facts.Skipped -eq 'dry-run') {
            $dryRunSkip = $true
        } elseif ($Result.Facts.ContainsKey('action') -and $Result.Facts.action -eq 'dry-run') {
            $dryRunSkip = $true
        }
    }
    if ($dryRunSkip) {
        return $false
    }
    Save-StateManifest -Components $Components
    Write-InstallLog $Result.Detail 'ERROR'
    Write-InstallLog 'Installation is incomplete. Completed pieces were left in place and a rerun can continue.' 'ERROR'
    exit 1
}

function Get-MachinePreflight {
    $errors = New-Object System.Collections.Generic.List[string]
    $warnings = New-Object System.Collections.Generic.List[string]
    $facts = @{}
    $architecture = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) {
        $architecture = $env:PROCESSOR_ARCHITEW6432
    }
    $facts.architecture = $architecture
    if ($architecture -ne 'AMD64') {
        $errors.Add("The processor architecture is $architecture. The pinned installers are Windows x64.")
    }
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    $facts.windows = [string]$os.Caption
    $facts.build = [int]$os.BuildNumber
    Write-InstallLog "Windows: $($facts.windows) build $($facts.build)."
    if ([int]$os.BuildNumber -lt [int]$Pins.windows.minimum_build) {
        if ($AllowOlderWindows) {
            $warnings.Add("Windows build $($facts.build) is older than Windows 11. -AllowOlderWindows was set.")
        } else {
            $errors.Add("Windows build $($facts.build) is older than Windows 11. Pass -AllowOlderWindows to continue.")
        }
    }
    $computer = Get-CimInstance -ClassName Win32_ComputerSystem
    $ram = [int64]$computer.TotalPhysicalMemory
    $facts.ram_bytes = $ram
    Write-InstallLog "RAM: $ram bytes."
    if ($ram -lt [int64]$Pins.windows.minimum_ram_bytes) {
        if ($AllowLowMemory) {
            $warnings.Add("Installed RAM is below 16 GiB. -AllowLowMemory was set.")
        } else {
            $errors.Add('Installed RAM is below 16 GiB.')
        }
    } elseif ($ram -lt [int64]$Pins.windows.target_ram_bytes) {
        $warnings.Add('Installed RAM is below the 30 GiB warning line for a 32 GiB machine.')
    }
    $gpu = Get-NvidiaReport
    $facts.gpu = $gpu.Detail
    $facts.driver = $gpu.Driver
    $facts.vram_mib = $gpu.MemoryMiB
    Write-InstallLog "GPU: $($gpu.Detail)"
    if (-not $gpu.Available) {
        if ($Profile -eq 'Design') {
            $errors.Add('nvidia-smi did not report a GPU. The Design profile stops before the NVIDIA portable download.')
        } else {
            $warnings.Add('nvidia-smi did not report a GPU. GPU monitoring stays unverified.')
        }
    } else {
        if ($gpu.MemoryMiB -lt [int]$Pins.windows.vram_warn_mib) {
            $warnings.Add("Reported VRAM is $($gpu.MemoryMiB) MiB.")
        }
        if ($gpu.Name -notlike '*4060*') {
            $warnings.Add("Detected GPU name: $($gpu.Name).")
        }
    }
    $free = Get-FreeBytesForPath -Path $InstallRoot
    $required = [int64]$Pins.disk.basic_free_bytes
    if ($Profile -eq 'Design') {
        $required = [int64]$Pins.disk.design_free_bytes
    }
    $facts.free_bytes = $free
    $facts.required_free_bytes = $required
    Write-InstallLog "Free disk space: $free bytes. Profile minimum: $required bytes."
    if ($free -lt $required) {
        $errors.Add("Free disk space is $free bytes. This profile needs $required bytes.")
    }
    $python = Find-Python312 -InstallRoot $InstallRoot
    if ($null -eq $python) {
        $facts.python = 'missing'
        Write-InstallLog 'No compatible Python runtime is installed yet. The installer will install Python 3.12.10.'
    } else {
        $facts.python = $python.Version
        Write-InstallLog "Python: $($python.Version) at $($python.Executable)"
        if ($python.Command) {
            Write-InstallLog "Python command: $($python.Command)"
        }
    }
    $ollama = Find-OllamaExecutable
    if ($null -eq $ollama) {
        $facts.ollama = 'missing'
        Write-InstallLog 'Ollama was not found.'
    } else {
        $facts.ollama = $ollama.Version
        Write-InstallLog "Ollama: $($ollama.Version) at $($ollama.Executable)"
    }
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($null -eq $git) {
        $facts.git = 'missing'
        Write-InstallLog 'Git was not found. Git is optional and is not required before installation.'
    } else {
        $gitText = @(& $git.Source --version) -join ' '
        $facts.git = $gitText.Trim()
        Write-InstallLog "Git: $($facts.git)"
    }
    foreach ($portName in @('ollama', 'comfyui')) {
        $port = [int]$Pins.ports.$portName
        foreach ($entry in @(Get-ListeningEntries -Port $port)) {
            $address = [string]$entry.LocalAddress
            if (-not (Test-LoopbackAddress -Address $address)) {
                $errors.Add("Port $port is listening on $address. The installer leaves that process alone.")
            } else {
                Write-InstallLog "Port $port is already listening on loopback."
            }
        }
    }
    foreach ($entry in @(Get-ListeningEntries -Port $ApiPort)) {
        $command = Get-ProcessCommandLine -ProcessId ([int]$entry.OwningProcess)
        if ($command -notlike '*local_ai_switchboard*') {
            $errors.Add("Port $ApiPort is already in use.")
        } else {
            Write-InstallLog "Port $ApiPort is already used by the gateway."
        }
    }
    return @{
        Ok       = ($errors.Count -eq 0)
        Errors   = @($errors)
        Warnings = @($warnings)
        Facts    = $facts
    }
}

function Install-PythonRuntime {
    $decision = Resolve-InstallerPython -InstallRoot $InstallRoot -InstallCommand {
        $installer = Join-Path $InstallRoot 'cache\python-3.12.10-amd64.exe'
        Write-InstallLog "No compatible Python runtime found. Installing pinned Python 3.12.10 from $($Pins.python.url)."
        return (Install-PinnedPythonPackage -Pins $Pins -InstallerPath $installer -DryRun:$DryRun)
    }
    if ($decision.Runtime -and -not $decision.Installed) {
        if ([string]$decision.Runtime.Executable -like '*python_embeded*') {
            return New-StageResult -Verified $false -Detail 'The only detected Python is the ComfyUI embedded interpreter.' -Facts @{}
        }
        $script:ResolvedPythonRuntime = $decision.Runtime
        Write-InstallLog "Keeping Python $($decision.Runtime.Version) at $($decision.Runtime.Executable)."
        return New-StageResult -Verified $true -Detail 'Kept the existing Python runtime.' -Facts @{
            action     = 'kept'
            version    = [string]$decision.Runtime.Version
            executable = [string]$decision.Runtime.Executable
            command    = [string]$decision.Runtime.Command
        }
    }
    if ($null -ne $decision.Result) {
        return $decision.Result
    }
    $detail = "No compatible Python runtime found. Automatic installation did not return a result. Detection: $(Get-PythonDiscoveryText)"
    return New-StageResult -Verified $false -Detail $detail -Facts @{
        detection = (Get-PythonDiscoveryText)
    }
}

function Install-OllamaRuntime {
    $found = Find-OllamaExecutable
    if ($null -ne $found) {
        $comparison = Compare-DottedVersion -Left $found.Version -Right $Pins.ollama.version
        if ($comparison -ge 0 -or -not $UpgradeOllama) {
            Write-InstallLog "Keeping Ollama $($found.Version)."
            return New-StageResult -Verified $true -Detail 'Kept the existing Ollama installation.' -Facts @{
                action     = 'kept'
                version    = $found.Version
                executable = $found.Executable
            }
        }
        Write-InstallLog "Upgrading Ollama $($found.Version) to pin $($Pins.ollama.version)."
    }
    $installer = Join-Path $InstallRoot 'cache\OllamaSetup.exe'
    $saved = Save-PinnedFile -Url $Pins.ollama.url -Destination $installer -Algorithm $Pins.ollama.algorithm -Checksum $Pins.ollama.checksum -ExpectedBytes ([int64]$Pins.ollama.expected_bytes) -Label 'OllamaSetup.exe' -DryRun:$DryRun
    if (-not $saved) {
        return New-StageResult -Verified $false -Detail 'dry run' -Facts @{ Skipped = 'dry-run' }
    }
    $signature = Test-Authenticode -Path $installer -Organization $Pins.ollama.authenticode_organization
    Write-InstallLog "Ollama installer Authenticode status $($signature.Status)."
    if (-not $signature.Trusted) {
        return New-StageResult -Verified $false -Detail "The Ollama installer signature is $($signature.Status)." -Facts @{
            authenticode = $signature.Status
        }
    }
    $markerDir = Join-Path $env:LOCALAPPDATA 'Ollama'
    New-Item -ItemType Directory -Force -Path $markerDir | Out-Null
    New-Item -ItemType File -Force -Path (Join-Path $markerDir 'upgraded') | Out-Null
    $proc = Start-Process -FilePath $installer -ArgumentList @('/VERYSILENT', '/NORESTART', '/SUPPRESSMSGBOXES') -PassThru
    $proc.WaitForExit()
    if ($proc.ExitCode -ne 0) {
        return New-StageResult -Verified $false -Detail "OllamaSetup.exe exited with code $($proc.ExitCode)." -Facts @{}
    }
    Update-SessionPath
    $installed = Find-OllamaExecutable
    if ($null -eq $installed) {
        return New-StageResult -Verified $false -Detail 'ollama --version failed after the installer exited.' -Facts @{}
    }
    if ((Compare-DottedVersion -Left $installed.Version -Right $Pins.ollama.version) -ne 0) {
        return New-StageResult -Verified $false -Detail "ollama --version reported $($installed.Version) after the pinned installer ran." -Facts @{
            version = $installed.Version
        }
    }
    return New-StageResult -Verified $true -Detail 'Ollama installed and ollama --version matches the pin.' -Facts @{
        action       = 'installed'
        version      = $installed.Version
        executable   = $installed.Executable
        authenticode = $signature.Status
        sha256       = $Pins.ollama.checksum
    }
}

function Install-ComfyPortable {
    if ($Profile -ne 'Design') {
        return New-StageResult -Verified $true -Detail 'The Basic profile leaves ComfyUI uninstalled.' -Facts @{ action = 'profile-skip' }
    }
    $destination = Join-Path $InstallRoot 'ComfyUI_windows_portable'
    $existing = Find-ComfyPortableRoot -Path $destination
    if ($existing) {
        Write-InstallLog "Keeping the ComfyUI portable tree at $existing."
        return New-StageResult -Verified $true -Detail 'Kept the existing ComfyUI portable tree.' -Facts @{
            action  = 'kept'
            version = 'existing'
            root    = $existing
        }
    }
    if (Test-Path -LiteralPath $destination) {
        return New-StageResult -Verified $false -Detail 'The ComfyUI directory exists without python_embeded\python.exe and ComfyUI\main.py. It was left unchanged.' -Facts @{}
    }
    $archive = Join-Path $InstallRoot 'cache\ComfyUI_windows_portable_nvidia.7z'
    $saved = Save-PinnedFile -Url $Pins.comfyui.url -Destination $archive -Algorithm $Pins.comfyui.algorithm -Checksum $Pins.comfyui.checksum -ExpectedBytes ([int64]$Pins.comfyui.expected_bytes) -Label 'ComfyUI Windows Portable NVIDIA' -DryRun:$DryRun
    if (-not $saved) {
        return New-StageResult -Verified $false -Detail 'dry run' -Facts @{ Skipped = 'dry-run' }
    }
    $staging = Join-Path $InstallRoot 'cache\comfy-extract'
    try {
        $expanded = Expand-PinnedArchive -Archive $archive -Destination $staging
    } catch {
        return New-StageResult -Verified $false -Detail $_.Exception.Message -Facts @{}
    }
    if (-not $expanded) {
        return New-StageResult -Verified $false -Detail 'The ComfyUI archive was not extracted.' -Facts @{}
    }
    $root = Find-ComfyPortableRoot -Path $staging
    if (-not $root) {
        return New-StageResult -Verified $false -Detail 'The extracted archive does not contain python_embeded\python.exe and ComfyUI\main.py.' -Facts @{}
    }
    Move-Item -LiteralPath $root -Destination $destination
    $finalRoot = Find-ComfyPortableRoot -Path $destination
    if (-not $finalRoot) {
        return New-StageResult -Verified $false -Detail 'The ComfyUI portable markers were missing after the tree was moved.' -Facts @{}
    }
    return New-StageResult -Verified $true -Detail 'ComfyUI portable archive checksum matched and the embedded interpreter was found.' -Facts @{
        action  = 'installed'
        version = [string]$Pins.comfyui.version
        root    = $finalRoot
        sha256  = [string]$Pins.comfyui.checksum
    }
}

function Install-OllamaModel {
    $ollama = Find-OllamaExecutable
    if ($null -eq $ollama) {
        return New-StageResult -Verified $false -Detail 'Ollama is not available for the model pull.' -Facts @{}
    }
    $models = Join-Path $InstallRoot 'models\ollama'
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $models | Out-Null
    }
    $hostState = Set-UserEnvironmentIfUnset -Name 'OLLAMA_HOST' -Value '127.0.0.1:11434' -DryRun:$DryRun
    $modelState = Set-UserEnvironmentIfUnset -Name 'OLLAMA_MODELS' -Value $models -DryRun:$DryRun
    if ($hostState -eq 'kept-different') {
        return New-StageResult -Verified $false -Detail 'OLLAMA_HOST is already set to a value other than 127.0.0.1:11434. It was left unchanged.' -Facts @{}
    }
    if ($DryRun) {
        Write-InstallLog "Would pull $($Pins.ollama.model) and create $($Pins.ollama.profile) with num_ctx $($Pins.ollama.num_ctx)."
        return New-StageResult -Verified $false -Detail 'dry run' -Facts @{ Skipped = 'dry-run'; checksum_verified = $false }
    }
    $env:OLLAMA_HOST = '127.0.0.1:11434'
    if ($modelState -ne 'kept-different') {
        $env:OLLAMA_MODELS = $models
    }
    $listed = Invoke-CapturedCommand -FilePath $ollama.Executable -ArgumentList @('list')
    $modelPresent = $listed.Text -match [regex]::Escape($Pins.ollama.model)
    $checksumNote = 'The Ollama library tag has no checksum in pins.json.'
    if (-not $modelPresent) {
        $allowed = $AcceptUnchecksummedModels
        if (-not $allowed -and -not $Silent) {
            $allowed = Confirm-InstallStep -Silent $false -DryRun $false -Prompt "Download $($Pins.ollama.model) through Ollama. $checksumNote" -DefaultNo
        }
        if (-not $allowed) {
            return New-StageResult -Verified $false -Detail 'Consent was not recorded for the unchecksummed Ollama model. Pass -AcceptUnchecksummedModels or accept the prompt.' -Facts @{
                checksum_verified = $false
            }
        }
        Write-InstallLog "Downloading $($Pins.ollama.model)."
        Invoke-ConsoleCommand -FilePath $ollama.Executable -ArgumentList @('pull', [string]$Pins.ollama.model)
        if ($LASTEXITCODE -ne 0) {
            return New-StageResult -Verified $false -Detail 'ollama pull failed.' -Facts @{ checksum_verified = $false }
        }
        $listed = Invoke-CapturedCommand -FilePath $ollama.Executable -ArgumentList @('list')
        if ($listed.Text -notmatch [regex]::Escape($Pins.ollama.model)) {
            return New-StageResult -Verified $false -Detail 'ollama list does not show the model after the pull.' -Facts @{ checksum_verified = $false }
        }
    }
    $modelFile = Join-Path $InstallRoot 'config\Modelfile.qwen35-4b-ctx8192'
    $modelText = "FROM $($Pins.ollama.model)`r`nPARAMETER num_ctx $($Pins.ollama.num_ctx)`r`n"
    $configDir = Split-Path -Parent $modelFile
    New-Item -ItemType Directory -Force -Path $configDir | Out-Null
    if (Test-Path -LiteralPath $modelFile) {
        $current = [System.IO.File]::ReadAllText($modelFile)
        if ($current -ne $modelText) {
            return New-StageResult -Verified $false -Detail 'An existing Modelfile differs from the 8192-context profile and was left unchanged.' -Facts @{}
        }
    } else {
        Write-Utf8File -Path $modelFile -Text $modelText
    }
    $shown = Invoke-CapturedCommand -FilePath $ollama.Executable -ArgumentList @('show', [string]$Pins.ollama.profile)
    if ($shown.ExitCode -ne 0) {
        Write-InstallLog "Creating $($Pins.ollama.profile). The library model is already present."
        Invoke-ConsoleCommand -FilePath $ollama.Executable -ArgumentList @('create', [string]$Pins.ollama.profile, '-f', $modelFile)
        if ($LASTEXITCODE -ne 0) {
            return New-StageResult -Verified $false -Detail 'ollama create failed for the 8192-context profile.' -Facts @{ checksum_verified = $false }
        }
        $shown = Invoke-CapturedCommand -FilePath $ollama.Executable -ArgumentList @('show', [string]$Pins.ollama.profile)
    }
    if ($shown.ExitCode -ne 0 -or $shown.Text -notmatch 'num_ctx\s+8192') {
        return New-StageResult -Verified $false -Detail 'ollama show did not report num_ctx 8192 for the profile.' -Facts @{ checksum_verified = $false }
    }
    return New-StageResult -Verified $true -Detail 'The Ollama model is listed and the 8192-context profile is present. The library tag checksum was not published in pins.json.' -Facts @{
        action            = 'ready'
        model             = [string]$Pins.ollama.model
        profile           = [string]$Pins.ollama.profile
        num_ctx           = 8192
        checksum_verified = $false
        models_dir        = if ($modelState -eq 'kept-different') { 'existing user value' } else { $models }
    }
}

function Install-ComfyModels {
    if ($Profile -ne 'Design') {
        return New-StageResult -Verified $true -Detail 'The Basic profile does not download image model files.' -Facts @{ action = 'profile-skip' }
    }
    $portable = Find-ComfyPortableRoot -Path (Join-Path $InstallRoot 'ComfyUI_windows_portable')
    if (-not $portable) {
        return New-StageResult -Verified $false -Detail 'The ComfyUI portable tree is not available for model files.' -Facts @{}
    }
    $legacyEncoder = Join-Path $portable 'ComfyUI\models\text_encoders\qwen_3_4b.safetensors'
    if (Test-Path -LiteralPath $legacyEncoder) {
        Write-InstallLog 'Left qwen_3_4b.safetensors in place. It is not the FP4 encoder qwen_3_4b_fp4_flux2.safetensors and was not reused.'
    }
    if ($DryRun) {
        foreach ($model in @($Pins.comfy_models)) {
            Write-InstallLog "Would download $($model.filename) from $($model.url) and require $($model.algorithm) $($model.checksum)."
        }
        return New-StageResult -Verified $false -Detail 'dry run' -Facts @{ Skipped = 'dry-run' }
    }
    $verifiedFiles = @()
    foreach ($model in @($Pins.comfy_models)) {
        $folder = Join-Path $portable "ComfyUI\models\$($model.folder)"
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        $destination = Join-Path $folder $model.filename
        $saved = Save-PinnedFile -Url $model.url -Destination $destination -Algorithm $model.algorithm -Checksum $model.checksum -ExpectedBytes ([int64]$model.expected_bytes) -Label $model.filename
        if (-not $saved) {
            return New-StageResult -Verified $false -Detail "The checksum for $($model.filename) was not verified." -Facts @{}
        }
        $verifiedFiles += $model.filename
    }
    return New-StageResult -Verified $true -Detail 'The pinned Klein files match their published LFS checksums.' -Facts @{
        action            = 'installed'
        files             = $verifiedFiles
        checksum_verified = $true
    }
}

function Install-WorkflowSlot {
    $script:PendingWorkflowPath = $null
    if ($Profile -ne 'Design') {
        return New-StageResult -Verified $true -Detail 'The Basic profile does not install the image workflow slot.' -Facts @{
            action                  = 'profile-skip'
            image_workflow          = 'not in the Basic profile'
            image_generation        = $false
            image_generation_tested = $false
        }
    }
    $source = Join-Path $SourceRoot 'src\local_ai_switchboard\workflows\flux2-klein-4b.json'
    $supplied = -not [string]::IsNullOrWhiteSpace($WorkflowFile)
    $candidate = $source
    if ($supplied) {
        if (-not (Test-Path -LiteralPath $WorkflowFile)) {
            return New-StageResult -Verified $false -Detail "WorkflowFile was not found at $WorkflowFile." -Facts @{
                image_generation        = $false
                image_generation_tested = $false
            }
        }
        $candidate = (Resolve-Path -LiteralPath $WorkflowFile).Path
    } elseif (-not (Test-Path -LiteralPath $source)) {
        return New-StageResult -Verified $false -Detail 'The disabled flux2-klein-4b workflow file is missing from the source tree.' -Facts @{}
    }
    try {
        $payload = Get-Content -LiteralPath $candidate -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return New-StageResult -Verified $false -Detail 'The workflow file is not valid JSON. A gateway manifest is required.' -Facts @{
            image_generation = $false
        }
    }
    if (Test-RawComfyApiExport -Payload $payload) {
        return New-StageResult -Verified $false -Detail 'WorkflowFile is a raw ComfyUI API export. Pass a gateway manifest with id, title, enabled, workflow, bindings, required, and defaults. The installer does not invent node ids or wrap a raw export.' -Facts @{
            image_generation        = $false
            image_generation_tested = $false
        }
    }
    $packagedReal = (-not $supplied) -and ($payload.enabled -ne $false -or $null -ne $payload.workflow)
    if ($supplied -or $packagedReal) {
        $script:PendingWorkflowPath = $candidate
        if ($DryRun) {
            Write-InstallLog "Would validate $candidate with load_manifest and copy it only when validation succeeds."
            return New-StageResult -Verified $false -Detail 'dry run' -Facts @{ Skipped = 'dry-run'; image_generation = $false }
        }
        Write-InstallLog "Workflow manifest $candidate will be validated after the gateway environment exists. Image generation is not ready."
        return New-StageResult -Verified $true -Detail 'The workflow manifest is waiting for validation in the gateway environment. Image generation is not ready.' -Facts @{
            action                  = 'pending-validation'
            path                    = $candidate
            image_workflow          = 'unavailable'
            image_generation        = $false
            image_generation_tested = $false
        }
    }
    $destinationDir = Join-Path $InstallRoot 'workflows'
    $destination = Join-Path $destinationDir 'flux2-klein-4b.json'
    if ($DryRun) {
        Write-InstallLog "Would copy the disabled workflow slot to $destination."
        return New-StageResult -Verified $false -Detail 'dry run' -Facts @{ Skipped = 'dry-run' }
    }
    New-Item -ItemType Directory -Force -Path $destinationDir | Out-Null
    if (Test-Path -LiteralPath $destination) {
        Write-InstallLog 'The existing workflow file was kept.'
        return New-StageResult -Verified $true -Detail 'Kept the existing workflow file. Image generation stays disabled until that file contains a real API export.' -Facts @{
            action                  = 'kept'
            path                    = $destination
            image_workflow          = 'unavailable'
            image_generation        = $false
            image_generation_tested = $false
        }
    }
    Copy-Item -LiteralPath $source -Destination $destination
    $copied = Get-Content -LiteralPath $destination -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($copied.enabled -ne $false -or $null -ne $copied.workflow) {
        return New-StageResult -Verified $false -Detail 'The copied workflow slot failed its disabled check.' -Facts @{}
    }
    return New-StageResult -Verified $true -Detail 'Installed the disabled flux2-klein-4b slot. A real ComfyUI API-format workflow export is required before image generation can run.' -Facts @{
        action                  = 'installed'
        path                    = $destination
        image_workflow          = 'unavailable'
        image_generation        = $false
        image_generation_tested = $false
    }
}

function Register-VerifiedWorkflow {
    if (-not $script:PendingWorkflowPath) {
        return $script:WorkflowStageResult
    }
    if ($DryRun) {
        return (New-StageResult -Verified $false -Detail 'dry run' -Facts @{ Skipped = 'dry-run'; image_generation = $false })
    }
    $venvPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        return (New-StageResult -Verified $false -Detail 'Workflow validation needs the gateway virtual environment.' -Facts @{
                image_generation        = $false
                image_generation_tested = $false
            })
    }
    $validated = Get-WorkflowManifestState -Python $venvPython -ManifestPath $script:PendingWorkflowPath
    if (-not $validated.Ok) {
        return (New-StageResult -Verified $false -Detail "validate_manifest rejected the workflow file. $($validated.Detail)" -Facts @{
                image_generation        = $false
                image_generation_tested = $false
            })
    }
    $destinationDir = Join-Path $InstallRoot 'workflows'
    New-Item -ItemType Directory -Force -Path $destinationDir | Out-Null
    $destination = Join-Path $destinationDir ($validated.Id + '.json')
    if (Test-Path -LiteralPath $destination) {
        $currentHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
        $incomingHash = (Get-FileHash -LiteralPath $script:PendingWorkflowPath -Algorithm SHA256).Hash
        if ($currentHash -ne $incomingHash -and -not $ForceConfig) {
            Write-InstallLog "Existing workflow $destination differs and was left unchanged. Pass -ForceConfig to replace it."
            $existing = Get-WorkflowManifestState -Python $venvPython -ManifestPath $destination
            $registered = $existing.Ok -and $existing.State -eq 'enabled' -and $existing.Graph -eq 'yes'
            $availability = if ($registered) { 'registered, not tested' } else { 'unavailable' }
            return (New-StageResult -Verified $true -Detail "Kept the existing workflow file. Image workflow is $availability. Image generation is not ready." -Facts @{
                    action                  = 'kept'
                    path                    = $destination
                    image_workflow          = $availability
                    image_generation        = $false
                    image_generation_tested = $false
                })
        }
        if ($currentHash -eq $incomingHash) {
            $registered = $validated.State -eq 'enabled' -and $validated.Graph -eq 'yes'
            $availability = if ($registered) { 'registered, not tested' } else { 'unavailable' }
            return (New-StageResult -Verified $true -Detail "Workflow $($validated.Id) already matches the validated manifest. Image workflow is $availability. Image generation is not ready." -Facts @{
                    action                  = 'kept'
                    path                    = $destination
                    image_workflow          = $availability
                    image_generation        = $false
                    image_generation_tested = $false
                })
        }
    }
    Copy-Item -LiteralPath $script:PendingWorkflowPath -Destination $destination -Force
    $copied = Get-WorkflowManifestState -Python $venvPython -ManifestPath $destination
    if (-not $copied.Ok) {
        return (New-StageResult -Verified $false -Detail 'The copied workflow file failed validate_manifest.' -Facts @{
                image_generation        = $false
                image_generation_tested = $false
            })
    }
    $registered = $copied.State -eq 'enabled' -and $copied.Graph -eq 'yes'
    $availability = if ($registered) { 'registered, not tested' } else { 'unavailable' }
    return (New-StageResult -Verified $true -Detail "Workflow $($copied.Id) was accepted by validate_manifest. Image workflow is $availability. Image generation is not ready until it is tested on this machine." -Facts @{
            action                  = 'registered'
            path                    = $destination
            image_workflow          = $availability
            image_generation        = $false
            image_generation_tested = $false
        })
}

function Invoke-InstallDoctor {
    if ($DryRun) {
        Write-InstallLog 'Would run doctor.ps1 and record gateway, Ollama, ComfyUI, and image workflow status separately.'
        return (New-StageResult -Verified $false -Detail 'dry run' -Facts @{ Skipped = 'dry-run' })
    }
    $doctor = Join-Path $InstallRoot 'service\doctor.ps1'
    if (-not (Test-Path -LiteralPath $doctor)) {
        $doctor = Join-Path $PSScriptRoot 'doctor.ps1'
    }
    if ($AllowOlderWindows) {
        & $doctor -InstallRoot $InstallRoot -AllowOlderWindows
    } else {
        & $doctor -InstallRoot $InstallRoot
    }
    if ($LASTEXITCODE -ne 0) {
        return (New-StageResult -Verified $false -Detail 'doctor.ps1 reported an operational failure. A missing image workflow does not change gateway readiness.' -Facts @{
                image_generation        = $false
                image_generation_tested = $false
            })
    }
    return (New-StageResult -Verified $true -Detail 'doctor.ps1 completed. Image generation stays not ready until a validated workflow is registered and tested.' -Facts @{
            action                  = 'diagnosed'
            image_generation        = $false
            image_generation_tested = $false
        })
}

function Install-GatewaySoftware {
    $python = $null
    if ($null -ne $script:ResolvedPythonRuntime -and $script:ResolvedPythonRuntime.Executable) {
        $python = $script:ResolvedPythonRuntime
    } else {
        $python = Find-Python312 -InstallRoot $InstallRoot
    }
    if ($null -eq $python) {
        if ($DryRun) {
            Write-InstallLog 'Would install Python 3.12.10 and create the gateway virtual environment with that interpreter.'
            Write-InstallLog 'Would write .env when that file is absent.'
            return New-StageResult -Verified $false -Detail 'dry run' -Facts @{ Skipped = 'dry-run' }
        }
        return New-StageResult -Verified $false -Detail 'Gateway installation needs Python 3.12 or newer. No compatible runtime was selected, so the virtual environment was not created.' -Facts @{
            detection = (Get-PythonDiscoveryText)
        }
    }
    if ([string]$python.Executable -like '*python_embeded*') {
        return New-StageResult -Verified $false -Detail 'Refusing to build the gateway virtual environment with the ComfyUI embedded interpreter.' -Facts @{}
    }
    $venvPython = Join-Path $InstallRoot 'venv\Scripts\python.exe'
    $projectVersion = Read-ProjectVersion -SourceRoot $SourceRoot
    if ($DryRun) {
        Write-InstallLog "Would create the gateway virtual environment with $($python.Executable) -m venv $(Join-Path $InstallRoot 'venv') and install project version $projectVersion."
        Write-InstallLog 'Would write .env when that file is absent.'
        return New-StageResult -Verified $false -Detail 'dry run' -Facts @{ Skipped = 'dry-run' }
    }
    foreach ($directory in @(
            (Join-Path $InstallRoot 'config'),
            (Join-Path $InstallRoot 'logs'),
            (Join-Path $InstallRoot 'data'),
            (Join-Path $InstallRoot 'data\outputs\images'),
            (Join-Path $InstallRoot 'cache'),
            (Join-Path $InstallRoot 'service')
        )) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
    $plan = Get-GatewayVenvPlan -VenvPython $venvPython
    if ($plan.Action -eq 'blocked' -or $plan.Action -eq 'invalid') {
        return New-StageResult -Verified $false -Detail $plan.Detail -Facts @{
            python = [string]$python.Executable
            venv   = $venvPython
        }
    }
    if ($plan.Action -eq 'create') {
        $created = Invoke-GatewayVenvCreate -PythonExecutable ([string]$python.Executable) -VenvPython $venvPython
        if (-not $created.Ok) {
            return New-StageResult -Verified $false -Detail $created.Detail -Facts @{
                python  = [string]$python.Executable
                command = [string]$created.Command
            }
        }
    } else {
        Write-InstallLog "Reusing the gateway virtual environment at $venvPython ($($plan.Detail))."
    }
    $venvCheck = Test-GatewayVirtualEnvironment -VenvPython $venvPython
    if (-not $venvCheck.Ok) {
        return New-StageResult -Verified $false -Detail $venvCheck.Detail -Facts @{
            python = [string]$python.Executable
            venv   = $venvPython
        }
    }
    $venvText = @(& $venvPython -c 'import sys; sys.stdout.write(sys.executable + chr(10) + sys.version.split()[0] + chr(10))') -join "`n"
    if ($LASTEXITCODE -ne 0 -or $venvText -like '*python_embeded*') {
        return New-StageResult -Verified $false -Detail 'The gateway interpreter check failed.' -Facts @{}
    }
    $probe = & $venvPython -c 'import importlib.metadata as metadata; print(metadata.version(''local-ai-switchboard''))' 2>$null
    $needsInstall = $LASTEXITCODE -ne 0
    if (-not $needsInstall) {
        $installedVersion = [string]@($probe)[-1]
        if ($installedVersion -ne $projectVersion) {
            Write-InstallLog "Gateway package $installedVersion is already installed. Leaving it in place. Use update.ps1 to change it."
        }
    }
    if ($needsInstall) {
        Invoke-PipInstall -Python $venvPython -SourceRoot $SourceRoot
        if ($LASTEXITCODE -ne 0) {
            return New-StageResult -Verified $false -Detail 'pip could not install the gateway project.' -Facts @{}
        }
    }
    $versionCode = 'import importlib.metadata as metadata; names=(''local-ai-switchboard'',''fastapi'',''httpx'',''pydantic-settings'',''uvicorn''); print(chr(10).join(name + ''=='' + metadata.version(name) for name in names))'
    $versions = @(& $venvPython -c $versionCode)
    if ($LASTEXITCODE -ne 0 -or $versions.Count -lt 5) {
        return New-StageResult -Verified $false -Detail 'The gateway dependencies could not be imported after installation.' -Facts @{}
    }
    $portableRoot = ''
    if ($Profile -eq 'Design') {
        $portableRoot = [string](Find-ComfyPortableRoot -Path (Join-Path $InstallRoot 'ComfyUI_windows_portable'))
    }
    $envPath = Join-Path $InstallRoot '.env'
    if ((Test-Path -LiteralPath $envPath) -and -not $ForceConfig) {
        $envRaw = [System.IO.File]::ReadAllText($envPath)
        if ($envRaw -notmatch 'LAS_OLLAMA_URL=http://127\.0\.0\.1:11434' -or $envRaw -notmatch 'LAS_COMFYUI_URL=http://127\.0\.0\.1:8188') {
            return New-StageResult -Verified $false -Detail 'The existing .env does not keep Ollama and ComfyUI on loopback. It was left unchanged.' -Facts @{}
        }
        Write-InstallLog 'Kept the existing .env file.'
    } else {
        $lines = @(
            "LAS_INSTALL_ROOT=$InstallRoot",
            "LAS_STORAGE_DIR=$(Join-Path $InstallRoot 'data')",
            "LAS_LOG_DIR=$(Join-Path $InstallRoot 'logs')",
            'LAS_OLLAMA_URL=http://127.0.0.1:11434',
            'LAS_COMFYUI_URL=http://127.0.0.1:8188',
            "LAS_API_HOST=0.0.0.0",
            "LAS_API_PORT=$ApiPort",
            'LAS_GPU_MEMORY_THRESHOLD_MB=1024',
            'LAS_OUTPUT_RETENTION_DAYS=14'
        )
        if ($portableRoot) {
            $lines += "LAS_COMFYUI_PORTABLE_DIR=$portableRoot"
            $lines += "LAS_WORKFLOWS_DIR=$(Join-Path $InstallRoot 'workflows')"
        }
        Write-Utf8File -Path $envPath -Text (($lines -join "`r`n") + "`r`n")
        Write-InstallLog "Wrote $envPath."
    }
    Write-Utf8File -Path (Join-Path $InstallRoot 'config\source-root.txt') -Text $SourceRoot
    $initScript = Join-Path $InstallRoot 'config\init_db.py'
    $initLines = @(
        'from local_ai_switchboard.config.settings import Settings',
        'from local_ai_switchboard.storage.jobs import JobStore',
        'settings = Settings()',
        'path = settings.resolved_storage_dir() / "switchboard.db"',
        'store = JobStore(path)',
        'store.close()',
        'print(path)'
    )
    Write-Utf8File -Path $initScript -Text (($initLines -join "`r`n") + "`r`n")
    Push-Location $InstallRoot
    try {
        $dbOutput = @(& $venvPython $initScript)
    } finally {
        Pop-Location
    }
    if ($LASTEXITCODE -ne 0) {
        return New-StageResult -Verified $false -Detail 'SQLite initialization failed.' -Facts @{}
    }
    $dbPath = [string]@($dbOutput)[-1]
    if (-not (Test-Path -LiteralPath $dbPath)) {
        return New-StageResult -Verified $false -Detail 'switchboard.db was not created.' -Facts @{}
    }
    $headerBytes = [System.IO.File]::ReadAllBytes($dbPath)
    $header = [System.Text.Encoding]::ASCII.GetString($headerBytes, 0, [Math]::Min(15, $headerBytes.Length))
    if ($header -ne 'SQLite format 3') {
        return New-StageResult -Verified $false -Detail 'The database file is not SQLite.' -Facts @{}
    }
    $service = Join-Path $InstallRoot 'service'
    foreach ($name in @('Common.ps1', 'pins.json', 'start.ps1', 'stop.ps1', 'doctor.ps1', 'uninstall.ps1', 'update.ps1')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $service $name) -Force
    }
    return New-StageResult -Verified $true -Detail 'The gateway environment imports and switchboard.db is SQLite.' -Facts @{
        action              = 'ready'
        python              = $venvPython
        base_python         = [string]$python.Executable
        project_version     = $projectVersion
        dependency_versions = @($versions)
        database            = $dbPath
    }
}

function Invoke-StackVerification {
    param($GatewayFacts)
    $checks = @{}
    $ollama = Find-OllamaExecutable
    if ($null -eq $ollama) {
        return New-StageResult -Verified $false -Detail 'Verification could not find ollama.exe.' -Facts @{}
    }
    $ollamaStatus = Test-HttpStatus -Url 'http://127.0.0.1:11434/api/version'
    if ($ollamaStatus -ne 200) {
        $app = Join-Path (Split-Path -Parent $ollama.Executable) 'Ollama.exe'
        if (Test-Path -LiteralPath $app) {
            if ($DryRun) {
                Write-InstallLog 'Would start the Ollama application and request /api/version.'
            } else {
                Start-Process -FilePath $app | Out-Null
                $ollamaStatus = Wait-HttpStatus -Url 'http://127.0.0.1:11434/api/version' -Accept @(200) -TimeoutSeconds 40
            }
        }
    }
    foreach ($entry in @(Get-ListeningEntries -Port 11434)) {
        if (-not (Test-LoopbackAddress -Address ([string]$entry.LocalAddress))) {
            return New-StageResult -Verified $false -Detail 'Ollama is listening outside 127.0.0.1.' -Facts @{}
        }
    }
    $checks.ollama_http = $ollamaStatus
    if (-not $DryRun -and $ollamaStatus -ne 200) {
        return New-StageResult -Verified $false -Detail 'Ollama /api/version did not return 200.' -Facts $checks
    }
    if ($Profile -eq 'Design') {
        $portable = Find-ComfyPortableRoot -Path (Join-Path $InstallRoot 'ComfyUI_windows_portable')
        if (-not $portable) {
            return New-StageResult -Verified $false -Detail 'ComfyUI portable markers were missing during verification.' -Facts $checks
        }
        $comfyUrl = 'http://127.0.0.1:8188/system_stats'
        $comfyStatus = Test-HttpStatus -Url $comfyUrl
        $started = $false
        $comfyProcess = 0
        if ($comfyStatus -ne 200) {
            if ($DryRun) {
                Write-InstallLog 'Would start ComfyUI on 127.0.0.1 for a health check and then stop that process.'
            } else {
                $mainScript = Join-Path $portable 'ComfyUI\main.py'
                $comfyArguments = @(
                    '-s'
                    $mainScript
                    '--listen'
                    '127.0.0.1'
                    '--port'
                    ([string]$Pins.comfyui.port)
                    '--disable-auto-launch'
                    '--windows-standalone-build'
                )
                $proc = Start-Process -FilePath (Join-Path $portable 'python_embeded\python.exe') -ArgumentList $comfyArguments -WorkingDirectory $portable -WindowStyle Hidden -PassThru
                $started = $true
                $comfyProcess = [int]$proc.Id
                $comfyStatus = Wait-HttpStatus -Url $comfyUrl -Accept @(200) -TimeoutSeconds 90
            }
        }
        foreach ($entry in @(Get-ListeningEntries -Port 8188)) {
            if (-not (Test-LoopbackAddress -Address ([string]$entry.LocalAddress))) {
                if ($started) {
                    Stop-OwnedProcess -ProcessId $comfyProcess -Label 'ComfyUI verification' | Out-Null
                }
                return New-StageResult -Verified $false -Detail 'ComfyUI is listening outside 127.0.0.1.' -Facts $checks
            }
        }
        if ($started) {
            Stop-OwnedProcess -ProcessId $comfyProcess -Label 'ComfyUI verification' | Out-Null
        }
        $checks.comfy_http = $comfyStatus
        if (-not $DryRun -and $comfyStatus -ne 200) {
            return New-StageResult -Verified $false -Detail 'ComfyUI /system_stats did not return 200.' -Facts $checks
        }
    }
    $gpu = Get-NvidiaReport
    $checks.gpu = $gpu.Detail
    if (-not $gpu.Available) {
        return New-StageResult -Verified $false -Detail 'nvidia-smi did not return a GPU during verification.' -Facts $checks
    }
    if ($DryRun) {
        Write-InstallLog 'Would start the gateway and request /health/live and /health/ready.'
        return New-StageResult -Verified $false -Detail 'dry run' -Facts @{ Skipped = 'dry-run' }
    }
    $startScript = Join-Path $InstallRoot 'service\start.ps1'
    & $startScript -InstallRoot $InstallRoot
    if ($LASTEXITCODE -ne 0) {
        return New-StageResult -Verified $false -Detail 'The gateway did not become ready.' -Facts $checks
    }
    $live = Test-HttpStatus -Url "http://127.0.0.1:$ApiPort/health/live"
    $ready = Test-HttpStatus -Url "http://127.0.0.1:$ApiPort/health/ready"
    $checks.gateway_live = $live
    $checks.gateway_ready = $ready
    if ($live -ne 200 -or $ready -ne 200) {
        return New-StageResult -Verified $false -Detail "Gateway health returned live $live and ready $ready." -Facts $checks
    }
    return New-StageResult -Verified $true -Detail 'Ollama, the gateway, and nvidia-smi responded.' -Facts $checks
}

$preflight = Get-MachinePreflight
foreach ($warning in @($preflight.Warnings)) {
    Write-InstallLog $warning 'WARN'
}
if (-not $preflight.Ok) {
    foreach ($item in @($preflight.Errors)) {
        Write-InstallLog $item 'ERROR'
    }
    exit 1
}
if (@($preflight.Warnings).Count -gt 0 -and -not $Silent -and -not $DryRun) {
    $continue = Confirm-InstallStep -Silent $false -DryRun $false -Prompt 'Continue with the warnings above' -DefaultNo
    if (-not $continue) {
        Write-InstallLog 'The install was left unchanged.'
        exit 1
    }
}

Write-InstallLog "Python pin: $($Pins.python.url)"
Write-InstallLog "Ollama pin: $($Pins.ollama.url)"
if ($Profile -eq 'Design') {
    Write-InstallLog "ComfyUI pin: $($Pins.comfyui.url)"
    foreach ($model in @($Pins.comfy_models)) {
        Write-InstallLog "Model pin: $($model.filename) $($model.expected_bytes) bytes from $($model.url)"
    }
}
if (-not $Silent -and -not $DryRun) {
    $continue = Confirm-InstallStep -Silent $false -DryRun $false -Prompt 'Continue with this install' -DefaultNo:$false
    if (-not $continue) {
        Write-InstallLog 'The install was left unchanged.'
        exit 1
    }
}

$components = @{}
$pythonResult = Install-PythonRuntime
Add-ComponentResult -Components $components -Name 'python' -Result $pythonResult
Stop-ForStage -Components $components -Result $pythonResult | Out-Null

$ollamaResult = Install-OllamaRuntime
Add-ComponentResult -Components $components -Name 'ollama' -Result $ollamaResult
Stop-ForStage -Components $components -Result $ollamaResult | Out-Null

$comfyResult = Install-ComfyPortable
Add-ComponentResult -Components $components -Name 'comfyui' -Result $comfyResult
Stop-ForStage -Components $components -Result $comfyResult | Out-Null

$modelResult = Install-OllamaModel
Add-ComponentResult -Components $components -Name 'ollama_model' -Result $modelResult
Stop-ForStage -Components $components -Result $modelResult | Out-Null

$weightsResult = Install-ComfyModels
Add-ComponentResult -Components $components -Name 'comfy_models' -Result $weightsResult
Stop-ForStage -Components $components -Result $weightsResult | Out-Null

$script:PendingWorkflowPath = $null
$script:WorkflowStageResult = $null
$workflowResult = Install-WorkflowSlot
$script:WorkflowStageResult = $workflowResult
Add-ComponentResult -Components $components -Name 'workflow' -Result $workflowResult
Stop-ForStage -Components $components -Result $workflowResult | Out-Null

$gatewayResult = Install-GatewaySoftware
Add-ComponentResult -Components $components -Name 'gateway' -Result $gatewayResult
Stop-ForStage -Components $components -Result $gatewayResult | Out-Null

$workflowResult = Register-VerifiedWorkflow
$script:WorkflowStageResult = $workflowResult
Add-ComponentResult -Components $components -Name 'workflow' -Result $workflowResult
Stop-ForStage -Components $components -Result $workflowResult | Out-Null

if ($SkipFirewall) {
    $firewallResult = New-StageResult -Verified $false -Detail 'The firewall rule was not configured because -SkipFirewall was set.' -Facts @{}
} else {
    $firewallResult = Install-GatewayFirewall -ApiPort $ApiPort -DryRun:$DryRun
    if (-not $firewallResult.Verified -and $firewallResult.Facts.ContainsKey('action') -and $firewallResult.Facts.action -eq 'needs-admin') {
        $shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $arguments = @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath,
            '-FirewallOnly', '-InstallRoot', $InstallRoot, '-ApiPort', "$ApiPort"
        )
        if ($DryRun) {
            $arguments += '-WhatIf'
        }
        try {
            $elevated = Start-Process -FilePath $shell -ArgumentList $arguments -Verb RunAs -Wait -PassThru
            if ($elevated.ExitCode -eq 0) {
                $firewallResult = Install-GatewayFirewall -ApiPort $ApiPort -DryRun:$DryRun
            }
        } catch {
            Write-InstallLog 'The firewall approval prompt was dismissed.' 'ERROR'
        }
    }
}
Add-ComponentResult -Components $components -Name 'firewall' -Result $firewallResult
Stop-ForStage -Components $components -Result $firewallResult | Out-Null

$taskResult = Install-GatewayLogonTask -InstallRoot $InstallRoot -DryRun:$DryRun
Add-ComponentResult -Components $components -Name 'startup' -Result $taskResult
Stop-ForStage -Components $components -Result $taskResult | Out-Null

$verification = Invoke-StackVerification -GatewayFacts $gatewayResult.Facts
Add-ComponentResult -Components $components -Name 'verification' -Result $verification
Stop-ForStage -Components $components -Result $verification | Out-Null

$doctorResult = Invoke-InstallDoctor
Add-ComponentResult -Components $components -Name 'doctor' -Result $doctorResult
Stop-ForStage -Components $components -Result $doctorResult | Out-Null

Write-InstallLog 'Installation checks finished.'
if (-not $DryRun) {
    $addresses = @(Get-LanIPv4Addresses)
    if ($addresses.Count -eq 0) {
        Write-InstallLog "No LAN IPv4 address was found. The gateway bind is 0.0.0.0:$ApiPort."
    } else {
        foreach ($address in $addresses) {
            Write-InstallLog "Gateway URL: http://${address}:$ApiPort/v1"
        }
    }
    Write-InstallLog 'The gateway does not use a password or API token. Open CoDesign and the Mac client use the Gateway URL only.'
    if ($Profile -eq 'Design') {
        Write-InstallLog 'Image generation: not ready. A missing or untested workflow does not change gateway, Ollama, or ComfyUI readiness.'
    }
}
if ($DryRun) {
    Write-InstallLog 'Dry run finished. No component was installed.'
    exit 0
}
Write-InstallLog 'Installation finished and the recorded checks were verified.'
exit 0
