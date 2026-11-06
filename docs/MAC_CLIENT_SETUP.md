# macOS asset client

These commands run in macOS Terminal. The client is `client/macos/generate_asset.py`. It uses the Python standard library only, so there is no package to install for the script itself.

The script talks to the Gateway on the Windows PC. It submits an image job, polls until the job finishes, and saves the file under the project path you pass to `--output`. It does not start ComfyUI and it does not print the bearer token.

The shipped workflow `flux2-klein-4b` stays disabled until a real ComfyUI API-format export is registered on the Gateway. The client can request only a workflow id the Gateway has enabled.

## Configure the client

Python 3.12 or newer is enough:

```bash
python3 --version
```

Create the user configuration directory and copy the example. Replace the Gateway address with the Windows PC's private LAN address, and replace the token with the contents of the Windows token file. Do not put the token in the shell command.

```bash
mkdir -p ~/.config/local-ai-switchboard
cp client/macos/client.json.example ~/.config/local-ai-switchboard/client.json
chmod 600 ~/.config/local-ai-switchboard/client.json
```

`client.json` contains two fields:

- `gateway_url`: `http://` or `https://` address of the Gateway, with no username or password in the URL.
- `token`: the bearer token. The file must be readable only by your user. The script refuses a config that is readable by the group or by other users.

The default path is `~/.config/local-ai-switchboard/client.json`. `LAS_CLIENT_CONFIG` can point at a different file.

## Generate an image

Run this from the project directory so `--output` is saved inside that project. The script creates missing parent directories. Paths may contain spaces and Unicode characters.

```bash
python3 client/macos/generate_asset.py \
  --prompt "Premium editorial photograph of a forest river" \
  --output "public/assets/hero image.png" \
  --workflow flux2-klein-4b \
  --width 768 \
  --height 768 \
  --seed 1
```

`--prompt`, `--output`, and `--workflow` are required. `--width`, `--height`, and `--seed` are sent only when you provide them.

Progress is printed as `submitted <job id>` and `status <state>`. The saved path is printed last. A failed, cancelled, or interrupted job is reported on stderr without the token.

Network loss, a timeout, and HTTP 429, 502, 503, and 504 are retried. Authentication and rejected requests are not retried. Each request times out after 30 seconds. Polling waits up to 300 seconds.
