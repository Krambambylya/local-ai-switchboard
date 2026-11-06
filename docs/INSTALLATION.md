# Installation

Commands in this file are labeled with the shell that runs them. The installer has been reviewed and covered by static tests. It has not been executed on Windows.

## Windows PowerShell

From the repository's `installer` directory on the Windows PC:

```powershell
.\install.ps1 -Profile Design
```

`Design` is the profile for the RTX 4060 machine. It installs the gateway, Ollama, the `qwen3.5:4b` library tag, the pinned ComfyUI Windows Portable NVIDIA archive, and the three checksummed FLUX.2 Klein 4B files. `Basic` stops before ComfyUI.

Useful switches:

```powershell
.\install.ps1 -Profile Design -WhatIf
.\install.ps1 -Profile Design -Silent -AcceptUnchecksummedModels
```

`-WhatIf` prints the plan and does not download or change the machine. `-AcceptUnchecksummedModels` is required in silent mode because the Ollama library tag has no checksum in `installer/pins.json`. The Klein files have published checksums and do not use that switch.

The installer:

- Detects Windows build, architecture, NVIDIA GPU, driver version, VRAM, RAM, free disk, Python, Ollama, Git, and ports 11434, 8188, and 8787.
- Installs Python 3.12.10 only when no Python 3.12 or newer is already present. A newer interpreter is kept.
- Runs the pinned `OllamaSetup.exe` with an argument list after the SHA-256 and Authenticode checks. It does not run a downloaded script.
- Extracts the pinned ComfyUI archive and records success only after `python_embeded\python.exe` and `ComfyUI\main.py` exist. That embedded interpreter is not the gateway virtual environment.
- Writes `secrets\token.txt` and `secrets\mac-client.json` with an ACL limited to the current user. A rerun keeps an existing token and `.env`.
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
| `doctor.ps1` | Read-only checks. It does not print the token. |
| `update.ps1` | Reinstall the gateway package from the local source tree. Refuses an older version unless `-AllowDowngrade` is set. Keeps models, data, secrets, and `.env`. |
| `uninstall.ps1` | Removes the virtual environment, logon task, and firewall rule. Keeps data, secrets, and models unless the matching removal switch is set. |

The installer does not install an NVIDIA driver and does not configure router port forwarding.

## macOS Terminal

The Mac does not run the gateway. Install nothing for chat beyond Open CoDesign's own OpenAI-compatible provider settings. See [OPENCODESIGN.md](OPENCODESIGN.md).

For images, use the standard-library client. Setup is in [MAC_CLIENT_SETUP.md](MAC_CLIENT_SETUP.md).

```bash
mkdir -p ~/.config/local-ai-switchboard
cp client/macos/client.json.example ~/.config/local-ai-switchboard/client.json
chmod 600 ~/.config/local-ai-switchboard/client.json
```

Put the Windows LAN URL and the token from `secrets\mac-client.json` in that file. Do not paste the token into the shell.

## Development environment

macOS Terminal, from the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Copy `.env.example` to `.env` only when you want a local gateway process. Leave the token out of `.env`. The token file path is `LAS_TOKEN_FILE`.
