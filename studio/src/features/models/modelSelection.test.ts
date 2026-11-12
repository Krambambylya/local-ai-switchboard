import { describe, expect, it } from "vitest";

import { PREFERRED_MODEL, resolveSelectedModel } from "./modelSelection";
import type { ModelInfo } from "../../types/api";

const models: ModelInfo[] = [
  { id: "qwen2.5:7b", object: "model", created: 0, owned_by: "ollama" },
  { id: PREFERRED_MODEL, object: "model", created: 0, owned_by: "ollama" },
];

describe("model selection", () => {
  it("prefers the saved model, then the default profile, then the first listed model", () => {
    expect(resolveSelectedModel("qwen2.5:7b", models)).toEqual({ selected: "qwen2.5:7b", missing: false });
    expect(resolveSelectedModel("", models)).toEqual({ selected: PREFERRED_MODEL, missing: false });
    expect(resolveSelectedModel("", [models[0]!])).toEqual({ selected: "qwen2.5:7b", missing: false });
  });

  it("keeps a missing selection visible", () => {
    expect(resolveSelectedModel("gone:model", models)).toEqual({ selected: "gone:model", missing: true });
    expect(resolveSelectedModel("", [])).toEqual({ selected: PREFERRED_MODEL, missing: true });
  });
});
