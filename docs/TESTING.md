# Testing

Three labels are used everywhere in this repository:

1. **Verified by automated tests** means `pytest` passed on the macOS development machine against mocks, fixtures, or static source checks.
2. **Verified by Windows hardware tests** means the check was run on the Windows 11 PC with its NVIDIA GPU. No item has this label. `nvidia-smi` output in unit tests is a fixture string, including the RTX 4060 row.
3. **Not yet tested** means the behavior is implemented or documented and has not been executed in the environment that would prove it.

## Latest automated run

Command, macOS Terminal, repository root:

```bash
.venv/bin/pytest -o addopts=
```

Result recorded with this documentation: **114 passed, 1 skipped**. The skip is `test_powershell_parser_when_available`. PowerShell is not installed on this Mac, so the installer scripts were not parsed by the PowerShell parser and were not executed. The new installer scenario checks are static. They do not download models or install software, and they were not run on Windows.

The suite uses Python 3.13 in `.venv` for development. The project requires Python 3.12 or newer. The Windows installer pins the last Python 3.12 build that still has an official Windows installer, which is 3.12.10, and only when the PC does not already have 3.12 or newer.

## Verified by automated tests

| Behavior | Tests |
| --- | --- |
| Health does not require credentials and does not leak paths or GPU details | `tests/test_health.py`, `tests/test_preflight.py` |
| Schema pages and CORS are absent | `test_schema_and_cors_are_not_published` |
| Provider URLs must be loopback | `tests/test_settings.py` |
| Logs redact bearer-shaped text and secret-shaped fields | `tests/test_logging.py`, `tests/test_health.py` |
| One scheduler, FIFO, no second worker, queue limit | `tests/test_scheduler.py` |
| LLM to LLM does not unload. LLM to image and image to LLM wait for release and memory. Unknown memory and release timeout fail closed. | `tests/test_scheduler.py`, `tests/test_ollama.py`, `tests/test_comfyui.py` |
| Chat is not a reverse proxy. Auth is required. Unsupported fields are named. SSE, tool calls, and reasoning are preserved. The stream holds the lease until cancel. | `tests/test_ollama.py` |
| ComfyUI launch arguments listen on `127.0.0.1` only. Routes do not call ComfyUI. Image jobs use the scheduler. `/free` 200 is not free VRAM. Unsafe filenames are rejected. | `tests/test_comfyui.py` |
| The packaged Klein file has no API graph | `test_packaged_sample_has_no_api_graph` |
| Restart marks an in-flight image job interrupted and does not resubmit it. Download stays inside the output root. | `tests/test_persistence.py` |
| The Mac client saves a Unicode path, retries, and sends no credential | `tests/test_generate_asset.py` |
| Installer pins, no remote script execution, firewall scope, and startup action | `tests/test_installer.py` |
| `nvidia-smi` CSV parsing, including an RTX 4060 sample row | `tests/test_nvidia_smi.py` |

The original scenarios map as follows.

| Scenario | Where it is covered |
| --- | --- |
| 1. One streaming chat | Mocked Ollama SSE test |
| 2. A second chat without reload | `test_second_chat_does_not_unload`, `test_llm_then_llm_does_not_reload` |
| 3. An image job through the scheduler | `tests/test_comfyui.py` |
| 4. Completion comes from ComfyUI history, not from `/free` | `test_free_response_is_not_proof_of_free_vram` and the lifecycle test |
| 5. Cancel an active image and a queued image | `test_active_and_queued_cancel` |
| 6. Disconnect during streaming releases the lease | `test_streaming_holds_the_lease_until_cancel_and_the_next_job_runs` |
| 7. Unknown workflow and disabled sample | Asset route tests and `test_packaged_sample_has_no_api_graph` |
| 8. Unsupported chat fields | `test_unsupported_fields_are_named_and_not_forwarded` |
| 9. Memory must be free before the next provider | Scheduler, Ollama, and ComfyUI release tests |
| 10. `/free` timeout does not start the next provider | `test_low_comfy_memory_blocks_the_next_provider_until_recover`, `test_timeout_while_releasing_gpu_memory_does_not_start_the_next_provider` |
| 11. Restart during an image job | `test_running_image_is_interrupted_on_restart_and_is_not_resubmitted` |
| 12. Tool calls preserved | `test_streaming_preserves_sse_tool_calls_and_reasoning`, `test_non_streaming_preserves_tool_calls` |
| 13. Path traversal rejected | `test_path_traversal_is_rejected_and_unicode_names_stay_inside_the_root` |
| 14. Only registered workflows | Asset route tests |
| 15. Windows paths with spaces and Unicode | Settings and `nvidia-smi` parser fixtures. Not a live Windows path. |
| 16. Mac client downloads and saves an asset | `tests/test_generate_asset.py` against a local mock |

## Verified by Windows hardware tests

None.

A parsed sample that mentions an RTX 4060 is an automated test. It is not a hardware pass.

## Not yet tested

- The Windows installer, including download, Authenticode, extraction, firewall creation, and the logon task.
- PowerShell parsing of `installer/*.ps1`.
- A real `nvidia-smi` process on the RTX 4060, and any live VRAM number.
- Ollama `qwen3.5:4b` pull, the 8192-context profile, and a chat through the gateway.
- ComfyUI portable launch, `/system_stats`, and a Klein image. The shipped workflow cannot generate an image until a real API export exists.
- Reachability from the Mac: gateway on the LAN, Ollama and ComfyUI refused.
- Open CoDesign pointed at `http://<windows-lan-ip>:8787/v1`.
- The Mac client saving a file that came from the Windows gateway.
- The Windows `msvcrt` scheduler lock.
- Reboot behavior of the scheduled task.
- `doctor.ps1` on a finished install.

The steps for the hardware list are in [WINDOWS_SETUP.md](WINDOWS_SETUP.md). Leave them unchecked until they are run.
