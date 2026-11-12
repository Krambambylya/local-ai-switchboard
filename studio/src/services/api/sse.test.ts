import { describe, expect, it } from "vitest";

import { consumeSse, parseSseBlock, readAssistantText } from "./sse";

describe("sse", () => {
  it("reads content, reasoning, and the done marker", () => {
    const parsed = consumeSse(
      'data: {"choices":[{"delta":{"content":"Hi","reasoning_content":"thinking"}}]}\n\ndata: [DONE]\n\n',
    );
    expect(parsed.rest).toBe("");
    expect(parsed.pieces).toEqual([
      { kind: "delta", content: "Hi", reasoning: "thinking" },
      { kind: "done" },
    ]);
  });

  it("keeps an incomplete block for the next chunk", () => {
    const parsed = consumeSse('data: {"choices":[{"delta":{"content":"A"}}]}\n\ndata: {"cho');
    expect(parsed.pieces).toEqual([{ kind: "delta", content: "A", reasoning: "" }]);
    expect(parsed.rest).toContain("cho");
  });

  it("marks malformed data and reads a non-stream message", () => {
    expect(parseSseBlock("data: {")).toEqual({ kind: "invalid" });
    expect(readAssistantText({
      choices: [{ message: { role: "assistant", content: "Hello", reasoning_content: "think" } }],
    })).toEqual({ content: "Hello", reasoning: "think" });
  });
});
