# Workflows

Image generation accepts a workflow id that is already registered. The client does not send a ComfyUI graph. A graph in the request is rejected with HTTP 400.

## Shipped slot

`src/local_ai_switchboard/workflows/flux2-klein-4b.json` is disabled. `workflow` is null. The file has no `class_type` and no node ids. The official Klein template is a UI subgraph, not a `/prompt` graph, so this repository does not invent one.

`POST /v1/assets` with `workflow_id` `flux2-klein-4b` returns HTTP 409 and this reason:

```text
A real ComfyUI API-format workflow export is required before image generation can be enabled.
```

The Design installer copies that disabled file to the install root's `workflows` directory when `-WorkflowFile` is omitted and the destination is absent. An existing different file is left unchanged unless `-ForceConfig` is set. Copying this disabled slot does not make image generation ready.

## -WorkflowFile manifest

`-WorkflowFile` takes a path to a gateway workflow manifest. It does not take a raw ComfyUI API export. A raw export is the object whose properties are node ids and whose values contain `class_type`. That object belongs only in the manifest's `workflow` field. The installer does not invent node ids and does not wrap a raw export.

The manifest is the JSON object accepted by `load_manifest` / `validate_manifest`. Unknown fields are rejected. The fields are:

| Field | Required content |
| --- | --- |
| `id` | Lowercase slug matching `^[a-z0-9][a-z0-9-]{0,63}$`. The installer writes `<id>.json`. |
| `title` | Non-empty string, at most 200 characters. |
| `enabled` | `true` or `false`. |
| `disabled_reason` | Non-empty string when `enabled` is false. Optional when `enabled` is true. |
| `workflow` | `null` when disabled. When enabled, the ComfyUI API-format graph exported from the same ComfyUI build. Each node id is a non-empty string. Each node has a non-empty `class_type` and an `inputs` object. |
| `bindings` | Object of parameter name to `{ "node": "<id that already exists in workflow>", "input": "<scalar input on that node>" }`. Both strings are 1 to 128 characters. The input must already exist and must not be a link (an array). |
| `required` | Parameter names that are keys in `bindings`. |
| `defaults` | Parameter names that are keys in `bindings`. A default is checked against the current value's type. |

A disabled manifest must have `workflow` set to `null` and a `disabled_reason`. An enabled manifest must have a non-empty `workflow`, and every binding must name a node and input that already exist in that graph.

String inputs must be non-empty and at most 4000 characters. A binding whose parameter name or ComfyUI input is `width` or `height` accepts 64 through 2048, including a ComfyUI input named `value`. Integer inputs named `batch` or `batch_size` accept 1 through 4. A boolean is not accepted as an integer. A number input accepts an integer or a float, not a boolean.

The object below is the manifest schema. It is not runnable: an empty `workflow` object fails validation, and the angle-bracket strings are not node ids. The FLUX.2 Klein graph is already in this repository. Do not build or export a ComfyUI graph by hand for that workflow.

```json
{
  "id": "flux2-klein-4b",
  "title": "FLUX.2 Klein 4B",
  "enabled": true,
  "disabled_reason": null,
  "workflow": {},
  "bindings": {
    "prompt": {
      "node": "<node id copied from the API export>",
      "input": "<input name copied from that node>"
    }
  },
  "required": ["prompt"],
  "defaults": {}
}
```

The installer runs `load_manifest` in the gateway virtual environment. It copies the file only when validation succeeds. If `<id>.json` already exists and its bytes differ, the existing file stays unless `-ForceConfig` is set. Registration is reported as `registered, not tested`. `doctor.ps1` then reports image generation ready because the manifest passed validation. That line does not mean a live generation ran, and it does not change gateway, Ollama, or ComfyUI readiness. A missing or disabled workflow stays `Image generation: not ready`.

Restart the gateway after adding or replacing a live workflow file. `GET /v1/workflows` lists ids, titles, enabled state, and parameter names. It does not return the graph.

