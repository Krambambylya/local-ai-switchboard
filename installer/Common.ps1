# Local helpers for the Windows installer. This file is dot-sourced from the
# repository or from the install-root service directory. It is not downloaded.

Set-StrictMode -Version 2.0

$script:InstallerDirectory = $PSScriptRoot
$script:LogPath = $null
$script:ResolvedPythonRuntime = $null
$script:PythonDiscoveryLog = $null

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {
    Write-Host 'TLS 1.2 could not be enabled for this PowerShell process.'
}

function Write-InstallLog {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level = 'INFO'
    )
    $line = '{0} {1} {2}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $Level, $Message
    if ($script:LogPath) {
        Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
    }
    Write-Host $line
}

function Open-ServiceLog {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $directory = Join-Path $InstallRoot 'logs'
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $script:LogPath = Join-Path $directory 'service.log'
}

function Initialize-InstallLog {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [switch]$DryRun
    )
    if ($DryRun) {
        $script:LogPath = $null
        Write-InstallLog 'Dry run: actions are printed and files are not changed.'
        return
    }
    New-Item -ItemType Directory -Force -Path $Directory | Out-Null
    $name = 'installer-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
    $script:LogPath = Join-Path $Directory $name
    Write-InstallLog "Installer log: $($script:LogPath)"
}

function Get-InstallerPins {
    $path = Join-Path $script:InstallerDirectory 'pins.json'
    if (-not (Test-Path -LiteralPath $path)) {
        throw "pins.json is missing at $path"
    }
    return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function New-StageResult {
    param(
        [Parameter(Mandatory = $true)][bool]$Verified,
        [Parameter(Mandatory = $true)][string]$Detail,
        [hashtable]$Facts
    )
    if ($null -eq $Facts) {
        $Facts = @{}
    }
    return @{
        Verified = $Verified
        Detail   = $Detail
        Facts    = $Facts
    }
}

function Write-Utf8File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text
    )
    $encoding = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Write-InstallManifest {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Manifest,
        [switch]$DryRun
    )
    $json = $Manifest | ConvertTo-Json -Depth 8
    if ($DryRun) {
        Write-InstallLog 'Would write config\installed.json.'
        return
    }
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Write-Utf8File -Path $Path -Text $json
}

function Confirm-InstallStep {
    param(
        [Parameter(Mandatory = $true)][bool]$Silent,
        [Parameter(Mandatory = $true)][bool]$DryRun,
        [Parameter(Mandatory = $true)][string]$Prompt,
        [switch]$DefaultNo
    )
    if ($DryRun -or $Silent) {
        return $true
    }
    $hint = 'Y/n'
    if ($DefaultNo) {
        $hint = 'y/N'
    }
    $answer = Read-Host "$Prompt [$hint]"
    if ($DefaultNo) {
        return $answer -match '^[Yy]'
    }
    return ($answer -eq '' -or $answer -match '^[Yy]')
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-CurrentUserName {
    return [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
}

function Read-ProjectVersion {
    param([Parameter(Mandatory = $true)][string]$SourceRoot)
    $path = Join-Path $SourceRoot 'pyproject.toml'
    if (-not (Test-Path -LiteralPath $path)) {
        throw "pyproject.toml is missing at $path"
    }
    $raw = Get-Content -LiteralPath $path -Raw -Encoding UTF8
    if ($raw -notmatch '(?m)^version\s*=\s*"([^"]+)"') {
        throw 'The project version is missing from pyproject.toml.'
    }
    return $Matches[1]
}

function Compare-DottedVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )
    $leftVersion = [version]$Left
    $rightVersion = [version]$Right
    if ($leftVersion -gt $rightVersion) { return 1 }
    if ($leftVersion -lt $rightVersion) { return -1 }
    return 0
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Set-UserEnvironmentIfUnset {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value,
        [switch]$DryRun
    )
    $current = [Environment]::GetEnvironmentVariable($Name, 'User')
    if ([string]::IsNullOrEmpty($current)) {
        if ($DryRun) {
            Write-InstallLog "Would set user environment $Name."
            return 'would-set'
        }
        [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
        Set-Item -Path "Env:$Name" -Value $Value
        Write-InstallLog "Set user environment $Name."
        return 'set'
    }
    if ($current -eq $Value) {
        Set-Item -Path "Env:$Name" -Value $Value
        Write-InstallLog "User environment $Name already matches."
        return 'kept'
    }
    Write-InstallLog "User environment $Name already has a value. It was left unchanged." 'WARN'
    return 'kept-different'
}

function Get-FreeBytesForPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $root = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($Path))
    $drive = New-Object System.IO.DriveInfo $root
    if (-not $drive.IsReady) {
        throw "The drive for $Path is not ready."
    }
    return [int64]$drive.AvailableFreeSpace
}

function Get-NvidiaReport {
    $report = @{
        Available    = $false
        Name         = ''
        Driver       = ''
        MemoryMiB    = 0
        Detail       = 'nvidia-smi is unavailable'
        CommandFound = $false
    }
    $command = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        $command = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    }
    if ($null -eq $command) {
        return $report
    }
    $report.CommandFound = $true
    $output = & $command.Source --query-gpu=name,driver_version,memory.total --format=csv,noheader,nounits
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace([string]$output)) {
        $report.Detail = 'nvidia-smi did not return a GPU row.'
        return $report
    }
    $line = @($output)[0].ToString().Trim()
    $parts = @($line.Split(','))
    if ($parts.Count -lt 3) {
        $report.Detail = 'nvidia-smi returned an unexpected row.'
        return $report
    }
    $memoryText = $parts[$parts.Count - 1].Trim()
    $driver = $parts[$parts.Count - 2].Trim()
    $name = ($parts[0..($parts.Count - 3)] -join ',').Trim()
    $memory = 0
    if (-not [int]::TryParse($memoryText, [ref]$memory)) {
        $report.Detail = 'nvidia-smi memory could not be read.'
        return $report
    }
    $report.Available = $true
    $report.Name = $name
    $report.Driver = $driver
    $report.MemoryMiB = $memory
    $report.Detail = "$name driver $driver VRAM $memory MiB"
    return $report
}

function Get-ListeningEntries {
    param([Parameter(Mandatory = $true)][int]$Port)
    try {
        $entries = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
    } catch {
        return @()
    }
    return $entries
}

function Test-LoopbackAddress {
    param([AllowEmptyString()][string]$Address)
    return $Address -eq '127.0.0.1' -or $Address -eq '::1' -or $Address -eq '0:0:0:0:0:0:0:1'
}

function Get-ProcessCommandLine {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    try {
        $process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$ProcessId"
    } catch {
        return ''
    }
    if ($null -eq $process) {
        return ''
    }
    return [string]$process.CommandLine
}

