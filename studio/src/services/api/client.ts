import type { HistoryTurn, ModelInfo } from "../../types/api";
import { readAssistantText, consumeSse, parseSseBlock, type SsePiece } from "./sse";

export const CHAT_TIMEOUT_MS = 180_000;

export class ApiError extends Error {
  readonly endpoint: string;
  readonly status: number | null;
  readonly errorType: string;
  readonly durationMs: number;

  constructor(
    message: string,
    details: { endpoint: string; status: number | null; errorType: string; durationMs: number },
  ) {
    super(message);
    this.name = "ApiError";
    this.endpoint = details.endpoint;
    this.status = details.status;
    this.errorType = details.errorType;
    this.durationMs = details.durationMs;
  }
}

export interface ApiSuccess<T> {
  value: T;
  endpoint: string;
  status: number;
  durationMs: number;
}

export async function getReady(signal?: AbortSignal): Promise<ApiSuccess<"ok" | "degraded">> {
  const endpoint = "/health/ready";
  const started = performance.now();
  const response = await fetchOrThrow(endpoint, { signal, headers: { Accept: "application/json" } }, started);
  const durationMs = performance.now() - started;
  const payload = await readBody(response, endpoint, durationMs);
  if (!isRecord(payload) || (payload.status !== "ok" && payload.status !== "degraded")) {
    throw new ApiError("The gateway returned invalid JSON.", {
      endpoint,
      status: response.status,
      errorType: "invalid_json",
      durationMs,
    });
  }
  if (!response.ok && payload.status !== "degraded") {
    throw httpError(response.status, payload, endpoint, durationMs);
  }
  return { value: payload.status, endpoint, status: response.status, durationMs };
}

export async function listModels(signal?: AbortSignal): Promise<ApiSuccess<ModelInfo[]>> {
  const endpoint = "/v1/models";
  const started = performance.now();
  const response = await fetchOrThrow(endpoint, { signal, headers: { Accept: "application/json" } }, started);
  const durationMs = performance.now() - started;
  const payload = await readBody(response, endpoint, durationMs);
  if (!response.ok) throw httpError(response.status, payload, endpoint, durationMs);
  if (!isRecord(payload) || !Array.isArray(payload.data)) {
    throw new ApiError("The gateway returned invalid JSON.", {
      endpoint,
      status: response.status,
      errorType: "invalid_json",
      durationMs,
    });
  }
  return { value: payload.data.flatMap(parseModel), endpoint, status: response.status, durationMs };
}

export async function streamChat(options: {
  model: string;
  messages: HistoryTurn[];
  signal?: AbortSignal;
  onDelta: (delta: { content: string; reasoning: string }) => void;
}): Promise<ApiSuccess<void>> {
  const endpoint = "/v1/chat/completions";
  const started = performance.now();
  const linked = linkAbort(options.signal, CHAT_TIMEOUT_MS);
  try {
    const response = await fetchOrThrow(
      endpoint,
      {
        method: "POST",
        signal: linked.signal,
        headers: { "Content-Type": "application/json", Accept: "text/event-stream, application/json" },
        body: JSON.stringify({ model: options.model, messages: options.messages, stream: true }),
      },
      started,
      linked,
    );
    const durationMs = () => performance.now() - started;
    const payloadType = response.headers.get("content-type") ?? "";
    if (!response.ok) {
      const payload = await readBody(response, endpoint, durationMs());
      throw httpError(response.status, payload, endpoint, durationMs());
    }
    if (!payloadType.includes("text/event-stream")) {
      const payload = await readBody(response, endpoint, durationMs());
      const text = readAssistantText(payload);
      if (!text) {
        throw new ApiError("The gateway returned invalid JSON.", {
          endpoint,
          status: response.status,
          errorType: "invalid_json",
          durationMs: durationMs(),
        });
      }
      options.onDelta(text);
      return { value: undefined, endpoint, status: response.status, durationMs: durationMs() };
    }
    await readEventStream(response, endpoint, options.onDelta, durationMs, linked);
    return { value: undefined, endpoint, status: response.status, durationMs: durationMs() };
  } finally {
    linked.stop();
  }
}

export function messageForStatus(status: number, gatewayMessage: string | null): string {
  const known: Record<number, string> = {
    400: "The request was rejected (400).",
    404: "The model was not found (404).",
    429: "The GPU queue is full (429).",
    500: "The gateway failed (500).",
    503: "The model service is unavailable (503).",
  };
  const base = known[status] ?? `The request failed (${status}).`;
  if (gatewayMessage && gatewayMessage.length <= 240 && !gatewayMessage.includes("\n")) {
    return `${base} ${gatewayMessage}`;
  }
  return base;
}

