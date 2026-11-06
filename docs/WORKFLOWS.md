# Workflows

Image generation accepts a workflow id that is already registered. The client does not send a ComfyUI graph. A graph in the request is rejected with HTTP 400.

## Shipped slot

`src/local_ai_switchboard/workflows/flux2-klein-4b.json` is disabled. `workflow` is null. The file has no `class_type` and no node ids. The official Klein template is a UI subgraph, not a `/prompt` graph, so this repository does not invent one.

`POST /v1/assets` with `workflow_id` `flux2-klein-4b` returns HTTP 409 and this reason:

```text
A real ComfyUI API-format workflow export is required before image generation can be enabled.
```

The Design installer copies that disabled file to the install root's `workflows` directory when the destination is absent. An existing file is left unchanged.

## Adding a real export

On the Windows PC, export the API-format graph from the same ComfyUI build the gateway launches. Save it as a manifest in the directory named by `LAS_WORKFLOWS_DIR`. The id must match `^[a-z0-9][a-z0-9-]{0,63}$`.

An enabled manifest needs:

- `enabled` true
- `workflow` set to the API graph object
- `bindings` that point at inputs which already exist and are scalar values, not links
- `required` and `defaults` whose names are keys in `bindings`

Width and height bindings, when the input is named `width` or `height`, accept 64 through 2048. `batch` and `batch_size` accept 1 through 4. Strings must be non-empty and at most 4000 characters. A boolean is not accepted as an integer.

Restart the gateway after adding the file. `GET /v1/workflows` lists ids, titles, enabled state, and parameter names. It does not return the graph.

Until that export exists, the Mac client can submit `flux2-klein-4b` and will receive the 409 above. The client test uses a mocked gateway, not this disabled slot, to prove download and save behavior.

## Model files

The Design profile downloads these files into the portable tree after checksum verification:

| File | Directory |
| --- | --- |
| `flux-2-klein-4b-fp8.safetensors` | `ComfyUI/models/diffusion_models` |
| `qwen_3_4b.safetensors` | `ComfyUI/models/text_encoders` |
| `flux2-vae.safetensors` | `ComfyUI/models/vae` |

Those weights are not in git. FP16 and the 9B Klein model are out of scope.
