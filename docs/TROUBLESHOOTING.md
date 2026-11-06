# Troubleshooting

These are the failures the current code reports. They are not a record of a live Windows session.

## Gateway

| What you see | What it means |
| --- | --- |
| `GET /health/ready` returns `degraded` | The token file is missing, empty, unreadable, or shorter than 16 characters, or another preflight check failed. The body does not say which one. Run `local-ai-switchboard preflight` on the PC. |
| HTTP 401 | The `Authorization: Bearer` value does not match `secrets\token.txt`. |
| HTTP 400 `Unsupported fields:` | The chat body includes a field outside the allowlist. Remove that field. The gateway will not ignore it. |
| HTTP 400 about a remote image URL | Send a base64 data URL, or send text only. |
| HTTP 404 on a workflow id | That id is not in the workflow directory. |
| HTTP 409 on `flux2-klein-4b` | The shipped slot is disabled until a real API-format export is installed. |
| HTTP 429 | The scheduler queue is at `LAS_MAX_QUEUE_DEPTH`. |
| A chat waits a long time after an image | The scheduler is waiting for ComfyUI residency and `nvidia-smi` headroom. A timeout leaves the scheduler in error instead of starting Ollama. |
| Port 8787 is already in use | The installer will not stop a foreign process. Stop the other listener or choose another `LAS_API_PORT` and a matching firewall rule. |
| Two gateway processes | The second process should exit because the scheduler lock is held. On Windows that lock uses `msvcrt` and has not been executed yet. |

Windows PowerShell:

```powershell
.\doctor.ps1
.\stop.ps1
.\start.ps1
```

`stop.ps1` does not stop ComfyUI unless you pass `-IncludeComfyUI`.

## Ollama and ComfyUI

Windows PowerShell:

```powershell
ollama --version
curl.exe http://127.0.0.1:11434/api/version
curl.exe http://127.0.0.1:8188/system_stats
```

`OLLAMA_HOST` must stay `127.0.0.1:11434`. If it is already set to something else, the installer leaves it unchanged and does not pull the model.

ComfyUI starts only when an image job needs it, the portable directory contains `python_embeded\python.exe` and `ComfyUI\main.py`, and nothing healthy is already on `127.0.0.1:8188`. If the portable directory is unset and nothing is listening, the image job fails with `ComfyUI is not running`.

## Mac client

macOS Terminal:

```bash
ls -l ~/.config/local-ai-switchboard/client.json
python3 client/macos/generate_asset.py --help
```

The config file must be mode `0600` and owned by you. The script exits 1 for a bad config or a network failure after retries, and exits 2 when the job finishes as failed, cancelled, or interrupted. It does not print the token.

A 409 from the disabled Klein workflow is expected today. Retries are not used for 400, 401, 404, or 409.

## Installer

The installer log is `logs\installer-*.log` under the install root. It records paths and versions. It does not record the token.

A checksum mismatch deletes the partial download and does not run the installer. An existing ComfyUI tree, token, or `.env` is kept. `update.ps1` refuses a source version older than the installed package unless `-AllowDowngrade` is set.

If Windows `tar` cannot unpack the ComfyUI `.7z`, install 7-Zip from `https://www.7-zip.org/` and run the installer again. The installer does not download 7-Zip itself.
