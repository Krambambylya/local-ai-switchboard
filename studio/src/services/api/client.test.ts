import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, CHAT_TIMEOUT_MS, streamChat } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("streamChat", () => {
  it("asks for a stream and reads a JSON completion when the gateway does not stream", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as { stream?: boolean; messages?: { content: string }[] };
      expect(body.stream).toBe(true);
      expect(body.messages?.[0]?.content).toBe("Hi");
      return Response.json({
        choices: [{ message: { role: "assistant", content: "Hello", reasoning_content: "think" } }],
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    let content = "";
    let reasoning = "";
    await streamChat({
      model: "qwen3.5:4b-ctx8192",
      messages: [{ role: "user", content: "Hi" }],
      onDelta: (delta) => {
        content += delta.content;
        reasoning += delta.reasoning;
      },
    });
    expect(content).toBe("Hello");
    expect(reasoning).toBe("think");
  });

  it("reports an HTTP error without including the request body", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json(
      { error: { message: "Model missing is not available", type: "not_found" } },
      { status: 404 },
    )));
    const error = await streamChat({
      model: "missing",
      messages: [{ role: "user", content: "Hi" }],
      onDelta: () => undefined,
    }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 404, errorType: "not_found", endpoint: "/v1/chat/completions" });
    expect((error as ApiError).message).toContain("404");
  });

  it("stops waiting after the chat timeout", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn((_input: RequestInfo | URL, init?: RequestInit) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => {
        reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
      });
    })));
    const pending = streamChat({
      model: "qwen3.5:4b-ctx8192",
      messages: [{ role: "user", content: "Hi" }],
      onDelta: () => undefined,
    });
    const assertion = expect(pending).rejects.toMatchObject({ errorType: "timeout" });
    await vi.advanceTimersByTimeAsync(CHAT_TIMEOUT_MS);
    await assertion;
  });
});
