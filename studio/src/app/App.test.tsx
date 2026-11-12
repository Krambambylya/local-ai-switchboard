import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { STORAGE_KEY } from "../services/storage/chats";

const MODELS = {
  object: "list",
  data: [
    { id: "qwen3.5:4b-ctx8192", object: "model", created: 0, owned_by: "ollama" },
    { id: "qwen2.5:7b", object: "model", created: 0, owned_by: "ollama" },
    { id: "qwen3.5:4b", object: "model", created: 0, owned_by: "ollama" },
  ],
};

const ANSWER = "Local AI Studio успешно подключена";

function jsonResponse(body: unknown, status = 200): Response {
  return Response.json(body, { status });
}

function sseResponse(text: string): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

function chatCalls(fetchMock: ReturnType<typeof vi.fn>): unknown[][] {
  return fetchMock.mock.calls.filter((call) => String(call[0]).includes("/v1/chat/completions"));
}

function studioTask(status = "succeeded") {
  return {
    id: "task-1",
    conversation_id: "chat-1",
    status,
    stage: status === "succeeded" ? "completed" : "analyzing",
    message: status === "succeeded" ? "Completed." : "Analyzing image with Qwen.",
    summary: status === "succeeded" ? "The image shows a red square." : null,
    error: null,
    stages: [
      { id: "analyzing", status: "succeeded", message: "Analyzing image with Qwen.", model: "qwen3.5:4b-ctx8192", provider: "ollama" },
      { id: "completed", status: "succeeded", message: "Completed." },
    ],
    assets: [],
    files: [{ id: "file-1", role: "original", name: "board.png", media_type: "image/png", width: 64, height: 64, byte_size: 12, label: "board.png", asset_id: null }],
    models: { analysis: "qwen3.5:4b-ctx8192" },
    switch_result: "Qwen analyzed the request. FLUX was not called.",
    operation: "analyze",
  };
}

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (typeof Request !== "undefined" && input instanceof Request) return input.url;
  return String(input);
}

