import type { ModelInfo } from "../../types/api";

export const PREFERRED_MODEL = "qwen3.5:4b-ctx8192";

export interface ModelSelection {
  selected: string;
  missing: boolean;
}

export function resolveSelectedModel(saved: string, models: ModelInfo[]): ModelSelection {
  if (models.length === 0) {
    return { selected: saved || PREFERRED_MODEL, missing: true };
  }
  if (saved && models.some((model) => model.id === saved)) {
    return { selected: saved, missing: false };
  }
  if (saved) return { selected: saved, missing: true };
  if (models.some((model) => model.id === PREFERRED_MODEL)) {
    return { selected: PREFERRED_MODEL, missing: false };
  }
  return { selected: models[0]?.id ?? "", missing: models.length === 0 };
}