`flux2-klein-4b` still returns the 409 above. After `verify-image-pipeline.ps1` passes, the Mac client uses `flux2-klein-t2i`. The client test uses a mocked gateway, not the disabled slot, to prove download and save behavior.

## FLUX.2 Klein text to image

The runnable workflow id is `flux2-klein-t2i`. `workflows/sources/flux2-klein-t2i-upstream.json` is the official API graph, stored unmodified. The adapted graph and Gateway manifest are `workflows/flux2-klein-t2i/workflow_api.json` and `workflows/flux2-klein-t2i/manifest.json`. Adaptation changes the local settings only: 768×768, 4 steps, CFG 1.0, `flux-2-klein-4b-fp8.safetensors`, and `qwen_3_4b_fp4_flux2.safetensors`. Sampler `euler`, batch size 1, `flux2-vae.safetensors`, the node ids, and the links stay as published. Bindings are `prompt` (required), `width` and `height` (default 768), `seed`, and `steps` (default 4). SaveImage stays node `9`.

That manifest is outside `src/local_ai_switchboard/workflows/`, so a normal Gateway start does not enable it. `install.ps1` and `update.ps1` copy it to `workflows\candidates\flux2-klein-t2i.json`. The registry does not load that directory. A model file whose pinned size and SHA256 already match is not downloaded again.

On the Windows PC, from the repository root:

```powershell
.\installer\verify-image-pipeline.ps1 -InstallRoot "D:\AI\LocalAISwitchboard" -Width 768 -Height 768
```

Optional switches are `-Seed 42`, `-LowVramReserveMb <mb>`, and `-SampleIntervalSeconds 1`. Width and height must be 64 through 2048.

The script checks the GPU and the pinned model hashes, restarts the Gateway once with the manifest staged, and submits the image job and the later chat through that Gateway. It does not start a second Gateway or a second ComfyUI, and it does not upgrade the pinned ComfyUI. Before the prompt is submitted, the running provider checks `/object_info` for every node class and for the three model filenames. Success writes `config\image-pipeline.json` and leaves `workflows\flux2-klein-t2i.json` loaded. `doctor.ps1` then reports the workflow as verified on this machine. The report records peak VRAM and does not claim the model fits fully in VRAM. Failure restores the previous workflow files and restarts the Gateway. Reports are written to `{InstallRoot}\data\diagnostics\image-pipeline\latest\`.

`-LowVramReserveMb` retries the image job once, and only when the job error is an out-of-memory failure. The retry sets the existing `LAS_COMFYUI_RESERVE_VRAM_MB` value through `start.ps1 -ReserveVramMb`. It does not add a `--lowvram` flag and it does not choose a reserve on its own.

Automated tests do not prove an RTX 4060 run. This repository does not record a generated image from that GPU.

## Model files

The Design profile downloads these files into the portable tree after checksum verification:

| File | Directory |
| --- | --- |
| `flux-2-klein-4b-fp8.safetensors` | `ComfyUI/models/diffusion_models` |
| `qwen_3_4b_fp4_flux2.safetensors` | `ComfyUI/models/text_encoders` |
| `flux2-vae.safetensors` | `ComfyUI/models/vae` |

The text encoder is the FP4 file from Comfy-Org/Flux-2-Klein-4B at revision `8556e4d870cda7c53c7942b190bfeea5be9bd411`, 3,848,213,998 bytes. `qwen_3_4b.safetensors` is a different full-size file. The installer leaves that file in place and does not treat it as the FP4 pin. These weights are not in git. FP16 and the 9B Klein model are out of scope.

Pinning the FP4 encoder does not show that the FP8 diffusion model, this encoder, and the VAE fit in 8 GB of VRAM at the same time. ComfyUI may offload weights to system RAM. A generation on the RTX 4060 is still required before image generation can be called viable on that GPU.