function Get-ObjectProperty {
    param(
        $Object,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ($null -eq $Object) {
        return $null
    }
    if ($Object -is [hashtable]) {
        if ($Object.ContainsKey($Name)) {
            return $Object[$Name]
        }
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Get-PythonPin {
    param($Pins)
    $pin = Get-ObjectProperty -Object $Pins -Name 'python'
    if ($null -eq $pin) {
        throw 'pins.json is missing the python entry.'
    }
    return $pin
}

function Write-PythonDiscovery {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level = 'INFO'
    )
    if ($null -eq $script:PythonDiscoveryLog) {
        $script:PythonDiscoveryLog = New-Object System.Collections.Generic.List[string]
    }
    $script:PythonDiscoveryLog.Add($Message)
    Write-InstallLog $Message $Level
}

function Get-PythonDiscoveryText {
    if ($null -eq $script:PythonDiscoveryLog -or $script:PythonDiscoveryLog.Count -eq 0) {
        return 'No Python discovery records were written.'
    }
    return ($script:PythonDiscoveryLog -join "`n")
}

function Limit-DiscoveryText {
    param(
        [AllowEmptyString()][string]$Text,
        [int]$Limit = 800
    )
    if ([string]::IsNullOrEmpty($Text)) {
        return ''
    }
    if ($Text.Length -le $Limit) {
        return $Text
    }
    return $Text.Substring(0, $Limit)
}

function Test-SupportedPythonVersion {
    param([AllowEmptyString()][string]$Version)
    return [bool]($Version -match '^3\.(1[2-9]|[2-9]\d)(\.|$)')
}

function Test-SamePath {
    param(
        [AllowEmptyString()][string]$Left,
        [AllowEmptyString()][string]$Right
    )
    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) {
        return $false
    }
    $a = $Left
    $b = $Right
    try { $a = [System.IO.Path]::GetFullPath($Left) } catch { }
    try { $b = [System.IO.Path]::GetFullPath($Right) } catch { }
    return ($a.TrimEnd('\') -ieq $b.TrimEnd('\'))
}

function Test-RejectedPythonPath {
    param(
        [AllowEmptyString()][string]$Path,
        [string]$InstallRoot
    )
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return 'path is empty'
    }
    $full = $Path
    try {
        $full = [System.IO.Path]::GetFullPath($Path)
    } catch {
        $full = $Path
    }
    if ($full -like '*python_embeded*') {
        return 'ComfyUI embedded interpreter'
    }
    if ($full -like '*\Microsoft\WindowsApps\*' -or $full -like '*\WindowsApps\*') {
        return 'WindowsApps execution alias'
    }
    $leaf = [System.IO.Path]::GetFileName($full).ToLowerInvariant()
    if ($leaf -eq 'py.exe' -or $leaf -eq 'pyw.exe' -or $leaf -eq 'pythonw.exe') {
        return 'launcher executable is not a Python runtime'
    }
    if ($leaf -ne 'python.exe' -and $leaf -ne 'python3.exe') {
        return "unsupported executable name $leaf"
    }
    if (-not [string]::IsNullOrWhiteSpace($InstallRoot)) {
        try {
            $venvRoot = [System.IO.Path]::GetFullPath((Join-Path $InstallRoot 'venv'))
            $prefix = $venvRoot.TrimEnd('\') + '\'
            if ($full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                return 'gateway virtual environment'
            }
        } catch {
        }
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        return 'file does not exist'
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item -or $item.PSIsContainer) {
        return 'path is not a file'
    }
    if ([int64]$item.Length -le 0) {
        return 'file is empty'
    }
    return $null
}

function Add-UniquePythonCandidate {
    param(
        $List,
        $Seen,
        [AllowEmptyString()][string]$Path
    )
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }
    $key = $Path.Trim().ToLowerInvariant()
    if ($Seen.ContainsKey($key)) {
        return
    }
    $Seen[$key] = $true
    [void]$List.Add($Path.Trim())
}

function Get-StoredPythonExecutable {
    param([string]$InstallRoot)
    if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
        return ''
    }
    $path = Join-Path $InstallRoot 'config\installed.json'
    if (-not (Test-Path -LiteralPath $path)) {
        return ''
    }
    try {
        $manifest = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-PythonDiscovery "Could not read the stored Python path from $path. $($_.Exception.Message)" 'WARN'
        return ''
    }
    $stored = [string](Get-ObjectProperty -Object $manifest -Name 'python_executable')
    if (-not [string]::IsNullOrWhiteSpace($stored)) {
        return $stored
    }
    $components = Get-ObjectProperty -Object $manifest -Name 'components'
    $python = Get-ObjectProperty -Object $components -Name 'python'
    $facts = Get-ObjectProperty -Object $python -Name 'facts'
    return [string](Get-ObjectProperty -Object $facts -Name 'executable')
}

function Get-RegisteredPythonExecutables {
    $paths = New-Object System.Collections.Generic.List[string]
    foreach ($root in @(
            'HKCU:\Software\Python\PythonCore',
            'HKLM:\Software\Python\PythonCore',
            'HKLM:\Software\WOW6432Node\Python\PythonCore'
        )) {
        try {
            if (-not (Test-Path -LiteralPath $root)) {
                continue
            }
            foreach ($key in @(Get-ChildItem -LiteralPath $root -ErrorAction Stop)) {
                $versionName = [string]$key.PSChildName
                if ($versionName -notmatch '^3\.(1[2-9]|[2-9]\d)') {
                    continue
                }
                if ($versionName -match '-32') {
                    continue
                }
                $installKey = 'Registry::' + $key.Name + '\InstallPath'
                if (-not (Test-Path -LiteralPath $installKey)) {
                    continue
                }
                $item = Get-Item -LiteralPath $installKey -ErrorAction Stop
                $exe = [string]$item.GetValue('ExecutablePath')
                if ([string]::IsNullOrWhiteSpace($exe)) {
                    $dir = [string]$item.GetValue('')
                    if (-not [string]::IsNullOrWhiteSpace($dir)) {
                        $exe = Join-Path $dir 'python.exe'
                    }
                }
                if (-not [string]::IsNullOrWhiteSpace($exe)) {
                    $paths.Add($exe)
                }
            }
        } catch {
            Write-PythonDiscovery "Registry Python key $root could not be read. $($_.Exception.Message)" 'WARN'
        }
    }
    return ,$paths.ToArray()
}

function Get-KnownPythonExecutables {
    $paths = New-Object System.Collections.Generic.List[string]
    $roots = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $roots.Add((Join-Path $env:LOCALAPPDATA 'Programs\Python'))
        $roots.Add((Join-Path $env:LOCALAPPDATA 'Python'))
    }
    if (-not [string]::IsNullOrWhiteSpace($env:ProgramFiles)) {
        $roots.Add($env:ProgramFiles)
        $roots.Add((Join-Path $env:ProgramFiles 'Python'))
    }
    $programFilesX86 = [string]${env:ProgramFiles(x86)}
    if (-not [string]::IsNullOrWhiteSpace($programFilesX86)) {
        $roots.Add($programFilesX86)
        $roots.Add((Join-Path $programFilesX86 'Python'))
    }
    foreach ($root in @($roots.ToArray())) {
        if ([string]::IsNullOrWhiteSpace($root) -or -not (Test-Path -LiteralPath $root)) {
            continue
        }
        foreach ($directory in @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue)) {
            $name = [string]$directory.Name
            if ($name -notmatch '^(Python3\d+|Python\d+|pythoncore-3\.(1[2-9]|[2-9]\d).*)$') {
                continue
            }
            if ($name -match '-32') {
                continue
            }
            $candidate = Join-Path $directory.FullName 'python.exe'
            if (Test-Path -LiteralPath $candidate) {
                $paths.Add($candidate)
            }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $pinned = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        if (Test-Path -LiteralPath $pinned) {
            $paths.Add($pinned)
        }
    }
    return ,$paths.ToArray()
}

function Get-PathPythonExecutables {
    $paths = New-Object System.Collections.Generic.List[string]
    if ([string]::IsNullOrWhiteSpace($env:Path)) {
        return ,@()
    }
    foreach ($entry in @($env:Path -split ';')) {
        $directory = $entry.Trim().Trim('"')
        if ([string]::IsNullOrWhiteSpace($directory)) {
            continue
        }
        if ($directory -like '*\WindowsApps*') {
            continue
        }
        foreach ($name in @('python.exe', 'python3.exe')) {
            $candidate = Join-Path $directory $name
            if (Test-Path -LiteralPath $candidate) {
                $paths.Add($candidate)
            }
        }
    }
    return ,$paths.ToArray()
}

function ConvertFrom-PythonLauncherList {
    param([AllowEmptyString()][string]$Text)
    $paths = New-Object System.Collections.Generic.List[string]
    if ([string]::IsNullOrWhiteSpace($Text)) {
        return ,@()
    }
    if ($Text -match 'No suitable Python runtime found') {
        return ,@()
    }
    $found = [regex]::Matches($Text, '[A-Za-z]:\\(?:[^:\r\n]*?)python(?:3)?\.exe')
    foreach ($match in @($found)) {
        $value = [string]$match.Value
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            $paths.Add($value.Trim())
        }
    }
    return ,$paths.ToArray()
}

function Test-CanExecuteFile {
    param([AllowEmptyString()][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item -or $item.PSIsContainer) {
        return $false
    }
    return ([int64]$item.Length -gt 0)
}

function Get-LivePythonLauncherText {
    $command = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        Write-PythonDiscovery 'Python launcher py.exe was not found.'
        return ''
    }
    $pyPath = [string]$command.Source
    $shown = "$pyPath -0p"
    if (-not (Test-CanExecuteFile -Path $pyPath)) {
        Write-PythonDiscovery "Ignoring Python launcher that cannot be executed: $shown" 'WARN'
        return ''
    }
    Write-PythonDiscovery "Listing Python launcher runtimes with: $shown"
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $restoreNative = $false
    $savedNative = $null
    if (Test-Path -LiteralPath 'variable:PSNativeCommandUseErrorActionPreference') {
        $restoreNative = $true
        $savedNative = $PSNativeCommandUseErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        $output = & $pyPath -0p 2>&1
        $code = $LASTEXITCODE
        $text = (@($output) | ForEach-Object { [string]$_ }) -join "`n"
        Write-PythonDiscovery "Launcher command $shown exited $code. Output: $(Limit-DiscoveryText -Text $text)"
        return $text
    } catch {
        $message = [string]$_.Exception.Message
        Write-PythonDiscovery "Launcher command $shown failed: $message" 'WARN'
        return $message
    } finally {
        $ErrorActionPreference = $previous
        if ($restoreNative) {
            $PSNativeCommandUseErrorActionPreference = $savedNative
        }
    }
}

function Invoke-PythonRuntimeProbe {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [scriptblock]$ProbeCommand,
        [switch]$SkipVenvProbe
    )
    # Windows PowerShell 5.1 strips double quotes from a native argument, so
    # python.exe -c receives invalid Python. The probe is a temporary file.
    $command = "$Executable <runtime probe>"
    if ($ProbeCommand) {
        $result = & $ProbeCommand $Executable ([bool]$SkipVenvProbe)
        if ($null -eq $result) {
            return @{
                Ok          = $false
                Reason      = 'probe returned no result'
                Command     = $command
                Executable  = $Executable
                Version     = ''
                Pip         = $false
                Venv        = $false
                ErrorOutput = ''
            }
        }
        return $result
    }
    $code = @'
import struct, sys
state = "venv-import-ok"
try:
    import venv
except Exception:
    state = "venv-import-missing"
