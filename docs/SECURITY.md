# Security audit

Reviewed on 5 November 2026 from the source tree and the automated suite on macOS. This is a code and test audit. It is not a penetration test, and it is not a result from the RTX 4060 PC.

## Result by control

| Control | Evidence | Status |
| --- | --- | --- |
| Ollama and ComfyUI URLs must be loopback | Settings reject any other host. ComfyUI's launch arguments contain one `--listen` followed by `127.0.0.1`. Provider HTTP clients set `trust_env=False` and `follow_redirects=False`. | Verified by automated tests for the settings and the argument list. The live sockets on Windows are not yet tested. |
| Gateway is the only LAN listener | Default bind is `0.0.0.0:8787`. The installer adds one Private / `LocalSubnet` firewall rule and does not add rules for 11434 or 8188. | The installer text is verified by static tests. The running firewall and the Mac's inability to reach Ollama and ComfyUI are not yet tested. |
| No application credentials | The gateway does not check an `Authorization` header, API key, or password. Every API route works without a credential. This is limited to a trusted home LAN. | Verified by route tests that call chat, models, assets, and health with no header and with an ignored header. |
| Health responses stay small | `GET /health/live` and `GET /health/ready` return only `live`, `ok`, or `degraded`. Ready does not include paths or GPU details. | Verified by automated tests. |
| No extra public API documents | FastAPI's `/docs`, `/redoc`, and `/openapi.json` are disabled. | Verified by `test_schema_and_cors_are_not_published`. |
| CORS | No CORS middleware is installed. | Verified by the same test: an `Origin` header does not produce `Access-Control-Allow-Origin`. |
| OpenAI streaming and tool calls | SSE bytes, tool-call payloads, and `reasoning_content` are passed through. Unsupported fields return 400 and are not forwarded. | Verified against a mocked Ollama. Open CoDesign itself has not been run. |
| Image jobs share the scheduler | `POST /v1/assets` calls `scheduler.submit`. Route modules do not call ComfyUI. | Verified by automated tests. |
| Memory is released before a provider switch | The scheduler waits for provider residency and `nvidia-smi` headroom. Unknown memory and a release timeout do not start the next provider. | Verified with mocks. A real GPU transition is not yet tested. |
| Workflow allowlist | Only registered ids are accepted. Disabled `flux2-klein-4b` returns 409. Unknown ids return 404. | Verified by automated tests. The packaged file contains no API graph. |
| Managed asset paths | Downloads use `confined_file`. Absolute paths, drive prefixes, backslashes, `..`, and files outside `outputs/images` are rejected. Retention deletes a job directory only when the id is safe and the directory contains no symlinks. | Verified by automated tests, including Unicode and spaces inside the root. |
| Logs omit credential-shaped text | Bearer values, JSON secret fields, and assignment-shaped secrets are replaced with `[REDACTED]`. Request logs record method, path, and status. Chat logs record the model name and the stream flag, not the prompt. | Verified by automated tests. |
| Mac client stores only the Gateway URL | `~/.config/local-ai-switchboard/config.json` has `gateway_url`. A token field is refused. Requests do not send `Authorization`. | Verified against a local mocked gateway. A download from the Windows PC is not yet tested. |
| Scheduled startup | The task action is the local `start.ps1`, which starts `python -m local_ai_switchboard serve`. The action is rejected by the installer's own check if it contains `python_embeded`, `--listen`, or `ComfyUI\main.py`. The task restarts the process. The gateway marks an in-flight image job interrupted rather than resuming it. | Verified by static review and by the restart persistence test. The task has not been registered or rebooted on Windows. |
| Installer downloads | `pins.json` names exact versioned URLs and checksums. The scripts contain no `Invoke-Expression` of downloaded code. | Verified by static tests. PowerShell did not parse or run the scripts on this Mac. |

## Changes made during this audit

The gateway no longer publishes `/docs`, `/redoc`, or `/openapi.json`. Those pages were unauthenticated and were outside the API the rest of the project implements.

## Not implemented

`GET /admin/status`, `GET /admin/gpu`, and `GET /admin/providers` are described in the original plan and are not in the application. There is no unauthenticated admin surface because those routes do not exist.

## Residual risk

The gateway bind is all interfaces and the API does not authenticate callers. A missing or wider firewall rule would expose the API on every network the PC joins. The Private / `LocalSubnet` rule is the network boundary. There is no second authentication system.

ComfyUI and Ollama are protected by binding to loopback in this program's configuration and launch command. A copy of either program started by something else, listening on `0.0.0.0`, would not be corrected by the gateway. The installer detects that case and stops. That detection has not been observed on a live PC.

`nvidia-smi` text is parsed and then logged as memory details. Prompt text and workflow graphs are not written to the gateway log by the current log statements. A provider exception that included a prompt would pass through the redaction filter, which removes common secret shapes, not arbitrary prompt text.
