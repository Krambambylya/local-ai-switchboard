# Local AI Studio

Local AI Studio is the browser page served by the existing gateway at `/studio`. It does not start a second process, a second scheduler, or a Node.js server. The Mac browser talks only to the gateway. The gateway still talks to Ollama and ComfyUI on `127.0.0.1`.

The page calls `/health/ready`, `/v1/models`, and `/v1/chat/completions`. Chat history stays in the browser. Hardware counters on the system panel are placeholders. Image attachments can be chosen in the composer, and sending is blocked until image input is a tested path through the gateway.

## Build on the Mac

From the repository root, with Node.js installed:

```bash
cd studio
npm install
npm test
npm run build
```

`npm run build` writes the static files to `src/local_ai_switchboard/studio_web/`. Commit that directory with the frontend source. The Windows machine does not need Node.js.

## Publish and run on Windows

On the Mac, commit the studio source, the generated `studio_web` files, and the gateway changes, then push. On the Windows PC, in the source tree recorded for the install:

```powershell
git pull
installer\update.ps1
```

`update.ps1` reinstalls the gateway package from that source tree. If the gateway was already answering `/health/live`, the script stops it and starts it again. If it was not running:

```powershell
installer\start.ps1
```

The API port stays `8787`.

## Open the page

From the Mac browser, use the Windows PC's current private LAN address. At the time of this phase that address is:

`http://192.168.0.173:8787/studio`

Do not open `localhost` on the Mac. That address is the Mac itself.

## First check

In the model selector, choose `qwen3.5:4b-ctx8192` if it is not already selected. Send:

`Ответь коротко: Local AI Studio успешно подключена к локальной модели.`

The reply should arrive as a streamed answer. The system panel should show Gateway Online, the model count from `/v1/models`, and `—` / `Not connected` for the hardware cards. An attached PNG, JPEG, or WebP should stay in the composer with the message that image analysis is not connected, and the send button should stay disabled until the attachment is removed.
