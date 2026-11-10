# Windows setup

This page is the hardware checklist for the Windows 11 PC. None of these steps have been run. Do not treat a box as done because the installer or the gateway code exists.

Run the checks in Windows PowerShell on the PC after `.\install.ps1 -Profile Design`, and run the Mac checks in macOS Terminal. The expected GPU is an RTX 4060 8 GB.

## Checklist

- [ ] `nvidia-smi` prints the GPU name, a driver version, and about 8188 MiB of memory. The installer does not install a driver.
- [ ] `netstat -ano` or `Get-NetTCPConnection` shows Ollama listening on `127.0.0.1:11434` only.
- [ ] The same check shows ComfyUI, when it is running, on `127.0.0.1:8188` only. A stopped ComfyUI is expected between image jobs.
- [ ] The gateway listens on port `8787`. From the Mac, `http://<windows-lan-ip>:8787/health/live` returns `{"status":"live"}`.
- [ ] From the Mac, `http://<windows-lan-ip>:11434` and `http://<windows-lan-ip>:8188` do not connect.
- [ ] The firewall rule `LocalAISwitchboard-<port>` is inbound TCP, Private profile, remote address `LocalSubnet`. The default name is `LocalAISwitchboard-8787`. No router port forward was added.
- [ ] Task Scheduler shows `Local AI Switchboard` as a per-user task at logon. Its action runs `installer` service `start.ps1` for the gateway. After a reboot and sign-in, `/health/live` answers without a manual start.
- [ ] One streaming chat completes through the gateway. A second chat starts without an Ollama unload between them.
- [ ] One 768×768 Klein image completes only after Ollama has unloaded and `nvidia-smi` shows enough free memory.
- [ ] A chat after that image waits until ComfyUI has released memory.
- [ ] Disconnecting a streaming client releases the GPU lease and the next request runs.
- [ ] A forced ComfyUI `/free` that does not actually free memory leaves the scheduler in error and does not start Ollama.
- [ ] Restarting the gateway during an image job marks that job `interrupted` and does not submit it again.
- [ ] A request to the gateway from the Mac succeeds with no `Authorization` header.
- [ ] `doctor.ps1` exits 0 after the install. This has not been observed yet.

## Commands to use during the checklist

Windows PowerShell:

```powershell
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
Get-NetTCPConnection -LocalPort 11434,8188,8787 -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess
Get-NetFirewallRule -Name LocalAISwitchboard-8787 | Get-NetFirewallAddressFilter
Get-ScheduledTask -TaskName "Local AI Switchboard"
.\doctor.ps1
```

macOS Terminal, using the Gateway URL and no credential:

```bash
curl -sS "http://<windows-lan-ip>:8787/health/live"
curl -sS -o /dev/null -w "%{http_code}\n" --max-time 5 "http://<windows-lan-ip>:11434/api/version"
```

The second command should fail to connect. A successful HTTP response from the Mac means Ollama is reachable beyond localhost, which the design does not allow.

The Klein image item is one double-click on the Windows PC:

`installer\verify-image-pipeline.cmd`

It finds the install, runs the 768×768 check in Windows PowerShell 5.1, and leaves the window open.

It uses the adapted official API graph. Do not build or export a ComfyUI graph by hand. `flux2-klein-4b` stays disabled and still returns HTTP 409. The command has not been run on this PC, so the checklist box stays open. A report from that command records peak VRAM and does not claim the model fits fully in VRAM. Automated tests do not prove an RTX 4060 run. See [WORKFLOWS.md](WORKFLOWS.md).