sys.stdout.write("LAS_VERSION=" + sys.version.split()[0] + "\n")
sys.stdout.write("LAS_EXECUTABLE=" + sys.executable + "\n")
sys.stdout.write("LAS_BITS=" + str(struct.calcsize("P") * 8) + "\n")
sys.stdout.write("LAS_VENV=" + state + "\n")
'@
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $restoreNative = $false
    $savedNative = $null
    $probeFile = $null
    if (Test-Path -LiteralPath 'variable:PSNativeCommandUseErrorActionPreference') {
        $restoreNative = $true
        $savedNative = $PSNativeCommandUseErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        $probeFile = Join-Path ([System.IO.Path]::GetTempPath()) ('las-python-probe-' + [guid]::NewGuid().ToString('n') + '.py')
        $command = "$Executable $probeFile"
        $utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false
        [System.IO.File]::WriteAllText($probeFile, $code, $utf8)
        $output = & $Executable $probeFile 2>&1
        $exitCode = $LASTEXITCODE
        $text = Limit-DiscoveryText -Text ((@($output) | ForEach-Object { [string]$_ }) -join "`n") -Limit 2000
        if ($exitCode -ne 0 -or $text -match 'No suitable Python runtime found') {
            $reason = "exit code $exitCode"
            if ($text -match 'No suitable Python runtime found') {
                $reason = 'No suitable Python runtime found'
            }
            return @{
                Ok          = $false
                Reason      = $reason
                Command     = $command
                Executable  = $Executable
                Version     = ''
                Pip         = $false
                Venv        = $false
                ErrorOutput = $text
            }
        }
        $version = ''
        $reported = ''
        $bits = ''
        $venvState = ''
        foreach ($line in @($text -split "`r?`n")) {
            $value = [string]$line
            if ($value.StartsWith('LAS_VERSION=')) { $version = $value.Substring(12).Trim() }
            elseif ($value.StartsWith('LAS_EXECUTABLE=')) { $reported = $value.Substring(15).Trim() }
            elseif ($value.StartsWith('LAS_BITS=')) { $bits = $value.Substring(9).Trim() }
            elseif ($value.StartsWith('LAS_VENV=')) { $venvState = $value.Substring(9).Trim() }
        }
        if ([string]::IsNullOrWhiteSpace($version) -or [string]::IsNullOrWhiteSpace($reported)) {
            return @{
                Ok          = $false
                Reason      = 'unexpected probe output'
                Command     = $command
                Executable  = $Executable
                Version     = $version
                Pip         = $false
                Venv        = $false
                ErrorOutput = $text
            }
        }
        if ($reported -like '*python_embeded*' -or $Executable -like '*python_embeded*') {
            return @{
                Ok          = $false
                Reason      = 'ComfyUI embedded interpreter'
                Command     = $command
                Executable  = $reported
                Version     = $version
                Pip         = $false
                Venv        = $false
                ErrorOutput = $text
            }
        }
        if ($bits -ne '64') {
            return @{
                Ok          = $false
                Reason      = "architecture is $bits-bit"
                Command     = $command
                Executable  = $reported
                Version     = $version
                Pip         = $false
                Venv        = $false
                ErrorOutput = $text
            }
        }
        if (-not (Test-SupportedPythonVersion -Version $version)) {
            return @{
                Ok          = $false
                Reason      = "version $version is not 3.12 or newer"
                Command     = $command
                Executable  = $reported
                Version     = $version
                Pip         = $false
                Venv        = $false
                ErrorOutput = $text
            }
        }
        if ($venvState -ne 'venv-import-ok') {
            return @{
                Ok          = $false
                Reason      = 'venv module is missing'
                Command     = $command
                Executable  = $reported
                Version     = $version
                Pip         = $false
                Venv        = $false
                ErrorOutput = $text
            }
        }
        $pipCommand = "$Executable -m pip --version"
        $pipOutput = & $Executable -m pip --version 2>&1
        $pipCode = $LASTEXITCODE
        $pipText = Limit-DiscoveryText -Text ((@($pipOutput) | ForEach-Object { [string]$_ }) -join "`n")
        if ($pipCode -ne 0) {
            return @{
                Ok          = $false
                Reason      = 'pip is missing'
                Command     = $pipCommand
                Executable  = $reported
                Version     = $version
                Pip         = $false
                Venv        = $false
                ErrorOutput = $pipText
            }
        }
        if (-not $SkipVenvProbe) {
            $probeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('las-venv-probe-' + [guid]::NewGuid().ToString('n'))
            $venvCommand = "$Executable -m venv $probeRoot"
            try {
                $venvOutput = & $Executable -m venv $probeRoot 2>&1
                $venvExit = $LASTEXITCODE
                $venvExe = Join-Path (Join-Path $probeRoot 'Scripts') 'python.exe'
                $venvText = Limit-DiscoveryText -Text ((@($venvOutput) | ForEach-Object { [string]$_ }) -join "`n")
                if ($venvExit -ne 0 -or -not (Test-Path -LiteralPath $venvExe)) {
                    return @{
                        Ok          = $false
                        Reason      = "venv creation failed with exit code $venvExit"
                        Command     = $venvCommand
                        Executable  = $reported
                        Version     = $version
                        Pip         = $true
                        Venv        = $false
                        ErrorOutput = $venvText
                    }
                }
                if ($venvExe -like '*python_embeded*') {
                    return @{
                        Ok          = $false
                        Reason      = 'venv interpreter is inside python_embeded'
                        Command     = $venvCommand
                        Executable  = $reported
                        Version     = $version
                        Pip         = $true
                        Venv        = $false
                        ErrorOutput = $venvText
                    }
                }
            } finally {
                if (Test-Path -LiteralPath $probeRoot) {
                    Remove-Item -LiteralPath $probeRoot -Recurse -Force -ErrorAction SilentlyContinue
                }
            }
        }
        $absolute = $reported
        if (-not [System.IO.Path]::IsPathRooted($absolute)) {
            $absolute = [System.IO.Path]::GetFullPath($Executable)
        }
        return @{
            Ok          = $true
            Reason      = ''
            Command     = $command
            Executable  = $absolute
            Version     = $version
            Pip         = $true
            Venv        = $true
            ErrorOutput = ''
        }
    } catch {
        return @{
            Ok          = $false
            Reason      = [string]$_.Exception.Message
            Command     = $command
            Executable  = $Executable
            Version     = ''
            Pip         = $false
            Venv        = $false
            ErrorOutput = [string]$_.Exception.Message
        }
    } finally {
        if ($probeFile -and (Test-Path -LiteralPath $probeFile)) {
            Remove-Item -LiteralPath $probeFile -Force -ErrorAction SilentlyContinue
        }
        $ErrorActionPreference = $previous
        if ($restoreNative) {
            $PSNativeCommandUseErrorActionPreference = $savedNative
        }
    }
}

function Find-Python312 {
    param(
        [string]$InstallRoot,
        [string[]]$CandidatePaths,
        [scriptblock]$ProbeCommand,
        [switch]$FromCandidatePathsOnly,
        [switch]$Fresh,
        [switch]$SkipVenvProbe,
        [string]$LauncherOutput
    )
    # The launcher version switch is not used to select a runtime. When no runtime
    # is registered it prints "No suitable Python runtime found" and can abort the
    # installer under ErrorAction Stop. Inventory uses -0p only. The selected
    # runtime is always an absolute python.exe that has been executed and checked.
    if (-not $Fresh -and $null -ne $script:ResolvedPythonRuntime -and $script:ResolvedPythonRuntime.Executable) {
        return $script:ResolvedPythonRuntime
    }
    $script:PythonDiscoveryLog = New-Object System.Collections.Generic.List[string]
    $seen = @{}
    $candidates = New-Object System.Collections.Generic.List[string]
    try {
        if (-not [string]::IsNullOrWhiteSpace($InstallRoot)) {
            Add-UniquePythonCandidate -List $candidates -Seen $seen -Path (Get-StoredPythonExecutable -InstallRoot $InstallRoot)
        }
        foreach ($path in @($CandidatePaths)) {
            Add-UniquePythonCandidate -List $candidates -Seen $seen -Path ([string]$path)
        }
        if (-not $FromCandidatePathsOnly) {
            foreach ($path in @(Get-RegisteredPythonExecutables)) {
                Add-UniquePythonCandidate -List $candidates -Seen $seen -Path ([string]$path)
            }
            foreach ($path in @(Get-KnownPythonExecutables)) {
                Add-UniquePythonCandidate -List $candidates -Seen $seen -Path ([string]$path)
            }
            foreach ($path in @(Get-PathPythonExecutables)) {
                Add-UniquePythonCandidate -List $candidates -Seen $seen -Path ([string]$path)
            }
            $live = Get-LivePythonLauncherText
            if ($live -match 'No suitable Python runtime found') {
                Write-PythonDiscovery 'Python launcher reported no suitable runtime. Discovery continued without treating the launcher as an interpreter.' 'WARN'
            }
            foreach ($path in @(ConvertFrom-PythonLauncherList -Text $live)) {
                Add-UniquePythonCandidate -List $candidates -Seen $seen -Path ([string]$path)
            }
        }
        if ($PSBoundParameters.ContainsKey('LauncherOutput')) {
            if ($LauncherOutput -match 'No suitable Python runtime found') {
                Write-PythonDiscovery 'Python launcher reported no suitable runtime. Discovery continued without treating the launcher as an interpreter.' 'WARN'
                Write-PythonDiscovery (Limit-DiscoveryText -Text $LauncherOutput) 'WARN'
            }
            foreach ($path in @(ConvertFrom-PythonLauncherList -Text $LauncherOutput)) {
                Add-UniquePythonCandidate -List $candidates -Seen $seen -Path ([string]$path)
            }
        }
    } catch {
        Write-PythonDiscovery "Python discovery could not inspect the machine. $($_.Exception.Message)" 'WARN'
    }
    foreach ($candidate in @($candidates.ToArray())) {
        if ([string]::IsNullOrWhiteSpace([string]$candidate)) {
            continue
        }
        $reason = Test-RejectedPythonPath -Path $candidate -InstallRoot $InstallRoot
        if ($reason) {
            Write-PythonDiscovery "Skipping Python candidate: $candidate. Reason: $reason. It was not executed."
            continue
        }
        $probeCommandText = "$candidate <runtime probe>"
        Write-PythonDiscovery "Probing Python candidate: $candidate. Command: $probeCommandText"
        try {
            $probe = Invoke-PythonRuntimeProbe -Executable $candidate -ProbeCommand $ProbeCommand -SkipVenvProbe:$SkipVenvProbe
        } catch {
            Write-PythonDiscovery "Rejected Python candidate $candidate. Command: $probeCommandText. Error: $($_.Exception.Message)" 'WARN'
            continue
        }
        $probeReason = [string](Get-ObjectProperty -Object $probe -Name 'Reason')
        $probeCommandShown = [string](Get-ObjectProperty -Object $probe -Name 'Command')
        $probeError = [string](Get-ObjectProperty -Object $probe -Name 'ErrorOutput')
        $probeVersion = [string](Get-ObjectProperty -Object $probe -Name 'Version')
        $probeExecutable = [string](Get-ObjectProperty -Object $probe -Name 'Executable')
        $probeOk = [bool](Get-ObjectProperty -Object $probe -Name 'Ok')
        if ([string]::IsNullOrWhiteSpace($probeCommandShown)) {
            $probeCommandShown = $probeCommandText
        }
        if (-not $probeOk) {
            $suffix = ''
            if (-not [string]::IsNullOrWhiteSpace($probeError)) {
                $suffix = " Output: $(Limit-DiscoveryText -Text $probeError)"
            }
            Write-PythonDiscovery "Rejected Python candidate $candidate. Reason: $probeReason. Command: $probeCommandShown.$suffix" 'WARN'
            continue
        }
        if (-not (Test-SupportedPythonVersion -Version $probeVersion)) {
            Write-PythonDiscovery "Rejected Python candidate $candidate. Reason: version $probeVersion is not 3.12 or newer. Command: $probeCommandShown" 'WARN'
            continue
        }
        $pipValue = Get-ObjectProperty -Object $probe -Name 'Pip'
        if ($null -ne $pipValue -and -not [bool]$pipValue) {
            Write-PythonDiscovery "Rejected Python candidate $candidate. Reason: pip is missing. Command: $probeCommandShown" 'WARN'
            continue
        }
        $venvValue = Get-ObjectProperty -Object $probe -Name 'Venv'
        if ($null -ne $venvValue -and -not [bool]$venvValue) {
            Write-PythonDiscovery "Rejected Python candidate $candidate. Reason: venv creation failed. Command: $probeCommandShown" 'WARN'
            continue
        }
        if ([string]::IsNullOrWhiteSpace($probeExecutable)) {
            $probeExecutable = $candidate
        }
        $selectedReason = Test-RejectedPythonPath -Path $probeExecutable -InstallRoot $InstallRoot
        if ($selectedReason) {
            Write-PythonDiscovery "Rejected Python candidate $candidate. Reported interpreter $probeExecutable. Reason: $selectedReason. Command: $probeCommandShown" 'WARN'
            continue
        }
        $selected = @{
            Version    = $probeVersion
            Executable = $probeExecutable
            Command    = $probeCommandShown
            Pip        = $true
            Venv       = $true
        }
        $script:ResolvedPythonRuntime = $selected
        Write-PythonDiscovery "Selected Python $($selected.Version) at $($selected.Executable). Command: $($selected.Command)"
        return $selected
    }
    Write-PythonDiscovery 'No compatible Python runtime was selected.'
    return $null
}

