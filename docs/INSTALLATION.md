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
- Installs Python 3.12.10 only when no Python 3.12 or newer is already present. A newer interpreter is kept.
- Runs the pinned `OllamaSetup.exe` with an argument list after the SHA-256 and Authenticode checks. It does not run a downloaded script.
- Extracts the pinned ComfyUI archive with Windows `tar.exe`, an already installed `7z.exe`, or a pinned `7zr.exe` downloaded only when both of those are unavailable. It records success only after `python_embeded\python.exe` and `ComfyUI\main.py` exist. That embedded interpreter is not the gateway virtual environment.
- Resumes an interrupted download from the `.partial` file with an HTTP range request. A file is installed only after its pinned size and checksum match. An incomplete `.partial` file is not an installed model.
- Writes `.env` when that file is absent. A rerun keeps an existing `.env`. The gateway does not generate a password or API token.
- Sets `OLLAMA_HOST=127.0.0.1:11434` when that user variable is unset. An existing different value is left in place and the model pull stops.
- Adds one firewall rule, `LocalAISwitchboard-8787`, for the gateway port on the Private profile and `LocalSubnet`.
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