async function fetchOrThrow(
  endpoint: string,
  init: RequestInit,
  started: number,
  linked?: LinkedAbort,
): Promise<Response> {
  try {
    return await fetch(endpoint, init);
  } catch (error) {
    throw classifyTransport(error, endpoint, performance.now() - started, linked);
  }
}

async function readBody(response: Response, endpoint: string, durationMs: number): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new ApiError("The gateway returned invalid JSON.", {
      endpoint,
      status: response.status,
      errorType: "invalid_json",
      durationMs,
    });
  }
}

async function readEventStream(
  response: Response,
  endpoint: string,
  onDelta: (delta: { content: string; reasoning: string }) => void,
  durationMs: () => number,
  linked: LinkedAbort,
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) {
    throw new ApiError("The connection was interrupted.", {
      endpoint,
      status: response.status,
      errorType: "interrupted",
      durationMs: durationMs(),
    });
  }
  const decoder = new TextDecoder();
  let pending = "";
  while (true) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch (error) {
      throw classifyTransport(error, endpoint, durationMs(), linked);
    }
    pending += decoder.decode(chunk.value, { stream: !chunk.done });
    const consumed = consumeSse(pending);
    pending = consumed.rest;
    applyPieces(consumed.pieces, onDelta, endpoint, response.status, durationMs());
    if (chunk.done) break;
  }
  if (pending.trim()) {
    const piece = parseSseBlock(pending);
    if (piece) applyPieces([piece], onDelta, endpoint, response.status, durationMs());
  }
}

function applyPieces(
  pieces: SsePiece[],
  onDelta: (delta: { content: string; reasoning: string }) => void,
  endpoint: string,
  status: number,
  durationMs: number,
): void {
  for (const piece of pieces) {
    if (piece.kind === "delta") {
      if (piece.content || piece.reasoning) onDelta(piece);
      continue;
    }
    if (piece.kind === "done") return;
    if (piece.kind === "error") {
      throw new ApiError(messageForStatus(status, piece.message), {
        endpoint,
        status,
        errorType: "provider_error",
        durationMs,
      });
    }
    throw new ApiError("The gateway returned invalid JSON.", {
      endpoint,
      status,
      errorType: "invalid_json",
      durationMs,
    });
  }
}

function httpError(status: number, payload: unknown, endpoint: string, durationMs: number): ApiError {
  const details = gatewayError(payload);
  return new ApiError(messageForStatus(status, details.message), {
    endpoint,
    status,
    errorType: details.errorType ?? "http_error",
    durationMs,
  });
}

function gatewayError(payload: unknown): { message: string | null; errorType: string | null } {
  if (!isRecord(payload) || !isRecord(payload.error)) return { message: null, errorType: null };
  return {
    message: typeof payload.error.message === "string" ? payload.error.message : null,
    errorType: typeof payload.error.type === "string" ? payload.error.type : null,
  };
}

function classifyTransport(error: unknown, endpoint: string, durationMs: number, linked?: LinkedAbort): ApiError {
  if (error instanceof ApiError) return error;
  if (linked?.timedOut() || (isAbortError(error) && linked?.timedOut())) {
    return new ApiError("The request timed out.", { endpoint, status: null, errorType: "timeout", durationMs });
  }
  if (isAbortError(error)) {
    return new ApiError("The request was stopped.", { endpoint, status: null, errorType: "cancelled", durationMs });
  }
  return new ApiError("The gateway is unreachable.", { endpoint, status: null, errorType: "network", durationMs });
}

function parseModel(value: unknown): ModelInfo[] {
  if (!isRecord(value) || typeof value.id !== "string" || value.id.length === 0) return [];
  return [
    {
      id: value.id,
      object: typeof value.object === "string" ? value.object : "model",
      created: typeof value.created === "number" ? value.created : 0,
      owned_by: typeof value.owned_by === "string" ? value.owned_by : "ollama",
    },
  ];
}

interface LinkedAbort {
  signal: AbortSignal;
  stop: () => void;
  timedOut: () => boolean;
}

function linkAbort(parent: AbortSignal | undefined, timeoutMs: number): LinkedAbort {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const onParent = () => controller.abort();
  if (parent) {
    if (parent.aborted) controller.abort();
    else parent.addEventListener("abort", onParent, { once: true });
  }
  return {
    signal: controller.signal,
    timedOut: () => timedOut,
    stop: () => {
      clearTimeout(timer);
      parent?.removeEventListener("abort", onParent);
    },
  };
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
