# Local AI Switchboard

Local AI Switchboard is a single-process gateway for one Windows PC with an NVIDIA GPU. A Mac client reaches the gateway on the private LAN. Ollama and ComfyUI stay on `127.0.0.1`. One scheduler holds the GPU, so a chat and an image job never run at the same time.

The current target machine is Windows 11 x64, an RTX 4060 8 GB, and 32 GB of RAM. The Mac is an M1. Development and the automated suite run on macOS. Nothing in this repository records a completed run on that Windows PC.

## What is verified

Automated tests on this Mac cover the gateway contract with mocked Ollama, mocked ComfyUI, and a parsed `nvidia-smi` sample. They do not open a real GPU, a real Ollama, or a real ComfyUI.

The Windows hardware checklist in [docs/TESTING.md](docs/TESTING.md) has not been performed. Treat every LAN, firewall, startup, and image-generation claim as unchecked until that list is run on the PC. Automated tests do not prove an RTX 4060 run, and this repository does not record a generated Klein image from that GPU.

On the Windows PC, after the Design install, double-click:

`installer\verify-image-pipeline.cmd`

It finds the install and runs the 768×768 check. The window stays open until you press a key.

## Layout

- `src/local_ai_switchboard/` is the gateway. The built Local AI Studio page is served at `/studio`.
- `studio/` is the frontend source. Node.js is used to build it. The running gateway does not start a Node server.
- `client/macos/generate_asset.py` downloads a finished image into a project directory. It sends the Gateway URL and no credentials.
- `installer/` is the Windows PowerShell installer and the lifecycle scripts.
- `docs/` is the operator and security documentation.

## Run the tests

macOS Terminal, from the repository root, with the project virtual environment:

```bash
.venv/bin/pytest
```

Python 3.12 or newer is required. The suite does not need an NVIDIA driver.

## Operator docs

- [Architecture](docs/ARCHITECTURE.md)
- [Local AI Studio](docs/STUDIO.md)
- [Installation](docs/INSTALLATION.md)
- [Windows setup and hardware checklist](docs/WINDOWS_SETUP.md)
- [Open CoDesign](docs/OPENCODESIGN.md)
- [macOS asset client](docs/MAC_CLIENT_SETUP.md)
- [Workflows](docs/WORKFLOWS.md)
- [Security audit](docs/SECURITY.md)
- [Testing status](docs/TESTING.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