function Resolve-InstallerPython {
    param(
        [string]$InstallRoot,
        [scriptblock]$InstallCommand,
        [string[]]$CandidatePaths,
        [scriptblock]$ProbeCommand,
        [switch]$FromCandidatePathsOnly,
        [switch]$Fresh,
        [string]$LauncherOutput
    )
    $parameters = @{
        InstallRoot = $InstallRoot
    }
    if ($CandidatePaths) {
        $parameters.CandidatePaths = $CandidatePaths
    }
    if ($ProbeCommand) {
        $parameters.ProbeCommand = $ProbeCommand
    }
    if ($FromCandidatePathsOnly) {
        $parameters.FromCandidatePathsOnly = $true
    }
    if ($Fresh) {
        $parameters.Fresh = $true
    }
    if ($PSBoundParameters.ContainsKey('LauncherOutput')) {
        $parameters.LauncherOutput = $LauncherOutput
    }
    $found = Find-Python312 @parameters
    if ($null -ne $found) {
        return @{ Installed = $false; Runtime = $found; Result = $null }
    }
    if (-not $InstallCommand) {
        return @{ Installed = $false; Runtime = $null; Result = $null }
    }
    $result = & $InstallCommand
    $runtime = $null
    if ($null -ne $script:ResolvedPythonRuntime) {
        $runtime = $script:ResolvedPythonRuntime
    }
    return @{ Installed = $true; Runtime = $runtime; Result = $result }
}

function Get-PinnedPythonDirectory {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw 'LOCALAPPDATA is not set. The pinned Python install needs a per-user directory.'
    }
    return (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312')
}

function Get-PinnedPythonInstallerArguments {
    param(
        [Parameter(Mandatory = $true)][string]$TargetDir,
        [bool]$IncludeLauncher = $true
    )
    $launcher = '0'
    if ($IncludeLauncher) {
        $launcher = '1'
    }
    $quotedTarget = '"{0}"' -f $TargetDir
    return "/quiet InstallAllUsers=0 InstallLauncherAllUsers=0 PrependPath=1 Include_launcher=$launcher Include_pip=1 Include_test=0 TargetDir=$quotedTarget"
}

