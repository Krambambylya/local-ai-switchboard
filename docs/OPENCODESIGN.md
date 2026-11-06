# Open CoDesign

Open CoDesign on the Mac uses the gateway as an OpenAI-compatible text endpoint. It has no ComfyUI provider. Image jobs go through `client/macos/generate_asset.py`, described in [MAC_CLIENT_SETUP.md](MAC_CLIENT_SETUP.md).

This configuration has not been exercised against a running Open CoDesign app. The automated tests cover the HTTP contract the app would call. They do not launch Open CoDesign.

## Provider settings

In Open CoDesign's OpenAI-compatible provider settings, use:

| Setting | Value |
| --- | --- |
| Base URL | `http://<windows-lan-ip>:8787/v1` |
| API key | The bearer token from the Windows file `secrets\mac-client.json` |
| Model | `qwen3.5:4b`, or `qwen3.5:4b-ctx8192` after the Design installer creates that profile |

The base URL ends in `/v1`. The gateway then serves `POST /v1/chat/completions` and `GET /v1/models`. Health checks are `GET /health/live` and `GET /health/ready` on the host root, not under `/v1`.

Example for a PC at `192.168.1.50`:

```text
http://192.168.1.50:8787/v1
```

Use the address printed by the installer, not this example, if the PC's address is different.

## Streaming and tools

Chat requests are queued on the GPU scheduler. Streaming responses are the SSE bytes from Ollama, including tool-call deltas and `reasoning_content`. The lease stays held until the stream ends or the client disconnects.

These request fields are accepted: `model`, `messages`, `stream`, `tools`, `temperature`, `top_p`, `max_tokens`, `max_completion_tokens`, `stop`, `seed`, `presence_penalty`, `frequency_penalty`, `response_format`.

Any other field, including `tool_choice`, `n`, `logit_bias`, `user`, and `keep_alive`, returns HTTP 400 and names the field. The gateway does not drop the field and continue. A remote image URL in a message returns 400. A base64 data URL is forwarded.

`GET /v1/models` requires the bearer token and returns the local model ids. It does not return Ollama's raw catalog.

## What Open CoDesign cannot do here

- It cannot call ComfyUI, port 8188, or `POST /v1/assets`.
- It cannot address Ollama on port 11434 from the Mac. That port is localhost on the Windows PC.
- It cannot enable the Klein workflow. Image generation stays disabled until a real API-format workflow export is registered.

A second chat can follow the first without an unload. A chat after an image job waits until ComfyUI and `nvidia-smi` both report enough free memory. That wait is inside the gateway. Open CoDesign sees a normal request that takes longer.
