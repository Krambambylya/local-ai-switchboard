import { afterEach, describe, expect, it } from "vitest";

import { createId } from "./id";

describe("createId", () => {
  const original = crypto.randomUUID;

  afterEach(() => {
    Object.defineProperty(crypto, "randomUUID", { configurable: true, value: original });
  });

  it("returns an id when randomUUID is missing", () => {
    Object.defineProperty(crypto, "randomUUID", { configurable: true, value: undefined });
    expect(createId()).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  });
});
