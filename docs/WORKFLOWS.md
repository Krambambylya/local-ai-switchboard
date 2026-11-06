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

String inputs must be non-empty and at most 4000 characters. Integer inputs named `width` or `height` accept 64 through 2048. Integer inputs named `batch` or `batch_size` accept 1 through 4. A boolean is not accepted as an integer. A number input accepts an integer or a float, not a boolean.

Shape of a manifest before a real graph is pasted into `workflow`. This skeleton is not runnable: an empty `workflow` object fails validation, and the angle-bracket strings are not node ids.

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

The installer runs `load_manifest` in the gateway virtual environment. It copies the file only when validation succeeds. If `<id>.json` already exists and its bytes differ, the existing file stays unless `-ForceConfig` is set. Registration is reported as `registered, not tested`. Image generation stays `not ready` until that workflow has also been run successfully on the machine. `doctor.ps1` reports that status separately from gateway, Ollama, and ComfyUI readiness.

Restart the gateway after adding or replacing the file. `GET /v1/workflows` lists ids, titles, enabled state, and parameter names. It does not return the graph.

Until that export exists, the Mac client can submit `flux2-klein-4b` and will receive the 409 above. The client test uses a mocked gateway, not this disabled slot, to prove download and save behavior.

## Model files

The Design profile downloads these files into the portable tree after checksum verification:

| File | Directory |
| --- | --- |
| `flux-2-klein-4b-fp8.safetensors` | `ComfyUI/models/diffusion_models` |
| `qwen_3_4b.safetensors` | `ComfyUI/models/text_encoders` |
| `flux2-vae.safetensors` | `ComfyUI/models/vae` |

Those weights are not in git. FP16 and the 9B Klein model are out of scope.
