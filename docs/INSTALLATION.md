# Installation

Commands in this file are labeled with the shell that runs them. The installer has been reviewed and covered by static tests. It has not been executed on Windows.

## Windows PowerShell

From the repository root in Windows PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\installer\install.ps1 -Profile Design -Root "D:\AI\LocalAISwitchboard" -AcceptUnchecksummedModels
```

`-Root` is an alias of `-InstallRoot`. `Design` installs the gateway, Ollama, the `qwen3.5:4b` library tag, the pinned ComfyUI Windows Portable NVIDIA archive, and the three checksummed FLUX.2 Klein 4B files. `Basic` stops before ComfyUI. The command above is the first-install command from the repository root. It still asks once whether to continue, and Windows asks for administrator approval before creating the firewall rule.

Useful switches:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\installer\install.ps1 -Profile Design -WhatIf
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\installer\install.ps1 -Profile Design -Silent -AcceptUnchecksummedModels
```

`-WhatIf` prints the plan and does not download or change the machine. `-AcceptUnchecksummedModels` is required in silent mode because the Ollama library tag has no checksum in `installer/pins.json`. The Klein files have published checksums and do not use that switch.

The installer:

- Detects Windows build, architecture, NVIDIA GPU, driver version, VRAM, RAM, free disk, Python, Ollama, Git, and ports 11434, 8188, and 8787.
- Installs Python only when it cannot find a usable 64-bit interpreter at Python 3.12 or newer. A compatible interpreter is kept, including one that is not on `PATH`. The machine does not need a manual Python install.

Python discovery does not treat `py.exe` or a `PATH` entry as proof that a runtime exists. It reads registered Python installations, the usual per-user and Program Files locations, a launcher inventory when `py.exe` can actually run, and real `python.exe` files outside the WindowsApps aliases. WindowsApps execution aliases are ignored. ComfyUI's `python_embeded\python.exe` is never used for the gateway. A candidate is accepted only after its version, pip, and virtual-environment creation have been checked. That check runs a temporary Python file. Windows PowerShell 5.1 strips double quotes from a `python -c` argument, which would turn the probe into a syntax error. The absolute path is stored in `config\installed.json` as `python_executable`.

When that search finds nothing, the installer downloads the pinned official `python-3.12.10-amd64.exe`, verifies the published MD5 and the recorded SHA-256, checks the Authenticode signature, and runs a per-user silent install into `%LOCALAPPDATA%\Programs\Python\Python312`. It waits for the installer process and checks the exit code. It then runs `%LOCALAPPDATA%\Programs\Python\Python312\python.exe` directly. It does not look that interpreter up through a refreshed `PATH` or through `py.exe`. An incomplete copy in that directory is repaired by rerunning the same pinned installer. If the download, checksum, exit code, or executable check fails, installation stops before the gateway virtual environment is created and the log includes the download URL, verification result, exit code, expected path, and detection output.

The gateway virtual environment is created with that validated executable:

```text
<validated python.exe> -m venv <install-root>\venv
```

An existing gateway virtual environment is reused when its interpreter is Python 3.12 or newer, pip works, and it is not ComfyUI's embedded Python. ComfyUI Portable can be present while the gateway Python is installed separately.
- Runs the pinned `OllamaSetup.exe` with an argument list after the SHA-256 and Authenticode checks. It does not run a downloaded script.
- Extracts the pinned ComfyUI archive with Windows `tar.exe`, an already installed `7z.exe`, or a pinned `7zr.exe` downloaded only when both of those are unavailable. It records success only after `python_embeded\python.exe` and `ComfyUI\main.py` exist. That embedded interpreter is not the gateway virtual environment.
- Resumes an interrupted download from the `.partial` file with an HTTP range request. A file is installed only after its pinned size and checksum match. An incomplete `.partial` file is not an installed model.
- Writes `.env` when that file is absent. A rerun keeps an existing `.env`. The gateway does not generate a password or API token.
- Sets `OLLAMA_HOST=127.0.0.1:11434` when that user variable is unset. An existing different value is left in place and the model pull stops.
- Adds one firewall rule named `LocalAISwitchboard-<port>` for the gateway port on the Private profile and `LocalSubnet`. The default port 8787 keeps the name `LocalAISwitchboard-8787`. A rerun reads `LAS_API_PORT` from an existing `.env` and uses that port for the rule and for health checks. `-ApiPort` applies when `.env` is created, or when `-ForceConfig` replaces it.
- Registers a per-user logon task that starts the gateway. ComfyUI is not in that task.

Default install root:

```text
%USERPROFILE%\AI\local-ai-switchboard
```

Other scripts in the same directory:

| Script | Role |
| --- | --- |
| `start.ps1` | Start the gateway process. It does not start ComfyUI. |
| `stop.ps1` | Stop the gateway. ComfyUI stays up unless `-IncludeComfyUI` is set. |
| `doctor.ps1` | Read-only checks. It reports gateway, Ollama, ComfyUI, and image-workflow status separately. A missing image workflow does not by itself mark the gateway unhealthy. |
| `update.ps1` | Reinstall the gateway package from the local source tree. Refuses an older version unless `-AllowDowngrade` is set. Keeps models, data, secrets, and `.env`. |
| `uninstall.ps1` | Removes the virtual environment, logon task, and firewall rule. Keeps data and models unless the matching removal switch is set. |

The installer does not install an NVIDIA driver and does not configure router port forwarding.

## macOS Terminal

The Mac does not run the gateway. Install nothing for chat beyond Open CoDesign's own OpenAI-compatible provider settings. See [OPENCODESIGN.md](OPENCODESIGN.md).

For images, use the standard-library client. Setup is in [MAC_CLIENT_SETUP.md](MAC_CLIENT_SETUP.md).

```bash
mkdir -p ~/.config/local-ai-switchboard
cp client/macos/config.json.example ~/.config/local-ai-switchboard/config.json
```

Put the Windows LAN URL in `gateway_url`. No token is required.

## Development environment

macOS Terminal, from the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Copy `.env.example` to `.env` only when you want a local gateway process. The gateway does not read a token from `.env`.