function Invoke-PinnedPythonInstallerProcess {
    param(
        [Parameter(Mandatory = $true)][string]$InstallerPath,
        [Parameter(Mandatory = $true)][string]$Arguments,
        [Parameter(Mandatory = $true)][string]$ExpectedExecutable,
        [scriptblock]$InstallerCommand
    )
    Write-PythonDiscovery "Running Python installer command: $InstallerPath $Arguments"
    if ($InstallerCommand) {
        $result = & $InstallerCommand $InstallerPath $Arguments $ExpectedExecutable
        if ($null -eq $result) {
            return -1
        }
        return [int]$result
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $proc = Start-Process -FilePath $InstallerPath -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
        if ($null -eq $proc) {
            Write-PythonDiscovery 'Start-Process did not return a Python installer process.' 'ERROR'
            return -1
        }
        Write-PythonDiscovery "Python installer process exited with code $($proc.ExitCode)."
        return [int]$proc.ExitCode
    } catch {
        Write-PythonDiscovery "Python installer process failed: $($_.Exception.Message)" 'ERROR'
        return -1
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Wait-PinnedPythonExecutable {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedExecutable,
        [int]$Seconds = 10
    )
    $limit = $Seconds
    if ($limit -lt 0) {
        $limit = 0
    }
    $attempt = 0
    while ($true) {
        if (Test-Path -LiteralPath $ExpectedExecutable) {
            $item = Get-Item -LiteralPath $ExpectedExecutable -Force -ErrorAction SilentlyContinue
            if ($null -ne $item -and -not $item.PSIsContainer -and [int64]$item.Length -gt 0) {
                return $true
            }
        }
        if ($attempt -ge $limit) {
            return $false
        }
        Start-Sleep -Seconds 1
        $attempt++
    }
}

function New-PythonInstallFacts {
    param(
        [string]$DownloadUrl,
        [bool]$ChecksumVerified,
        [string]$ChecksumAlgorithm,
        $ExitCode,
        [string]$ExpectedExecutable,
        [string]$Detection,
        [string]$ErrorOutput,
        [string]$Authenticode,
        [string]$Sha256,
        [hashtable]$Extra
    )
    $facts = @{
        download_url         = $DownloadUrl
        checksum_verified    = $ChecksumVerified
        checksum_algorithm   = $ChecksumAlgorithm
        exit_code            = $ExitCode
        expected_executable  = $ExpectedExecutable
        detection            = $Detection
        error_output         = $ErrorOutput
        authenticode         = $Authenticode
        sha256               = $Sha256
    }
    if ($Extra) {
        foreach ($key in @($Extra.Keys)) {
            $facts[$key] = $Extra[$key]
        }
    }
    return $facts
}

function Complete-PinnedPythonInstall {
    param(
        [Parameter(Mandatory = $true)][string]$InstallerPath,
        [Parameter(Mandatory = $true)][string]$ExpectedExecutable,
        [Parameter(Mandatory = $true)][string]$DownloadUrl,
        [Parameter(Mandatory = $true)][bool]$ChecksumVerified,
        [string]$ChecksumAlgorithm = 'MD5',
        [string]$Checksum = '',
        [string]$Sha256 = '',
        [string]$Authenticode = '',
        [scriptblock]$InstallerCommand,
        [scriptblock]$ProbeCommand,
        [int]$ExecutableWaitSeconds = 10
    )
    $targetDir = Split-Path -Parent $ExpectedExecutable
    $repair = $false
    if (Test-Path -LiteralPath $targetDir) {
        $repair = $true
        if (-not (Test-Path -LiteralPath $ExpectedExecutable)) {
            Write-InstallLog "Python directory $targetDir is incomplete. The pinned installer will reinstall it."
        } else {
            Write-InstallLog "Repairing the Python installation at $targetDir."
        }
    } else {
        New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    }
    $checksumText = 'failed'
    if ($ChecksumVerified) {
        $checksumText = "passed ($ChecksumAlgorithm)"
    }
    $arguments = Get-PinnedPythonInstallerArguments -TargetDir $targetDir -IncludeLauncher $true
    $exitCode = Invoke-PinnedPythonInstallerProcess -InstallerPath $InstallerPath -Arguments $arguments -ExpectedExecutable $ExpectedExecutable -InstallerCommand $InstallerCommand
    $attemptText = [string]$exitCode
    $ready = Wait-PinnedPythonExecutable -ExpectedExecutable $ExpectedExecutable -Seconds $ExecutableWaitSeconds
    if (($exitCode -ne 0 -and $exitCode -ne 3010) -or -not $ready) {
        if (-not $ready) {
            $fallback = Get-PinnedPythonInstallerArguments -TargetDir $targetDir -IncludeLauncher $false
            Write-InstallLog "Python installer exit code $exitCode did not leave $ExpectedExecutable. Retrying without the launcher."
            $exitCode = Invoke-PinnedPythonInstallerProcess -InstallerPath $InstallerPath -Arguments $fallback -ExpectedExecutable $ExpectedExecutable -InstallerCommand $InstallerCommand
            $attemptText = "$attemptText,$exitCode"
            $ready = Wait-PinnedPythonExecutable -ExpectedExecutable $ExpectedExecutable -Seconds $ExecutableWaitSeconds
        }
    }
    $detection = Get-PythonDiscoveryText
    if (-not $ready) {
        if ($exitCode -eq 0 -or $exitCode -eq 3010) {
            $detail = "Python 3.12.10 installation completed, but the expected executable was not found at: $ExpectedExecutable. Download: $DownloadUrl. Download verification: $checksumText. Installer exit code: $exitCode. Detection: $detection."
        } else {
            $detail = "No compatible Python runtime found. Automatic installation failed with exit code $exitCode. Download: $DownloadUrl. Download verification: $checksumText. Expected executable: $ExpectedExecutable. Detection: $detection."
        }
        return (New-StageResult -Verified $false -Detail $detail -Facts (New-PythonInstallFacts -DownloadUrl $DownloadUrl -ChecksumVerified $ChecksumVerified -ChecksumAlgorithm $ChecksumAlgorithm -ExitCode $exitCode -ExpectedExecutable $ExpectedExecutable -Detection $detection -ErrorOutput $detection -Authenticode $Authenticode -Sha256 $Sha256 -Extra @{ attempts = $attemptText; checksum = $Checksum }))
    }
    if ($ProbeCommand) {
        $probe = Invoke-PythonRuntimeProbe -Executable $ExpectedExecutable -ProbeCommand $ProbeCommand
    } else {
        $probe = Invoke-PythonRuntimeProbe -Executable $ExpectedExecutable
    }
    $probeOk = [bool](Get-ObjectProperty -Object $probe -Name 'Ok')
    $probeVersion = [string](Get-ObjectProperty -Object $probe -Name 'Version')
    $probeExecutable = [string](Get-ObjectProperty -Object $probe -Name 'Executable')
    $shownProbeCommand = [string](Get-ObjectProperty -Object $probe -Name 'Command')
    $probeReason = [string](Get-ObjectProperty -Object $probe -Name 'Reason')
    $probeError = [string](Get-ObjectProperty -Object $probe -Name 'ErrorOutput')
    $actual = "version $($probeVersion) at $probeExecutable. Reason: $probeReason. Command: $shownProbeCommand. Output: $(Limit-DiscoveryText -Text $probeError)"
    if (-not $probeOk -or $probeVersion -notlike '3.12.10*') {
        $detail = "No compatible Python runtime found. Automatic installation failed with exit code $exitCode. The executable at $ExpectedExecutable failed validation ($actual). Download: $DownloadUrl. Download verification: $checksumText. Expected executable: $ExpectedExecutable."
        return (New-StageResult -Verified $false -Detail $detail -Facts (New-PythonInstallFacts -DownloadUrl $DownloadUrl -ChecksumVerified $ChecksumVerified -ChecksumAlgorithm $ChecksumAlgorithm -ExitCode $exitCode -ExpectedExecutable $ExpectedExecutable -Detection $actual -ErrorOutput $probeError -Authenticode $Authenticode -Sha256 $Sha256 -Extra @{ attempts = $attemptText }))
    }
    if (-not (Test-SamePath -Left $probeExecutable -Right $ExpectedExecutable)) {
        $detail = "Python 3.12.10 installation completed, but the expected executable was not found at: $ExpectedExecutable. Detection reported $probeExecutable. Download: $DownloadUrl. Download verification: $checksumText. Installer exit code: $exitCode."
        return (New-StageResult -Verified $false -Detail $detail -Facts (New-PythonInstallFacts -DownloadUrl $DownloadUrl -ChecksumVerified $ChecksumVerified -ChecksumAlgorithm $ChecksumAlgorithm -ExitCode $exitCode -ExpectedExecutable $ExpectedExecutable -Detection $actual -ErrorOutput $probeError -Authenticode $Authenticode -Sha256 $Sha256))
    }
    if (-not $InstallerCommand) {
        Update-SessionPath
        Write-InstallLog "Session PATH was refreshed. Python commands in this install use $probeExecutable."
    }
    $script:ResolvedPythonRuntime = @{
        Version    = $probeVersion
        Executable = $probeExecutable
        Command    = $shownProbeCommand
        Pip        = $true
        Venv       = $true
    }
    $action = 'installed'
    if ($repair) {
        $action = 'repaired'
    }
    Write-InstallLog "Python version command: $shownProbeCommand reported $probeVersion."
    return (New-StageResult -Verified $true -Detail 'Python 3.12.10 installed and the version command succeeded.' -Facts @{
            action              = $action
            version             = $probeVersion
            executable          = $probeExecutable
            command             = $shownProbeCommand
            authenticode        = $Authenticode
            md5_verified        = $true
            sha256              = $Sha256
            download_url        = $DownloadUrl
            exit_code           = $exitCode
            expected_executable = $ExpectedExecutable
            checksum_verified   = $true
        })
}

function Install-PinnedPythonPackage {
    param(
        [Parameter(Mandatory = $true)]$Pins,
        [Parameter(Mandatory = $true)][string]$InstallerPath,
        [switch]$DryRun,
        [scriptblock]$DownloadCommand,
        [scriptblock]$SignatureCommand,
        [scriptblock]$InstallerCommand,
        [scriptblock]$ProbeCommand,
        [int]$ExecutableWaitSeconds = 10,
        [string]$ExpectedExecutable
    )
    $pythonPin = Get-PythonPin -Pins $Pins
    $downloadUrl = [string](Get-ObjectProperty -Object $pythonPin -Name 'url')
    $algorithm = [string](Get-ObjectProperty -Object $pythonPin -Name 'algorithm')
    $checksum = [string](Get-ObjectProperty -Object $pythonPin -Name 'checksum')
    $subject = [string](Get-ObjectProperty -Object $pythonPin -Name 'authenticode_subject')
    $sha256 = [string](Get-ObjectProperty -Object $pythonPin -Name 'sha256')
    $expectedBytes = 0
    $bytesValue = Get-ObjectProperty -Object $pythonPin -Name 'expected_bytes'
    if ($null -ne $bytesValue -and [int64]$bytesValue -gt 0) {
        $expectedBytes = [int64]$bytesValue
    }
    if ([string]::IsNullOrWhiteSpace($ExpectedExecutable)) {
        $ExpectedExecutable = Join-Path (Get-PinnedPythonDirectory) 'python.exe'
    }
    try {
        if ($DownloadCommand) {
            $saved = [bool](& $DownloadCommand $downloadUrl $InstallerPath)
        } else {
            $saved = [bool](Save-PinnedFile -Url $downloadUrl -Destination $InstallerPath -Algorithm $algorithm -Checksum $checksum -ExpectedBytes $expectedBytes -Label 'Python 3.12.10' -DryRun:$DryRun)
        }
    } catch {
        $message = [string]$_.Exception.Message
        $detail = "No compatible Python runtime found. Automatic installation failed. Download verification failed for $downloadUrl. $message"
        return (New-StageResult -Verified $false -Detail $detail -Facts (New-PythonInstallFacts -DownloadUrl $downloadUrl -ChecksumVerified $false -ChecksumAlgorithm $algorithm -ExitCode $null -ExpectedExecutable $ExpectedExecutable -Detection (Get-PythonDiscoveryText) -ErrorOutput $message -Sha256 $sha256))
    }
    if (-not $saved) {
        return (New-StageResult -Verified $false -Detail 'dry run' -Facts @{ Skipped = 'dry-run' })
    }
    if (-not [string]::IsNullOrWhiteSpace($sha256)) {
        $shaOk = Test-PinnedFileHash -Path $InstallerPath -Algorithm 'SHA256' -Checksum $sha256 -ExpectedBytes 0
        if (-not $shaOk) {
            $detail = "No compatible Python runtime found. Automatic installation failed. SHA256 verification failed for $downloadUrl."
            return (New-StageResult -Verified $false -Detail $detail -Facts (New-PythonInstallFacts -DownloadUrl $downloadUrl -ChecksumVerified $false -ChecksumAlgorithm 'SHA256' -ExitCode $null -ExpectedExecutable $ExpectedExecutable -Detection (Get-PythonDiscoveryText) -ErrorOutput "SHA256 verification failed for $InstallerPath" -Sha256 $sha256))
        }
        Write-InstallLog 'Python installer SHA256 verified.'
    }
    if ($SignatureCommand) {
        $signature = & $SignatureCommand $InstallerPath
    } else {
        $signature = Test-Authenticode -Path $InstallerPath -SubjectLike $subject
    }
    $status = [string](Get-ObjectProperty -Object $signature -Name 'Status')
    $trusted = [bool](Get-ObjectProperty -Object $signature -Name 'Trusted')
    Write-InstallLog "Python installer Authenticode status $status."
    if (-not $trusted) {
        $detail = "The Python installer signature is $status. Download: $downloadUrl. Download verification: passed ($algorithm). Expected executable: $ExpectedExecutable."
        return (New-StageResult -Verified $false -Detail $detail -Facts (New-PythonInstallFacts -DownloadUrl $downloadUrl -ChecksumVerified $true -ChecksumAlgorithm $algorithm -ExitCode $null -ExpectedExecutable $ExpectedExecutable -Detection (Get-PythonDiscoveryText) -ErrorOutput $status -Authenticode $status -Sha256 $sha256))
    }
    return (Complete-PinnedPythonInstall -InstallerPath $InstallerPath -ExpectedExecutable $ExpectedExecutable -DownloadUrl $downloadUrl -ChecksumVerified $true -ChecksumAlgorithm $algorithm -Checksum $checksum -Sha256 $sha256 -Authenticode $status -InstallerCommand $InstallerCommand -ProbeCommand $ProbeCommand -ExecutableWaitSeconds $ExecutableWaitSeconds)
}

function Test-GatewayVirtualEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$VenvPython,
        [scriptblock]$ProbeCommand
    )
    if ($VenvPython -like '*python_embeded*') {
        return @{ Ok = $false; Detail = 'The gateway interpreter is inside ComfyUI python_embeded.'; Version = '' }
    }
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        return @{ Ok = $false; Detail = "The gateway virtual environment Python was not found at $VenvPython."; Version = '' }
    }
    $probe = Invoke-PythonRuntimeProbe -Executable $VenvPython -ProbeCommand $ProbeCommand -SkipVenvProbe
    $probeOk = [bool](Get-ObjectProperty -Object $probe -Name 'Ok')
    $version = [string](Get-ObjectProperty -Object $probe -Name 'Version')
    $reason = [string](Get-ObjectProperty -Object $probe -Name 'Reason')
    $command = [string](Get-ObjectProperty -Object $probe -Name 'Command')
    if (-not $probeOk) {
        return @{ Ok = $false; Detail = "The gateway virtual environment failed validation. $reason Command: $command"; Version = $version }
    }
    return @{ Ok = $true; Detail = "$version $VenvPython"; Version = $version }
}

function Get-GatewayVenvPlan {
    param(
        [Parameter(Mandatory = $true)][string]$VenvPython,
        [scriptblock]$ProbeCommand
    )
    if (Test-Path -LiteralPath $VenvPython) {
        $check = Test-GatewayVirtualEnvironment -VenvPython $VenvPython -ProbeCommand $ProbeCommand
        if ($check.Ok) {
            return @{ Action = 'reuse'; Ok = $true; Detail = [string]$check.Detail }
        }
        return @{ Action = 'invalid'; Ok = $false; Detail = [string]$check.Detail }
    }
    $scriptsDir = Split-Path -Parent $VenvPython
    $venvRoot = Split-Path -Parent $scriptsDir
    if (Test-Path -LiteralPath $venvRoot) {
        return @{ Action = 'blocked'; Ok = $false; Detail = 'The venv directory exists without Scripts\python.exe. It was left unchanged.' }
    }
    return @{ Action = 'create'; Ok = $true; Detail = '' }
}

