#Requires -Version 5.1
# Behavioral tests for Python discovery and the pinned installer.
# Downloads, Authenticode, and the real Python installer process are mocked.
# This file does not install Python and does not prove a hardware installation.

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    Write-Error 'These installer runtime tests run on Windows.'
    exit 1
}

$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'installer\Common.ps1')

$script:failures = New-Object System.Collections.Generic.List[string]
$script:installCalled = $false
$script:ResolvedPythonRuntime = $null
$script:PythonDiscoveryLog = New-Object System.Collections.Generic.List[string]
$script:testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('las-installer-tests-' + [guid]::NewGuid().ToString('n'))
New-Item -ItemType Directory -Force -Path $script:testRoot | Out-Null

function Assert-True {
    param(
        $Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if ($Condition) {
        Write-Host "PASS $Message"
        return
    }
    Write-Host "FAIL $Message"
    $script:failures.Add($Message)
}

function New-FakePython {
    param([Parameter(Mandatory = $true)][string]$Path)
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Set-Content -LiteralPath $Path -Value 'fake-python' -Encoding ASCII
    return $Path
}

function New-ProbeResult {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$Version = '3.12.10',
        [bool]$Ok = $true,
        [string]$Reason = '',
        [bool]$Pip = $true,
        [bool]$Venv = $true
    )
    return @{
        Ok          = $Ok
        Version     = $Version
        Executable  = $Path
        Command     = "$Path -c <runtime probe>"
        Reason      = $Reason
        Pip         = $Pip
        Venv        = $Venv
        ErrorOutput = $Reason
    }
}

function New-TestPins {
    param([string]$Sha256 = '')
    return @{
        python = @{
            url                   = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
            algorithm             = 'MD5'
            checksum              = '5eddb0b6f12c852725de071ae681dde4'
            expected_bytes        = 0
            authenticode_subject  = 'Python Software Foundation'
            sha256                = $Sha256
        }
    }
}

function Invoke-InstallerCase {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Body
    )
    $script:ResolvedPythonRuntime = $null
    $script:PythonDiscoveryLog = New-Object System.Collections.Generic.List[string]
    $script:installCalled = $false
    Write-Host "CASE $Name"
    try {
        & $Body
    } catch {
        $message = "$Name threw: $($_.Exception.Message)"
        Write-Host "FAIL $message"
        $script:failures.Add($message)
    }
}

