# Architecture

The gateway is one Uvicorn process with `workers=1`. A second process is refused by an exclusive lock file under the storage directory. Unix locking is covered by an automated test. The Windows `msvcrt` lock is implemented and has not been executed on Windows.

`WEB_CONCURRENCY`, `UVICORN_WORKERS`, and `LAS_WORKERS` must be empty or `1`.

## Processes

| Process | Bind | Who starts it |
| --- | --- | --- |
| Gateway | `LAS_API_HOST` and `LAS_API_PORT`, default `0.0.0.0:8787` | Per-user logon task on Windows, or `local-ai-switchboard serve` |
| Ollama | `127.0.0.1:11434` only | The Ollama Windows app. The gateway refuses any other URL. |
| ComfyUI | `127.0.0.1:8188` only | The gateway, when an image job needs it and the portable directory is set. An already healthy server is reused. |

The LAN restriction for the gateway is the Windows firewall rule created by `installer/install.ps1`: inbound TCP on the API port, Private profile, remote address `LocalSubnet`. The application itself binds the port. It does not configure the router, and it does not publish Ollama or ComfyUI.

## Request path

Chat:

1. `POST /v1/chat/completions` does not check a credential.
2. Fields outside the allowlist are rejected and are not stripped.
3. The request is queued on the GPU scheduler as an LLM job.
4. The lease is held until the SSE body ends, the non-streaming body is collected, or the client disconnects.
5. The response bytes, including tool-call deltas and `reasoning_content`, are passed through.

Image:

1. `POST /v1/assets` checks the workflow registry and does not check a credential.
2. Unknown workflow ids return 404. A disabled workflow returns 409. A client-supplied graph returns 400.
3. The job is queued on the same scheduler. The HTTP response is `202` with the job id.
4. The provider submits only the registered API graph to ComfyUI `POST /prompt`. After ComfyUI is healthy, and before that submit, it checks `/object_info` for every class, model filename, input name, and link in the graph. A missing class fails the job and does not upgrade ComfyUI.
5. Completion is ComfyUI history with `status.completed` true and `status_str` `success`. HTTP 200 from `POST /free` is not treated as free VRAM.
6. Output files are copied under `storage_dir/outputs/images/<job id>/`. Download resolves that relative path and refuses anything outside the directory.

SQLite at `storage_dir/switchboard.db` stores image job metadata. Chat transcripts are not stored. A gateway restart marks queued or running image jobs `interrupted` and does not submit them again. An output directory that already has a `.complete` marker is recorded as succeeded before that restart sweep, so a file written before a failed commit is not discarded. The scheduler does not accept GPU work until startup recovery has inspected Ollama `/api/ps` and ComfyUI, released anything those managed providers still hold, and seen enough free memory from `nvidia-smi` when a release was required. A provider that is not running is left alone.

## Scheduler

One lease is active. Same-provider work does not unload the resident provider. A switch from chat to image, or from image to chat, calls `release_resources()`, then waits until both of these are true:

- The provider reports that it has released. For Ollama that is an empty `GET /api/ps` after native unload. For ComfyUI that is an empty queue plus `vram_free` and `torch_vram_free` at or above the threshold.
- `nvidia-smi` reports free memory at or above `LAS_GPU_MEMORY_THRESHOLD_MB`. Unknown memory fails closed.

A release timeout leaves the scheduler in `ERROR`. Queued work waits there until a later recovery sees the provider clear and `nvidia-smi` has enough free memory. A new submission retries that recovery once. It is admitted only when the retry succeeds, and it is refused when the GPU is still not clear. There is no separate recovery command. `/health/ready` is degraded when startup recovery does not finish. A failed run returns the scheduler to idle. Shutdown asks the resident provider to release. If this process spawned ComfyUI, it asks that child to exit, waits, and then stops that process tree if it is still running. A ComfyUI process this gateway did not start is left running.

## Models

The Design installer pulls `qwen3.5:4b` through the Ollama application and creates a local profile `qwen3.5:4b-ctx8192` with `num_ctx` 8192. The gateway does not hardcode a model name. Open CoDesign sends the model name on each chat request.

The shipped workflow `flux2-klein-4b` is disabled and contains no graph. `flux2-klein-t2i` is the adapted official API graph. Install and update stage it under `workflows\candidates`, which the registry does not load. `verify-image-pipeline.ps1` copies it into the live directory, restarts this Gateway once, and sends the image job and the following chat through that process. The chat is what switches the scheduler from ComfyUI back to Ollama. The script does not construct a second scheduler. See [WORKFLOWS.md](WORKFLOWS.md).