function Invoke-GatewayVenvCreate {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExecutable,
        [Parameter(Mandatory = $true)][string]$VenvPython,
        [scriptblock]$Runner
    )
    $scriptsDir = Split-Path -Parent $VenvPython
    $venvRoot = Split-Path -Parent $scriptsDir
    $command = '"{0}" -m venv "{1}"' -f $PythonExecutable, $venvRoot
    $rejected = Test-RejectedPythonPath -Path $PythonExecutable
    if ($rejected) {
        return @{ Ok = $false; Detail = "Refusing to build the gateway virtual environment with $PythonExecutable. Reason: $rejected."; Command = $command }
    }
    if ($PythonExecutable -like '*python_embeded*') {
        return @{ Ok = $false; Detail = 'Refusing to build the gateway virtual environment with the ComfyUI embedded interpreter.'; Command = $command }
    }
    Write-InstallLog "Creating gateway virtual environment with: $command"
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Runner) {
            $result = & $Runner $PythonExecutable $venvRoot
            if ($null -eq $result) {
                $exitCode = 0
            } else {
                $exitCode = [int]$result
            }
        } else {
            & $PythonExecutable -m venv $venvRoot
            $exitCode = $LASTEXITCODE
        }
    } catch {
        return @{ Ok = $false; Detail = "The gateway virtual environment was not created. Command: $command. Error: $($_.Exception.Message)"; Command = $command }
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        return @{ Ok = $false; Detail = "The gateway virtual environment was not created. Command: $command. Exit code: $exitCode."; Command = $command }
    }
    if ($VenvPython -like '*python_embeded*') {
        return @{ Ok = $false; Detail = 'The gateway interpreter is inside ComfyUI python_embeded.'; Command = $command }
    }
    return @{ Ok = $true; Detail = $command; Command = $command }
}

function Find-OllamaExecutable {
    $candidates = @()
    $command = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        $command = Get-Command ollama -ErrorAction SilentlyContinue
    }
    if ($command) {
        $candidates += $command.Source
    }
    $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe')
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate)) {
            continue
        }
        $output = & $candidate --version 2>&1
        if ($LASTEXITCODE -ne 0) {
            continue
        }
        $text = (@($output) -join ' ')
        if ($text -match '(\d+\.\d+\.\d+)') {
            return @{
                Executable = $candidate
                Version    = $Matches[1]
                Output     = $text.Trim()
            }
        }
    }
    return $null
}

function Test-Authenticode {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$SubjectLike,
        [string]$Organization
    )
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    $subject = ''
    if ($signature.SignerCertificate) {
        $subject = [string]$signature.SignerCertificate.Subject
    }
    $status = [string]$signature.Status
    $trusted = $status -eq 'Valid'
    if ($trusted -and $SubjectLike -and $subject -notlike "*$SubjectLike*") {
        $trusted = $false
    }
    if ($trusted -and $Organization) {
        $pattern = '(^|, )O=' + [regex]::Escape($Organization) + '(,|$)'
        if ($subject -notmatch $pattern) {
            $trusted = $false
        }
    }
    return @{
        Status  = $status
        Subject = $subject
        Trusted = $trusted
    }
}

function Restore-FileLength {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][int64]$Length
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Write)
    try {
        $stream.SetLength($Length)
    } finally {
        $stream.Close()
    }
}

function Receive-PinnedHttpFile {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Destination,
        [switch]$Resume,
        [int64]$ExpectedBytes = 0
    )
    $startLength = [int64]0
    if ($Resume -and (Test-Path -LiteralPath $Destination)) {
        $startLength = [int64](Get-Item -LiteralPath $Destination).Length
    }
    $request = [System.Net.HttpWebRequest][System.Net.WebRequest]::Create($Url)
    $request.UserAgent = 'LocalAISwitchboardInstaller'
    $request.AllowAutoRedirect = $true
    if ($startLength -gt 0) {
        $request.AddRange($startLength)
    }
    $response = $null
    try {
        try {
            $response = $request.GetResponse()
        } catch [System.Net.WebException] {
            $errorResponse = $_.Exception.Response
            $statusCode = 0
            if ($errorResponse) {
                $statusCode = [int]$errorResponse.StatusCode
                $errorResponse.Close()
            }
            if ($statusCode -eq 416) {
                return @{ Complete = $false; RangeRejected = $true; StartLength = $startLength }
            }
            return @{ Complete = $false; RangeRejected = $false; StartLength = $startLength }
        }
        $statusCode = [int]$response.StatusCode
        if ($startLength -gt 0 -and $statusCode -ne 206) {
            return @{ Complete = $false; RangeRejected = $true; StartLength = $startLength }
        }
        $mode = [System.IO.FileMode]::Create
        if ($startLength -gt 0 -and $statusCode -eq 206) {
            $mode = [System.IO.FileMode]::Append
        }
        $inputStream = $response.GetResponseStream()
        $outputStream = [System.IO.File]::Open($Destination, $mode, [System.IO.FileAccess]::Write)
        try {
            $inputStream.CopyTo($outputStream)
        } catch {
            return @{ Complete = $false; RangeRejected = $false; StartLength = $startLength }
        } finally {
            $outputStream.Close()
            $inputStream.Close()
        }
    } finally {
        if ($response) {
            $response.Close()
        }
    }
    if ($ExpectedBytes -gt 0 -and (Test-Path -LiteralPath $Destination)) {
        $length = [int64](Get-Item -LiteralPath $Destination).Length
        if ($startLength -gt 0 -and $length -gt $ExpectedBytes) {
            Restore-FileLength -Path $Destination -Length $startLength
            return @{ Complete = $false; RangeRejected = $true; StartLength = $startLength }
        }
    }
    return @{ Complete = $true; RangeRejected = $false; StartLength = $startLength }
}

function Invoke-PinnedTransfer {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Destination,
        [switch]$Resume,
        [int64]$ExpectedBytes = 0
    )
    $startLength = [int64]0
    if ($Resume -and (Test-Path -LiteralPath $Destination)) {
        $startLength = [int64](Get-Item -LiteralPath $Destination).Length
    }
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) {
        return (Receive-PinnedHttpFile -Url $Url -Destination $Destination -Resume:$Resume -ExpectedBytes $ExpectedBytes)
    }
    $argumentList = @(
        '--fail', '--location', '--retry', '3', '--retry-delay', '2',
        '--silent', '--show-error', '--output', $Destination
    )
    if ($startLength -gt 0) {
        $argumentList += @('--continue-at', '-')
    }
    $argumentList += $Url
    $output = & curl.exe @argumentList 2>&1
    $code = $LASTEXITCODE
    $text = (@($output) | ForEach-Object { [string]$_ }) -join "`n"
    $rangeRejected = $code -eq 33 -or $text -match '(?i)(byte ranges|416)'
    if ($rangeRejected) {
        if ($startLength -gt 0) {
            Restore-FileLength -Path $Destination -Length $startLength
        }
        return @{ Complete = $false; RangeRejected = $true; StartLength = $startLength }
    }
    if ($code -ne 0) {
        return @{ Complete = $false; RangeRejected = $false; StartLength = $startLength }
    }
    if ($ExpectedBytes -gt 0 -and (Test-Path -LiteralPath $Destination)) {
        $length = [int64](Get-Item -LiteralPath $Destination).Length
        if ($startLength -gt 0 -and $length -gt $ExpectedBytes) {
            Restore-FileLength -Path $Destination -Length $startLength
            return @{ Complete = $false; RangeRejected = $true; StartLength = $startLength }
        }
    }
    return @{ Complete = $true; RangeRejected = $false; StartLength = $startLength }
}

function Save-ReplacementDownload {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Partial,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Algorithm,
        [Parameter(Mandatory = $true)][string]$Checksum,
        [int64]$ExpectedBytes = 0,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $replacement = "$Destination.full"
    if (Test-Path -LiteralPath $replacement) {
        Remove-Item -LiteralPath $replacement -Force
    }
    $second = Invoke-PinnedTransfer -Url $Url -Destination $replacement -ExpectedBytes $ExpectedBytes
    if (-not $second.Complete) {
        if (Test-Path -LiteralPath $replacement) {
            Remove-Item -LiteralPath $replacement -Force -ErrorAction SilentlyContinue
        }
        throw "The download of $Label stopped. The partial file was kept at $Partial."
    }
    if (-not (Test-PinnedFileHash -Path $replacement -Algorithm $Algorithm -Checksum $Checksum -ExpectedBytes $ExpectedBytes)) {
        Remove-Item -LiteralPath $replacement -Force -ErrorAction SilentlyContinue
        throw "The checksum of $Label does not match the pin. The partial file was kept at $Partial."
    }
    if (Test-Path -LiteralPath $Partial) {
        Remove-Item -LiteralPath $Partial -Force
    }
    Move-Item -LiteralPath $replacement -Destination $Destination
    Write-InstallLog "$Label size and $Algorithm checksum verified."
    return $true
}