try {
    Invoke-InstallerCase -Name 'No Python installed' -Body {
        $decision = Resolve-InstallerPython -Fresh -FromCandidatePathsOnly -InstallCommand {
            $script:installCalled = $true
            return 'install'
        }
        Assert-True $decision.Installed 'no Python starts automatic installation'
        Assert-True $script:installCalled 'the pinned installer command is reached'
        Assert-True ($null -eq $decision.Runtime) 'no interpreter is selected before installation'
    }

    Invoke-InstallerCase -Name 'Python launcher without a runtime' -Body {
        $decision = Resolve-InstallerPython -Fresh -FromCandidatePathsOnly -LauncherOutput "No suitable Python runtime found" -InstallCommand {
            $script:installCalled = $true
            return 'install'
        }
        Assert-True $decision.Installed 'launcher output does not stop installation'
        Assert-True $script:installCalled 'pinned install follows a launcher with no runtime'
        Assert-True ((Get-PythonDiscoveryText) -match 'No suitable Python runtime found') 'the launcher message is recorded'
        $listed = Join-Path $script:testRoot 'listed\python.exe'
        New-FakePython -Path $listed | Out-Null
        $script:ResolvedPythonRuntime = $null
        $found = Find-Python312 -Fresh -FromCandidatePathsOnly -LauncherOutput (" -V:3.12 *        " + $listed) -ProbeCommand {
            param($Executable, $Skip)
            return (New-ProbeResult -Path $Executable -Version '3.12.10')
        }
        Assert-True ($null -ne $found -and $found.Executable -eq $listed) 'a launcher inventory path can be selected'
    }

    Invoke-InstallerCase -Name 'Python 3.11 only' -Body {
        Assert-True (-not (Test-SupportedPythonVersion -Version '3.11.9')) '3.11 is outside the supported range'
        $path = New-FakePython -Path (Join-Path $script:testRoot 'py311\python.exe')
        $decision = Resolve-InstallerPython -Fresh -FromCandidatePathsOnly -CandidatePaths @($path) -ProbeCommand {
            param($Executable, $Skip)
            return (New-ProbeResult -Path $Executable -Version '3.11.9' -Ok $true -Pip $true -Venv $true)
        } -InstallCommand {
            $script:installCalled = $true
            return 'install'
        }
        Assert-True $decision.Installed 'Python 3.11 is not reused'
        Assert-True $script:installCalled 'Python 3.11 causes the pinned install to run'
    }

    Invoke-InstallerCase -Name 'Compatible Python 3.12' -Body {
        Assert-True (Test-SupportedPythonVersion -Version '3.12.10') '3.12.10 is supported'
        $path = New-FakePython -Path (Join-Path $script:testRoot 'py312\python.exe')
        $decision = Resolve-InstallerPython -Fresh -FromCandidatePathsOnly -CandidatePaths @($path) -ProbeCommand {
            param($Executable, $Skip)
            return (New-ProbeResult -Path $Executable -Version '3.12.10')
        } -InstallCommand {
            $script:installCalled = $true
            return 'install'
        }
        Assert-True (-not $script:installCalled) 'Python 3.12 does not install another copy'
        Assert-True (-not $decision.Installed) 'the existing 3.12 runtime is kept'
        Assert-True ($decision.Runtime.Executable -eq $path) 'the selected executable is the 3.12 interpreter'
        Assert-True ($decision.Runtime.Version -eq '3.12.10') 'the selected version is 3.12.10'
    }

    Invoke-InstallerCase -Name 'Compatible Python 3.13' -Body {
        Assert-True (Test-SupportedPythonVersion -Version '3.13.1') '3.13 is supported'
        $path = New-FakePython -Path (Join-Path $script:testRoot 'py313\python.exe')
        $decision = Resolve-InstallerPython -Fresh -FromCandidatePathsOnly -CandidatePaths @($path) -ProbeCommand {
            param($Executable, $Skip)
            return (New-ProbeResult -Path $Executable -Version '3.13.1')
        } -InstallCommand {
            $script:installCalled = $true
            return 'install'
        }
        Assert-True (-not $script:installCalled) 'Python 3.13 does not install another copy'
        Assert-True ($decision.Runtime.Version -eq '3.13.1') 'Python 3.13 is reused'
        Assert-True ($decision.Runtime.Executable -eq $path) 'the 3.13 absolute path is kept'
    }

    Invoke-InstallerCase -Name 'Python available only through an absolute path' -Body {
        $path = New-FakePython -Path (Join-Path $script:testRoot 'absolute only\python.exe')
        $found = Find-Python312 -Fresh -FromCandidatePathsOnly -CandidatePaths @($path) -ProbeCommand {
            param($Executable, $Skip)
            return (New-ProbeResult -Path $Executable -Version '3.12.8')
        }
        Assert-True ($null -ne $found) 'an off-PATH interpreter is found'
        Assert-True ($found.Executable -eq $path) 'discovery returns that absolute path'
        Assert-True ($found.Command -like "*$path*") 'the log command uses the absolute executable'
    }

    Invoke-InstallerCase -Name 'WindowsApps execution alias' -Body {
        $alias = New-FakePython -Path (Join-Path $script:testRoot 'Microsoft\WindowsApps\python.exe')
        $empty = Join-Path $script:testRoot 'empty-python\python.exe'
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $empty) | Out-Null
        New-Item -ItemType File -Force -Path $empty | Out-Null
        $real = New-FakePython -Path (Join-Path $script:testRoot 'real-python\python.exe')
        $script:probed = New-Object System.Collections.Generic.List[string]
        $found = Find-Python312 -Fresh -FromCandidatePathsOnly -CandidatePaths @($alias, $empty, $real) -ProbeCommand {
            param($Executable, $Skip)
            $script:probed.Add($Executable)
            return (New-ProbeResult -Path $Executable -Version '3.12.10')
        }
        Assert-True ($script:probed -notcontains $alias) 'the WindowsApps alias is not executed'
        Assert-True ($script:probed -notcontains $empty) 'an empty python.exe is not executed'
        Assert-True ($found.Executable -eq $real) 'a real interpreter is selected after the alias'
        $script:installCalled = $false
        $onlyAlias = Resolve-InstallerPython -Fresh -FromCandidatePathsOnly -CandidatePaths @($alias) -ProbeCommand {
            param($Executable, $Skip)
            throw "alias was probed: $Executable"
        } -InstallCommand {
            $script:installCalled = $true
            return 'install'
        }
        Assert-True $onlyAlias.Installed 'only a WindowsApps alias causes a real Python install'
        Assert-True $script:installCalled 'the pinned installer runs when the alias is the only hit'
    }

    Invoke-InstallerCase -Name 'Python installation success' -Body {
        $target = Join-Path $script:testRoot 'success\Python312'
        $expected = Join-Path $target 'python.exe'
        $installer = Join-Path $script:testRoot 'success\python-3.12.10-amd64.exe'
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $installer) | Out-Null
        Set-Content -LiteralPath $installer -Value 'installer-bytes' -Encoding ASCII
        $hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
        $script:args = New-Object System.Collections.Generic.List[string]
        $result = Install-PinnedPythonPackage -Pins (New-TestPins -Sha256 $hash) -InstallerPath $installer -ExpectedExecutable $expected -ExecutableWaitSeconds 0 -DownloadCommand {
            param($Url, $Destination)
            return $true
        } -SignatureCommand {
            param($Path)
            return @{ Status = 'Valid'; Subject = 'CN=Python Software Foundation'; Trusted = $true }
        } -InstallerCommand {
            param($Path, $Arguments, $Expected)
            $script:args.Add($Arguments)
            New-FakePython -Path $Expected | Out-Null
            return 0
        } -ProbeCommand {
            param($Executable, $Skip)
            return (New-ProbeResult -Path $Executable -Version '3.12.10')
        }
        Assert-True $result.Verified 'a verified installer result is accepted'
        Assert-True ($result.Detail -eq 'Python 3.12.10 installed and the version command succeeded.') 'success reports the version command'
        Assert-True ($script:args.Count -eq 1) 'a successful install does not retry'
        Assert-True ($script:args[0] -match 'TargetDir=') 'the installer receives TargetDir'
        Assert-True ($script:args[0] -match 'Include_launcher=1') 'the first attempt includes the per-user launcher'
        Assert-True ($script:args[0] -match [regex]::Escape($target)) 'TargetDir is the expected directory'
        Assert-True ($result.Facts.executable -eq $expected) 'state records the absolute python.exe'
        Assert-True ($script:ResolvedPythonRuntime.Executable -eq $expected) 'later stages reuse that absolute interpreter'
        Assert-True ($result.Facts.sha256 -eq $hash) 'SHA256 verification passed'
    }

    Invoke-InstallerCase -Name 'Python installation failure' -Body {
        $expected = Join-Path $script:testRoot 'failure\python.exe'
        $download = Install-PinnedPythonPackage -Pins (New-TestPins) -InstallerPath (Join-Path $script:testRoot 'failure\missing.exe') -ExpectedExecutable $expected -DownloadCommand {
            param($Url, $Destination)
            throw 'The checksum of Python 3.12.10 does not match the pin.'
        }
        Assert-True (-not $download.Verified) 'a checksum failure is not verified'
        Assert-True ($download.Detail -match 'Download verification failed') 'checksum failure names download verification'
        Assert-True ($download.Facts.checksum_verified -eq $false) 'checksum failure records the verification result'
        Assert-True ($download.Facts.download_url -match 'python-3.12.10-amd64.exe') 'checksum failure records the download URL'
        $installer = Join-Path $script:testRoot 'failure\bad-hash.exe'
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $installer) | Out-Null
        Set-Content -LiteralPath $installer -Value 'not-the-installer' -Encoding ASCII
        $script:installerRan = $false
        $hashFailure = Install-PinnedPythonPackage -Pins (New-TestPins -Sha256 ('0' * 64)) -InstallerPath $installer -ExpectedExecutable $expected -ExecutableWaitSeconds 0 -DownloadCommand {
            param($Url, $Destination)
            return $true
        } -InstallerCommand {
            param($Path, $Arguments, $Expected)
            $script:installerRan = $true
            return 0
        }
        Assert-True (-not $hashFailure.Verified) 'SHA256 mismatch stops installation'
        Assert-True ($hashFailure.Detail -match 'SHA256 verification failed') 'SHA256 failure is explicit'
        Assert-True (-not $script:installerRan) 'the installer does not run after a bad SHA256'
    }

    Invoke-InstallerCase -Name 'Non-zero installer exit code' -Body {
        $expected = Join-Path $script:testRoot 'exitcode\Python312\python.exe'
        $installer = Join-Path $script:testRoot 'exitcode\python-3.12.10-amd64.exe'
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $installer) | Out-Null
        Set-Content -LiteralPath $installer -Value 'installer-bytes' -Encoding ASCII
        $script:args = New-Object System.Collections.Generic.List[string]
        $result = Install-PinnedPythonPackage -Pins (New-TestPins) -InstallerPath $installer -ExpectedExecutable $expected -ExecutableWaitSeconds 0 -DownloadCommand {
            param($Url, $Destination)
            return $true
        } -SignatureCommand {
            param($Path)
            return @{ Status = 'Valid'; Trusted = $true }
        } -InstallerCommand {
            param($Path, $Arguments, $Expected)
            $script:args.Add($Arguments)
            return 1603
        } -ProbeCommand {
            param($Executable, $Skip)
            throw 'probe ran after a failed install'
        }
        Assert-True (-not $result.Verified) 'a non-zero installer exit is a failure'
        Assert-True ($result.Detail -match 'Automatic installation failed with exit code 1603') 'the exit code is reported'
        Assert-True ($result.Facts.expected_executable -eq $expected) 'the expected executable is reported'
        Assert-True ($result.Facts.download_url -match 'python.org') 'the download URL is reported'
        Assert-True ($result.Facts.checksum_verified -eq $true) 'checksum status is reported with the exit code'
        Assert-True ($script:args.Count -eq 2) 'the install is retried once without the launcher'
        Assert-True ($script:args[1] -match 'Include_launcher=0') 'the retry disables the launcher'
        $missing = Join-Path $script:testRoot 'missing-exe\Python312\python.exe'
        $script:args = New-Object System.Collections.Generic.List[string]
        $missingResult = Install-PinnedPythonPackage -Pins (New-TestPins) -InstallerPath $installer -ExpectedExecutable $missing -ExecutableWaitSeconds 0 -DownloadCommand {
            param($Url, $Destination)
            return $true
        } -SignatureCommand {
            param($Path)
            return @{ Status = 'Valid'; Trusted = $true }
        } -InstallerCommand {
            param($Path, $Arguments, $Expected)
            $script:args.Add($Arguments)
            return 0
        }
        Assert-True (-not $missingResult.Verified) 'exit code 0 without python.exe is a failure'
        Assert-True ($missingResult.Detail -match 'expected executable was not found') 'the missing executable path is reported'
        Assert-True ($missingResult.Detail -match [regex]::Escape($missing)) 'the detail contains the expected path'
    }

    Invoke-InstallerCase -Name 'Missing pip' -Body {
        $path = New-FakePython -Path (Join-Path $script:testRoot 'no-pip\python.exe')
        $found = Find-Python312 -Fresh -FromCandidatePathsOnly -CandidatePaths @($path) -ProbeCommand {
            param($Executable, $Skip)
            return (New-ProbeResult -Path $Executable -Version '3.12.10' -Ok $false -Reason 'pip is missing' -Pip $false -Venv $true)
        }
        Assert-True ($null -eq $found) 'an interpreter without pip is not selected'
        Assert-True ((Get-PythonDiscoveryText) -match 'pip is missing') 'missing pip is logged with the candidate'
    }

    Invoke-InstallerCase -Name 'Failed venv creation' -Body {
        $path = New-FakePython -Path (Join-Path $script:testRoot 'no-venv\python.exe')
        $found = Find-Python312 -Fresh -FromCandidatePathsOnly -CandidatePaths @($path) -ProbeCommand {
            param($Executable, $Skip)
            return (New-ProbeResult -Path $Executable -Version '3.12.10' -Ok $false -Reason 'venv creation failed with exit code 1' -Pip $true -Venv $false)
        }
        Assert-True ($null -eq $found) 'an interpreter that cannot create a venv is not selected'
        $base = New-FakePython -Path (Join-Path $script:testRoot 'venv-fail\python.exe')
        $venvPython = Join-Path $script:testRoot 'venv-fail\gateway\Scripts\python.exe'
        $created = Invoke-GatewayVenvCreate -PythonExecutable $base -VenvPython $venvPython -Runner {
            param($Python, $VenvRoot)
            return 1
        }
        Assert-True (-not $created.Ok) 'a failed venv command is not accepted'
        Assert-True ($created.Detail -match 'was not created') 'venv failure says the environment was not created'
        Assert-True ($created.Command -match [regex]::Escape($base)) 'venv failure names the validated python.exe'
        Assert-True ($created.Command -match '-m venv') 'venv creation uses -m venv'
        Assert-True ($created.Command -notmatch '-3\.12') 'venv creation does not use a launcher version switch'
    }

    Invoke-InstallerCase -Name 'Existing valid venv' -Body {
        $venvPython = New-FakePython -Path (Join-Path $script:testRoot 'existing-venv\Scripts\python.exe')
        $plan = Get-GatewayVenvPlan -VenvPython $venvPython -ProbeCommand {
            param($Executable, $Skip)
            return (New-ProbeResult -Path $Executable -Version '3.12.10')
        }
        Assert-True ($plan.Action -eq 'reuse') 'a valid gateway venv is reused'
        Assert-True $plan.Ok 'the reused venv passed validation'
        $blockedRoot = Join-Path $script:testRoot 'blocked-venv'
        New-Item -ItemType Directory -Force -Path $blockedRoot | Out-Null
        $blocked = Get-GatewayVenvPlan -VenvPython (Join-Path $blockedRoot 'Scripts\python.exe')
        Assert-True ($blocked.Action -eq 'blocked') 'a venv directory without python.exe is left unchanged'
        $base = New-FakePython -Path (Join-Path $script:testRoot 'create-venv\base\python.exe')
        $createdPython = Join-Path $script:testRoot 'create-venv\venv\Scripts\python.exe'
        $script:command = ''
        $created = Invoke-GatewayVenvCreate -PythonExecutable $base -VenvPython $createdPython -Runner {
            param($Python, $VenvRoot)
            $script:command = '"{0}" -m venv "{1}"' -f $Python, $VenvRoot
            New-FakePython -Path (Join-Path $VenvRoot 'Scripts\python.exe') | Out-Null
            return 0
        }
        Assert-True $created.Ok 'venv creation can succeed with the validated executable'
        Assert-True ($script:command -eq $created.Command) 'the recorded command is the executed command'
        Assert-True ($script:command -match '-m venv') 'the create command is python -m venv'
    }

    Invoke-InstallerCase -Name 'ComfyUI embedded Python only' -Body {
        $embedded = New-FakePython -Path (Join-Path $script:testRoot 'ComfyUI_windows_portable\python_embeded\python.exe')
        $script:probed = New-Object System.Collections.Generic.List[string]
        $decision = Resolve-InstallerPython -Fresh -FromCandidatePathsOnly -CandidatePaths @($embedded) -ProbeCommand {
            param($Executable, $Skip)
            $script:probed.Add($Executable)
            return (New-ProbeResult -Path $Executable -Version '3.12.10')
        } -InstallCommand {
            $script:installCalled = $true
            return 'install'
        }
        Assert-True ($script:probed.Count -eq 0) 'ComfyUI embedded Python is not probed for the gateway'
        Assert-True $decision.Installed 'embedded Python alone still installs the gateway runtime'
        Assert-True ((Get-PythonDiscoveryText) -match 'python_embeded') 'the skip reason names python_embeded'
        $py = New-FakePython -Path (Join-Path $script:testRoot 'Launcher\py.exe')
        $script:probed = New-Object System.Collections.Generic.List[string]
        $launcherOnly = Find-Python312 -Fresh -FromCandidatePathsOnly -CandidatePaths @($py) -ProbeCommand {
            param($Executable, $Skip)
            $script:probed.Add($Executable)
            return (New-ProbeResult -Path $Executable -Version '3.12.10')
        }
        Assert-True ($script:probed.Count -eq 0) 'py.exe is not executed'
        Assert-True ($null -eq $launcherOnly) 'py.exe is not selected as the runtime'
    }

    Invoke-InstallerCase -Name 'Interrupted installation recovery' -Body {
        $target = Join-Path $script:testRoot 'repair\Python312'
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        $expected = Join-Path $target 'python.exe'
        $installer = Join-Path $script:testRoot 'repair\python-3.12.10-amd64.exe'
        Set-Content -LiteralPath $installer -Value 'installer-bytes' -Encoding ASCII
        $result = Install-PinnedPythonPackage -Pins (New-TestPins) -InstallerPath $installer -ExpectedExecutable $expected -ExecutableWaitSeconds 0 -DownloadCommand {
            param($Url, $Destination)
            return $true
        } -SignatureCommand {
            param($Path)
            return @{ Status = 'Valid'; Trusted = $true }
        } -InstallerCommand {
            param($Path, $Arguments, $Expected)
            New-FakePython -Path $Expected | Out-Null
            return 0
        } -ProbeCommand {
            param($Executable, $Skip)
            return (New-ProbeResult -Path $Executable -Version '3.12.10')
        }
        Assert-True $result.Verified 'an incomplete Python directory can be repaired'
        Assert-True ($result.Facts.action -eq 'repaired') 'the repair is recorded as repaired'
        Assert-True ($result.Facts.executable -eq $expected) 'repair ends at the expected python.exe'
        $installRoot = Join-Path $script:testRoot 'state-root'
        $stored = New-FakePython -Path (Join-Path $script:testRoot 'stored\python.exe')
        $config = Join-Path $installRoot 'config'
        New-Item -ItemType Directory -Force -Path $config | Out-Null
        @{ python_executable = $stored } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $config 'installed.json') -Encoding UTF8
        $script:ResolvedPythonRuntime = $null
        $again = Find-Python312 -InstallRoot $installRoot -Fresh -FromCandidatePathsOnly -ProbeCommand {
            param($Executable, $Skip)
            return (New-ProbeResult -Path $Executable -Version '3.12.10')
        }
        Assert-True ($again.Executable -eq $stored) 'a stored interpreter path is reused'
    }
} finally {
    if (Test-Path -LiteralPath $script:testRoot) {
        Remove-Item -LiteralPath $script:testRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($script:failures.Count -gt 0) {
    Write-Host ("FAILED {0} assertion(s)." -f $script:failures.Count)
    exit 1
}
Write-Host 'Python runtime installer tests passed.'
exit 0