function installGateway(chat?: (body: { model?: string; messages?: { content: string }[] }) => Response) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = requestUrl(input);
    const method = (init?.method ?? (typeof Request !== "undefined" && input instanceof Request ? input.method : "GET")).toUpperCase();
    if (url.includes("/health/ready")) return jsonResponse({ status: "ok" });
    if (url.includes("/v1/models")) return jsonResponse(MODELS);
    if (url.includes("/v1/chat/completions")) {
      const body = JSON.parse(String(init?.body ?? "{}")) as { model?: string; messages?: { content: string }[] };
      if (chat) return chat(body);
      return sseResponse(`data: ${JSON.stringify({ choices: [{ delta: { content: ANSWER } }] })}\n\ndata: [DONE]\n\n`);
    }
    if (url.includes("/v1/studio/tasks") && method === "POST" && !url.includes("/approve") && !url.includes("/cancel") && !url.includes("/retry")) {
      return jsonResponse(studioTask("running"), 202);
    }
    if (url.includes("/v1/studio/tasks/task-1") && method === "GET") {
      return jsonResponse(studioTask("succeeded"));
    }
    return jsonResponse({ error: { message: "missing", type: "not_found" } }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function studioCalls(fetchMock: ReturnType<typeof vi.fn>): unknown[][] {
  return fetchMock.mock.calls.filter((call) => requestUrl(call[0] as RequestInfo | URL).includes("/v1/studio/tasks"));
}

async function readyModel() {
  return screen.findByRole("option", { name: "qwen3.5:4b-ctx8192" });
}

describe("Local AI Studio", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("loads the gateway, lists models, and selects the default model", async () => {
    installGateway();
    render(<App />);
    await readyModel();
    const system = screen.getByRole("complementary", { name: "System overview" });
    expect(within(system).getByText("Online")).toBeInTheDocument();
    expect(within(system).getByText("Ready")).toBeInTheDocument();
    expect(within(system).getByText("3")).toBeInTheDocument();
    expect(screen.getByLabelText("Model")).toHaveValue("qwen3.5:4b-ctx8192");
    expect(within(screen.getByLabelText("Model")).getByRole("option", { name: "qwen3.5:4b" })).toBeInTheDocument();
  });

  it("saves the selected model", async () => {
    const user = userEvent.setup();
    installGateway();
    render(<App />);
    await readyModel();
    await user.selectOptions(screen.getByLabelText("Model"), "qwen2.5:7b");
    await waitFor(() => {
      expect(localStorage.getItem(STORAGE_KEY)).toContain("qwen2.5:7b");
    });
  });

  it("sends the current chat, renders the answer, and keeps it when switching chats", async () => {
    const user = userEvent.setup();
    const fetchMock = installGateway();
    render(<App />);
    await readyModel();
    const message = screen.getByLabelText("Message");
    await user.type(message, "line{Shift>}{Enter}{/Shift}more");
    expect(message).toHaveValue("line\nmore");
    expect(chatCalls(fetchMock)).toHaveLength(0);
    await user.clear(message);
    await user.type(message, "Привет{Enter}");
    expect(await screen.findByText(ANSWER)).toBeInTheDocument();
    const calls = chatCalls(fetchMock);
    expect(calls).toHaveLength(1);
    const body = JSON.parse(String((calls[0]?.[1] as RequestInit | undefined)?.body));
    expect(body).toEqual({
      model: "qwen3.5:4b-ctx8192",
      stream: true,
      messages: [{ role: "user", content: "Привет" }],
    });
    await waitFor(() => expect(localStorage.getItem(STORAGE_KEY)).toContain(ANSWER));
    await user.click(screen.getByRole("button", { name: "New chat" }));
    expect(screen.getByRole("heading", { name: "Local AI Studio" })).toBeInTheDocument();
    expect(screen.queryByText(ANSWER)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open Привет" }));
    expect(screen.getByText(ANSWER)).toBeInTheDocument();
    const chatRegion = screen.getByRole("region", { name: "Chat" });
    expect(within(chatRegion).getByText("Привет")).toBeInTheDocument();
  });

  it("shows an API error and a diagnostic entry", async () => {
    const user = userEvent.setup();
    installGateway(() => jsonResponse({ error: { message: "Ollama is not reachable", type: "provider_error" } }, 503));
    render(<App />);
    await readyModel();
    await user.type(screen.getByLabelText("Message"), "Привет{Enter}");
    expect(await screen.findByText(/unavailable \(503\)/)).toBeInTheDocument();
    const system = screen.getByRole("complementary", { name: "System overview" });
    expect(within(system).getByText("503")).toBeInTheDocument();
    expect(within(system).getByText("provider_error")).toBeInTheDocument();
  });

  it("sends an image through the studio task API instead of chat completions", async () => {
    const user = userEvent.setup();
    const fetchMock = installGateway();
    render(<App />);
    await readyModel();
    const file = new File(["png"], "board.png", { type: "image/png" });
    await user.upload(screen.getByLabelText("Attach"), file);
    expect(screen.getByText("board.png")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Message"), "Describe the image");
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
    expect(screen.queryByText("This file type is not supported.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("The image shows a red square.")).toBeInTheDocument();
    expect(chatCalls(fetchMock)).toHaveLength(0);
    expect(studioCalls(fetchMock).length).toBeGreaterThan(0);
    expect(screen.getByText("Analyzing image with Qwen.")).toBeInTheDocument();
  });

  it("shows an unsupported attachment without calling the model", async () => {
    const user = userEvent.setup();
    const fetchMock = installGateway();
    render(<App />);
    await readyModel();
    const input = screen.getByLabelText("Attach");
    fireEvent.change(input, { target: { files: [new File(["x"], "notes.txt", { type: "text/plain" })] } });
    expect(screen.getByText("This file type is not supported.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Message"), "read this{Enter}");
    expect(chatCalls(fetchMock)).toHaveLength(0);
    expect(studioCalls(fetchMock)).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("renders metric placeholders instead of invented numbers", async () => {
    installGateway();
    render(<App />);
    await readyModel();
    const system = screen.getByRole("complementary", { name: "System overview" });
    expect(within(system).getByText("CPU Usage")).toBeInTheDocument();
    expect(within(system).getAllByText("Not connected")).toHaveLength(8);
    expect(within(system).getAllByText("—").length).toBeGreaterThanOrEqual(8);
    expect(within(system).getByText("Metrics integration pending")).toBeInTheDocument();
    expect(within(system).getByText("Metrics collection is not implemented yet.")).toBeInTheDocument();
  });

  it("tells the user when the saved model is gone and does not send", async () => {
    const user = userEvent.setup();
    const now = "2026-11-11T12:00:00.000Z";
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      version: 1,
      selectedModel: "gone:model",
      activeChatId: "chat-1",
      chats: [{ id: "chat-1", title: "New chat", createdAt: now, updatedAt: now, messages: [] }],
    }));
    const fetchMock = installGateway();
    render(<App />);
    expect(await screen.findByText("The selected model is no longer available.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Message"), "hello{Enter}");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(chatCalls(fetchMock)).toHaveLength(0);
  });

  it("shows the gateway as offline when the API cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }));
    render(<App />);
    expect(await screen.findByText("The gateway is unreachable.")).toBeInTheDocument();
    expect(screen.getAllByText("Offline").length).toBeGreaterThan(0);
  });

  it("renders a code block from the model response", async () => {
    const user = userEvent.setup();
    const payload = JSON.stringify({ choices: [{ delta: { content: "```ts\nconst value = 1;\n```" } }] });
    installGateway(() => sseResponse(`data: ${payload}\n\ndata: [DONE]\n\n`));
    render(<App />);
    await readyModel();
    await user.type(screen.getByLabelText("Message"), "code{Enter}");
    expect(await screen.findByText("const value = 1;")).toBeInTheDocument();
  });

  it("deletes a chat after confirmation", async () => {
    const user = userEvent.setup();
    installGateway();
    render(<App />);
    await readyModel();
    await user.type(screen.getByLabelText("Message"), "Привет{Enter}");
    expect(await screen.findByText(ANSWER)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete Привет" }));
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.queryByRole("button", { name: "Open Привет" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Local AI Studio" })).toBeInTheDocument();
  });
});