function Save-PinnedFile {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Algorithm,
        [Parameter(Mandatory = $true)][string]$Checksum,
        [int64]$ExpectedBytes = 0,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$DryRun
    )
    $expected = $Checksum.ToLowerInvariant()
    if ($DryRun) {
        Write-InstallLog "Would download $Label from $Url and require $Algorithm $expected."
        return $false
    }
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    if (Test-Path -LiteralPath $Destination) {
        if (Test-PinnedFileHash -Path $Destination -Algorithm $Algorithm -Checksum $expected -ExpectedBytes $ExpectedBytes) {
            Write-InstallLog "$Label is already present and the checksum matches."
            return $true
        }
        throw "An existing $Label file does not match the pinned checksum. It was left in place."
    }
    $partial = "$Destination.partial"
    if (Test-Path -LiteralPath $partial) {
        if (Test-PinnedFileHash -Path $partial -Algorithm $Algorithm -Checksum $expected -ExpectedBytes $ExpectedBytes) {
            Move-Item -LiteralPath $partial -Destination $Destination
            Write-InstallLog "$Label size and $Algorithm checksum verified."
            return $true
        }
        $partialLength = [int64](Get-Item -LiteralPath $partial).Length
        if ($ExpectedBytes -gt 0 -and $partialLength -ge $ExpectedBytes) {
            Write-InstallLog "$Label partial is $partialLength bytes and does not match the pin. It cannot be resumed." 'WARN'
            Remove-Item -LiteralPath $partial -Force
        }
    }
    $resume = Test-Path -LiteralPath $partial
    if ($resume) {
        $resumeLength = [int64](Get-Item -LiteralPath $partial).Length
        Write-InstallLog "Resuming $Label from $resumeLength bytes with an HTTP range request."
    } else {
        Write-InstallLog "Downloading $Label from $Url"
    }
    $transfer = Invoke-PinnedTransfer -Url $Url -Destination $partial -Resume:$resume -ExpectedBytes $ExpectedBytes
    if ($transfer.RangeRejected) {
        Write-InstallLog "The server did not honor a ranged resume for $Label. The partial file was kept."
        return (Save-ReplacementDownload -Url $Url -Partial $partial -Destination $Destination -Algorithm $Algorithm -Checksum $expected -ExpectedBytes $ExpectedBytes -Label $Label)
    }
    if (-not $transfer.Complete) {
        throw "The download of $Label stopped. The partial file was kept at $partial."
    }
    if (-not (Test-PinnedFileHash -Path $partial -Algorithm $Algorithm -Checksum $expected -ExpectedBytes $ExpectedBytes)) {
        if ($resume -and [int64]$transfer.StartLength -gt 0) {
            Restore-FileLength -Path $partial -Length ([int64]$transfer.StartLength)
            Write-InstallLog "The resumed $Label file did not match the pin. The partial file was kept and one full download will be tried."
            return (Save-ReplacementDownload -Url $Url -Partial $partial -Destination $Destination -Algorithm $Algorithm -Checksum $expected -ExpectedBytes $ExpectedBytes -Label $Label)
        }
        Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        throw "The checksum of $Label does not match the pin."
    }
    Move-Item -LiteralPath $partial -Destination $Destination
    Write-InstallLog "$Label size and $Algorithm checksum verified."
    return $true
}

function Test-PinnedFileHash {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Algorithm,
        [Parameter(Mandatory = $true)][string]$Checksum,
        [int64]$ExpectedBytes = 0
    )
    $item = Get-Item -LiteralPath $Path
    if ($ExpectedBytes -gt 0 -and [int64]$item.Length -ne $ExpectedBytes) {
        Write-InstallLog "$Path is $([int64]$item.Length) bytes. The pin expects $ExpectedBytes bytes." 'ERROR'
        return $false
    }
    $hash = (Get-FileHash -LiteralPath $Path -Algorithm $Algorithm).Hash.ToLowerInvariant()
    if ($hash -ne $Checksum.ToLowerInvariant()) {
        Write-InstallLog "Checksum mismatch for $Path." 'ERROR'
        return $false
    }
    return $true
}

function Expand-PinnedArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Archive,
        [Parameter(Mandatory = $true)][string]$Destination,
        [switch]$DryRun
    )
    if ($DryRun) {
        Write-InstallLog "Would extract $Archive into $Destination."
        return $false
    }
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
    if ($tar) {
        & tar.exe -xf $Archive -C $Destination
        if ($LASTEXITCODE -eq 0 -and (Find-ComfyPortableRoot -Path $Destination)) {
            return $true
        }
        Write-InstallLog 'The Windows tar command could not extract the archive.' 'WARN'
        Remove-Item -LiteralPath $Destination -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    }
    $sevenZipPath = Find-SevenZipExecutable
    if (-not $sevenZipPath) {
        $pin = (Get-InstallerPins).seven_zip
        $sevenZipPath = Join-Path (Split-Path -Parent $Archive) '7zr.exe'
        Write-InstallLog 'tar.exe and an installed 7z.exe are unavailable. Downloading the pinned 7zr.exe extractor.'
        $saved = Save-PinnedFile -Url ([string]$pin.url) -Destination $sevenZipPath -Algorithm ([string]$pin.algorithm) -Checksum ([string]$pin.checksum) -ExpectedBytes ([int64]$pin.expected_bytes) -Label '7zr.exe'
        if (-not $saved) {
            throw 'The pinned 7zr.exe extractor was not downloaded. The ComfyUI archive was not extracted.'
        }
    }
    & $sevenZipPath x "-o$Destination" -y $Archive
    if ($LASTEXITCODE -ne 0) {
        throw 'The 7z extractor could not extract the ComfyUI archive.'
    }
    return $true
}

function Find-SevenZipExecutable {
    $command = Get-Command 7z.exe -ErrorAction SilentlyContinue
    if ($command) {
        return [string]$command.Source
    }
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($env:ProgramFiles) {
        $candidates.Add((Join-Path $env:ProgramFiles '7-Zip\7z.exe'))
    }
    $programFilesX86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
    if ($programFilesX86) {
        $candidates.Add((Join-Path $programFilesX86 '7-Zip\7z.exe'))
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    return $null
}

function Test-RawComfyApiExport {
    param($Payload)
    if ($null -eq $Payload) {
        return $false
    }
    if ($Payload -is [System.Array]) {
        return $false
    }
    $names = @($Payload.PSObject.Properties.Name)
    if ($names -contains 'id' -or $names -contains 'workflow' -or $names -contains 'bindings') {
        return $false
    }
    foreach ($property in @($Payload.PSObject.Properties)) {
        $value = $property.Value
        if ($null -eq $value) {
            continue
        }
        $valueNames = @($value.PSObject.Properties.Name)
        if ($valueNames -contains 'class_type') {
            return $true
        }
    }
    return $false
}

function Get-WorkflowCheckerCode {
    $lines = @(
        'import sys'
        'from pathlib import Path'
        'from local_ai_switchboard.providers.workflows import load_manifest'
        ''
        'manifest = load_manifest(Path(sys.argv[1]))'
        'sys.stdout.write(''LAS_WORKFLOW_ID='' + manifest.id + ''\n'')'
        'sys.stdout.write(''LAS_WORKFLOW_STATE='' + (''enabled'' if manifest.enabled else ''disabled'') + ''\n'')'
        'sys.stdout.write(''LAS_WORKFLOW_GRAPH='' + (''yes'' if manifest.workflow else ''no'') + ''\n'')'
    )
    return ($lines -join "`n")
}

function Get-WorkflowManifestState {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$ManifestPath
    )
    $output = & $Python -c (Get-WorkflowCheckerCode) $ManifestPath 2>&1
    $id = ''
    $state = ''
    $graph = ''
    foreach ($line in @($output)) {
        $text = [string]$line
        if ($text -like 'LAS_WORKFLOW_ID=*') {
            $id = $text.Substring('LAS_WORKFLOW_ID='.Length)
        }
        if ($text -like 'LAS_WORKFLOW_STATE=*') {
            $state = $text.Substring('LAS_WORKFLOW_STATE='.Length)
        }
        if ($text -like 'LAS_WORKFLOW_GRAPH=*') {
            $graph = $text.Substring('LAS_WORKFLOW_GRAPH='.Length)
        }
    }
    $detail = ((@($output) | ForEach-Object { [string]$_ }) -join ' ').Trim()
    return @{
        Ok     = ($LASTEXITCODE -eq 0 -and $id -ne '')
        Id     = $id
        State  = $state
        Graph  = $graph
        Detail = $detail
    }
}

function Find-ComfyPortableRoot {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    $directPython = Join-Path $Path 'python_embeded\python.exe'
    $directMain = Join-Path $Path 'ComfyUI\main.py'
    if ((Test-Path -LiteralPath $directPython) -and (Test-Path -LiteralPath $directMain)) {
        return (Resolve-Path -LiteralPath $Path).Path
    }
    $matches = @(Get-ChildItem -LiteralPath $Path -Recurse -Filter python.exe -ErrorAction SilentlyContinue | Where-Object {
            $_.Directory.Name -eq 'python_embeded'
        })
    if ($matches.Count -ne 1) {
        return $null
    }
    $root = $matches[0].Directory.Parent.FullName
    $main = Join-Path $root 'ComfyUI\main.py'
    if (-not (Test-Path -LiteralPath $main)) {
        return $null
    }
    return $root
}

function Get-LanIPv4Addresses {
    $addresses = @()
    try {
        $routes = @(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop | Sort-Object RouteMetric)
        $all = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Where-Object {
                $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*'
            })
    } catch {
        Write-InstallLog 'A LAN address could not be queried.' 'WARN'
        return @()
    }
    foreach ($route in $routes) {
        foreach ($address in $all) {
            if ($address.InterfaceIndex -eq $route.InterfaceIndex -and $addresses -notcontains $address.IPAddress) {
                $addresses += $address.IPAddress
            }
        }
    }
    foreach ($address in $all) {
        if ($addresses -notcontains $address.IPAddress) {
            $addresses += $address.IPAddress
        }
    }
    return $addresses
}

function Test-HttpStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSeconds = 5
    )
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSeconds
        return [int]$response.StatusCode
    } catch {
        $response = $_.Exception.Response
        if ($response -and $response.StatusCode) {
            return [int]$response.StatusCode
        }
        return 0
    }
}

function Wait-HttpStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int[]]$Accept,
        [int]$TimeoutSeconds = 30
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $status = Test-HttpStatus -Url $Url -TimeoutSeconds 5
        if ($Accept -contains $status) {
            return $status
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    return 0
}

function Get-ConfiguredApiPort {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [int]$DefaultPort = 8787
    )
    $envFile = Join-Path $InstallRoot '.env'
    if (-not (Test-Path -LiteralPath $envFile)) {
        return $DefaultPort
    }
    foreach ($line in @(Get-Content -LiteralPath $envFile)) {
        if ($line -match '^\s*LAS_API_PORT\s*=\s*([0-9]+)\s*$') {
            return [int]$Matches[1]
        }
    }
    return $DefaultPort
}

function Get-FirewallRuleName {
    param([Parameter(Mandatory = $true)][int]$ApiPort)
    return "LocalAISwitchboard-$ApiPort"
}

