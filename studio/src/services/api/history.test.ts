import { describe, expect, it } from "vitest";

import { historyFromMessages, selectHistory } from "./history";

describe("history", () => {
  it("keeps the newest message intact and drops older turns that do not fit", () => {
    const latest = "keep me exact";
    const input = [
      { role: "user" as const, content: "a".repeat(80) },
      { role: "assistant" as const, content: latest },
    ];
    const selected = selectHistory(input, 4);
    expect(selected).toEqual([{ role: "assistant", content: latest }]);
    expect(input[1]?.content).toBe(latest);
  });

  it("sends one oversized message without changing its text", () => {
    const huge = "z".repeat(100);
    expect(selectHistory([{ role: "user", content: huge }], 1)).toEqual([{ role: "user", content: huge }]);
  });

  it("skips a pending assistant message", () => {
    const turns = historyFromMessages([
      {
        id: "1",
        role: "user",
        content: "Hello",
        reasoning: "",
        createdAt: "2026-11-11T00:00:00.000Z",
        status: "complete",
        error: null,
      },
      {
        id: "2",
        role: "assistant",
        content: "",
        reasoning: "",
        createdAt: "2026-11-11T00:00:00.000Z",
        status: "pending",
        error: null,
      },
    ]);
    expect(turns).toEqual([{ role: "user", content: "Hello" }]);
  });
});