function Get-GatewayFirewallState {
    param([Parameter(Mandatory = $true)][int]$ApiPort)
    $name = Get-FirewallRuleName -ApiPort $ApiPort
    try {
        $rule = Get-NetFirewallRule -Name $name -ErrorAction Stop
    } catch {
        return @{ Present = $false; Verified = $false; Detail = 'The firewall rule is absent.' }
    }
    $portFilter = $rule | Get-NetFirewallPortFilter
    $addressFilter = $rule | Get-NetFirewallAddressFilter
    $profileText = [string]$rule.Profile
    $remote = [string]$addressFilter.RemoteAddress
    $localPort = [string]$portFilter.LocalPort
    $matches = $rule.Direction -eq 'Inbound' -and
        $rule.Action -eq 'Allow' -and
        $rule.Enabled -eq 'True' -and
        $portFilter.Protocol -eq 'TCP' -and
        $localPort -eq [string]$ApiPort -and
        ($profileText -eq 'Private' -or $profileText -eq '2') -and
        $remote -eq 'LocalSubnet'
    $detail = "profile=$profileText remote=$remote port=$localPort"
    return @{
        Present  = $true
        Verified = [bool]$matches
        Detail   = $detail
        Name     = $name
    }
}

function Install-GatewayFirewall {
    param(
        [Parameter(Mandatory = $true)][int]$ApiPort,
        [switch]$DryRun
    )
    $existing = Get-GatewayFirewallState -ApiPort $ApiPort
    if ($existing.Present -and $existing.Verified) {
        Write-InstallLog 'The private LocalSubnet firewall rule is already present.'
        return (New-StageResult -Verified $true -Detail $existing.Detail -Facts @{ action = 'kept'; name = $existing.Name })
    }
    if ($existing.Present -and -not $existing.Verified) {
        Write-InstallLog "The existing firewall rule was left unchanged. $($existing.Detail)" 'ERROR'
        return (New-StageResult -Verified $false -Detail $existing.Detail -Facts @{ action = 'left-unchanged' })
    }
    if ($DryRun) {
        Write-InstallLog "Would add an inbound Private LocalSubnet TCP rule for port $ApiPort."
        return (New-StageResult -Verified $false -Detail 'dry run' -Facts @{ action = 'dry-run' })
    }
    if (-not (Test-IsAdministrator)) {
        return (New-StageResult -Verified $false -Detail 'Administrator approval is required for the firewall rule.' -Facts @{ action = 'needs-admin' })
    }
    try {
        $ruleName = Get-FirewallRuleName -ApiPort $ApiPort
        New-NetFirewallRule -Name $ruleName -DisplayName 'Local AI Switchboard' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $ApiPort -Profile Private -RemoteAddress LocalSubnet | Out-Null
    } catch {
        Write-InstallLog 'The firewall rule could not be created.' 'ERROR'
        return (New-StageResult -Verified $false -Detail 'The firewall command failed.' -Facts @{ action = 'failed' })
    }
    $created = Get-GatewayFirewallState -ApiPort $ApiPort
    if (-not $created.Verified) {
        Write-InstallLog "The firewall rule was queried and does not match the required scope. $($created.Detail)" 'ERROR'
        return (New-StageResult -Verified $false -Detail $created.Detail -Facts @{ action = 'query-mismatch' })
    }
    Write-InstallLog "Firewall rule verified. $($created.Detail)"
    return (New-StageResult -Verified $true -Detail $created.Detail -Facts @{ action = 'created'; name = (Get-FirewallRuleName -ApiPort $ApiPort) })
}

function Remove-GatewayFirewall {
    param(
        [int]$ApiPort = 8787,
        [switch]$DryRun
    )
    $name = Get-FirewallRuleName -ApiPort $ApiPort
    if ($DryRun) {
        Write-InstallLog "Would remove firewall rule $name."
        return
    }
    if (-not (Test-IsAdministrator)) {
        throw 'Administrator approval is required to remove the firewall rule.'
    }
    try {
        $rule = Get-NetFirewallRule -Name $name -ErrorAction Stop
    } catch {
        $rule = $null
    }
    if ($null -eq $rule) {
        Write-InstallLog 'The firewall rule is already absent.'
        return
    }
    Remove-NetFirewallRule -Name $name
    try {
        $still = Get-NetFirewallRule -Name $name -ErrorAction Stop
    } catch {
        $still = $null
    }
    if ($null -ne $still) {
        throw 'The firewall rule is still present after removal.'
    }
    Write-InstallLog 'Removed the Local AI Switchboard firewall rule.'
}

function Get-GatewayTaskState {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $taskName = 'Local AI Switchboard'
    try {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    } catch {
        return @{ Present = $false; Verified = $false; Detail = 'The logon task is absent.' }
    }
    $action = @($task.Actions)[0]
    $execute = [string]$action.Execute
    $arguments = [string]$action.Arguments
    $work = [string]$action.WorkingDirectory
    $expectedScript = Join-Path $InstallRoot 'service\start.ps1'
    $scriptMatches = $arguments -like "*$expectedScript*"
    $unsafe = $arguments -match 'python_embeded|--listen|ComfyUI\\main\.py'
    $restart = 0
    if ($task.Settings -and $null -ne $task.Settings.RestartCount) {
        $restart = [int]$task.Settings.RestartCount
    }
    $workMatches = $false
    try {
        $left = [System.IO.Path]::GetFullPath($work).TrimEnd('\')
        $right = [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
        $workMatches = $left.Equals($right, [System.StringComparison]::OrdinalIgnoreCase)
    } catch {
        $workMatches = $false
    }
    $verified = $scriptMatches -and -not $unsafe -and $workMatches -and $restart -ge 1
    $detail = "execute=$execute restart=$restart"
    return @{
        Present  = $true
        Verified = [bool]$verified
        Detail   = $detail
        Task     = $task
    }
}

function Install-GatewayLogonTask {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [switch]$DryRun
    )
    $existing = Get-GatewayTaskState -InstallRoot $InstallRoot
    if ($existing.Present -and $existing.Verified) {
        Write-InstallLog 'The per-user logon task already matches this install.'
        return (New-StageResult -Verified $true -Detail $existing.Detail -Facts @{ action = 'kept' })
    }
    if ($existing.Present -and -not $existing.Verified) {
        Write-InstallLog "An existing scheduled task was left unchanged. $($existing.Detail)" 'ERROR'
        return (New-StageResult -Verified $false -Detail $existing.Detail -Facts @{ action = 'left-unchanged' })
    }
    if ($DryRun) {
        Write-InstallLog 'Would register a per-user logon task for the gateway.'
        return (New-StageResult -Verified $false -Detail 'dry run' -Facts @{ action = 'dry-run' })
    }
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $startScript = Join-Path $InstallRoot 'service\start.ps1'
    $user = Get-CurrentUserName
    $action = New-ScheduledTaskAction -Execute $powershell -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`" -InstallRoot `"$InstallRoot`"" -WorkingDirectory $InstallRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    try {
        Register-ScheduledTask -TaskName 'Local AI Switchboard' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Starts the Local AI Switchboard gateway at logon. ComfyUI stays stopped until the gateway needs it.' | Out-Null
    } catch {
        Write-InstallLog 'The logon task could not be registered.' 'ERROR'
        return (New-StageResult -Verified $false -Detail 'Register-ScheduledTask failed.' -Facts @{ action = 'failed' })
    }
    $created = Get-GatewayTaskState -InstallRoot $InstallRoot
    if (-not $created.Verified) {
        Write-InstallLog "The scheduled task was queried and does not match the expected action. $($created.Detail)" 'ERROR'
        return (New-StageResult -Verified $false -Detail $created.Detail -Facts @{ action = 'query-mismatch' })
    }
    Write-InstallLog 'The per-user logon task was registered and queried back.'
    return (New-StageResult -Verified $true -Detail $created.Detail -Facts @{ action = 'created' })
}

function Remove-GatewayLogonTask {
    param([switch]$DryRun)
    if ($DryRun) {
        Write-InstallLog 'Would remove the Local AI Switchboard logon task.'
        return
    }
    try {
        $task = Get-ScheduledTask -TaskName 'Local AI Switchboard' -ErrorAction Stop
    } catch {
        Write-InstallLog 'The logon task is already absent.'
        return
    }
    Unregister-ScheduledTask -TaskName 'Local AI Switchboard' -Confirm:$false
    Write-InstallLog 'Removed the Local AI Switchboard logon task.'
}

function Stop-OwnedProcess {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$AllowForce,
        [switch]$DryRun
    )
    if ($DryRun) {
        Write-InstallLog "Would stop $Label process $ProcessId."
        return $true
    }
    $running = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $running) {
        Write-InstallLog "$Label process $ProcessId is already stopped."
        return $true
    }
    & taskkill.exe /PID $ProcessId | Out-Null
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $running = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if ($null -eq $running) {
            Write-InstallLog "Stopped $Label process $ProcessId."
            return $true
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    if (-not $AllowForce) {
        Write-InstallLog "$Label process $ProcessId is still running." 'WARN'
        return $false
    }
    & taskkill.exe /F /PID $ProcessId | Out-Null
    Start-Sleep -Seconds 1
    $running = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -ne $running) {
        Write-InstallLog "$Label process $ProcessId is still running after a forced stop." 'ERROR'
        return $false
    }
    Write-InstallLog "Stopped $Label process $ProcessId after it ignored the first request."
    return $true
}

function Get-GatewayProcessId {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $pidFile = Join-Path $InstallRoot 'data\gateway.pid'
    if (Test-Path -LiteralPath $pidFile) {
        $text = (Get-Content -LiteralPath $pidFile -Raw).Trim()
        $processId = 0
        if ([int]::TryParse($text, [ref]$processId)) {
            $command = Get-ProcessCommandLine -ProcessId $processId
            if ($command -like '*local_ai_switchboard*') {
                return $processId
            }
        }
    }
    $gatewayProcesses = @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            ([string]$_.CommandLine) -like '*local_ai_switchboard*' -and ([string]$_.ExecutablePath) -like '*\venv\Scripts\python.exe'
        })
    if ($gatewayProcesses.Count -eq 1) {
        return [int]$gatewayProcesses[0].ProcessId
    }
    return 0
}

function Get-ComfyProcessIds {
    param([Parameter(Mandatory = $true)][string]$PortableRoot)
    $found = @()
    $processes = @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $command = [string]$_.CommandLine
            $executable = [string]$_.ExecutablePath
            $command -like '*--listen*' -and $command -like '*127.0.0.1*' -and $command -like '*main.py*' -and $executable -like '*python_embeded*'
        })
    foreach ($process in $processes) {
        if ($PortableRoot -and $process.ExecutablePath -notlike "$PortableRoot*") {
            continue
        }
        $found += [int]$process.ProcessId
    }
    return $found
}
