import type { StudioRegion, StudioTaskView } from "../../types/api";
import { ApiError, messageForStatus } from "./client";

export const TASK_POLL_MS = 1000;
export const TASK_TIMEOUT_MS = 25 * 60 * 1000;

export async function createStudioTask(options: {
  prompt: string;
  conversationId: string;
  files: File[];
  signal?: AbortSignal;
}): Promise<{ value: StudioTaskView; endpoint: string; status: number; durationMs: number }> {
  const endpoint = "/v1/studio/tasks";
  const started = performance.now();
  const body = new FormData();
  body.append("prompt", options.prompt);
  body.append("conversation_id", options.conversationId);
  for (const file of options.files) body.append("attachments", file, file.name);
  const response = await fetchOrThrow(endpoint, { method: "POST", body, signal: options.signal }, started);
  const durationMs = performance.now() - started;
  const payload = await readBody(response, endpoint, durationMs);
  if (!response.ok) throw httpError(response.status, payload, endpoint, durationMs);
  const task = parseTask(payload);
  if (!task) {
    throw new ApiError("The gateway returned invalid JSON.", {
      endpoint,
      status: response.status,
      errorType: "invalid_json",
      durationMs,
    });
  }
  return { value: task, endpoint, status: response.status, durationMs };
}

export async function getStudioTask(
  taskId: string,
  signal?: AbortSignal,
): Promise<{ value: StudioTaskView; endpoint: string; status: number; durationMs: number }> {
  const endpoint = `/v1/studio/tasks/${taskId}`;
  const started = performance.now();
  const response = await fetchOrThrow(endpoint, { signal, headers: { Accept: "application/json" } }, started);
  const durationMs = performance.now() - started;
  const payload = await readBody(response, endpoint, durationMs);
  if (!response.ok) throw httpError(response.status, payload, endpoint, durationMs);
  const task = parseTask(payload);
  if (!task) {
    throw new ApiError("The gateway returned invalid JSON.", {
      endpoint,
      status: response.status,
      errorType: "invalid_json",
      durationMs,
    });
  }
  return { value: task, endpoint, status: response.status, durationMs };
}

export async function patchStudioRegions(
  taskId: string,
  regions: StudioRegion[],
  signal?: AbortSignal,
): Promise<StudioTaskView> {
  const endpoint = `/v1/studio/tasks/${taskId}/regions`;
  const started = performance.now();
  const response = await fetchOrThrow(
    endpoint,
    {
      method: "PATCH",
      signal,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ regions }),
    },
    started,
  );
  const durationMs = performance.now() - started;
  const payload = await readBody(response, endpoint, durationMs);
  if (!response.ok) throw httpError(response.status, payload, endpoint, durationMs);
  const task = parseTask(payload);
  if (!task) {
    throw new ApiError("The gateway returned invalid JSON.", {
      endpoint,
      status: response.status,
      errorType: "invalid_json",
      durationMs,
    });
  }
  return task;
}

export async function approveStudioTask(taskId: string, signal?: AbortSignal): Promise<StudioTaskView> {
  return postTaskAction(taskId, "approve", signal);
}

export async function cancelStudioTask(taskId: string, signal?: AbortSignal): Promise<StudioTaskView> {
  return postTaskAction(taskId, "cancel", signal);
}

export async function retryStudioTask(taskId: string, signal?: AbortSignal): Promise<StudioTaskView> {
  return postTaskAction(taskId, "retry", signal);
}

export function fileUrl(fileId: string): string {
  return `/v1/studio/files/${fileId}`;
}

export function archiveUrl(taskId: string): string {
  return `/v1/studio/tasks/${taskId}/archive`;
}

export function isTerminalStatus(status: string): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled" || status === "interrupted";
}

export function parseTask(value: unknown): StudioTaskView | null {
  if (!isRecord(value) || typeof value.id !== "string") return null;
  if (typeof value.status !== "string") return null;
  const models = isRecord(value.models)
    ? Object.fromEntries(
        Object.entries(value.models).flatMap(([key, entry]) => (typeof entry === "string" ? [[key, entry]] : [])),
      )
    : {};
  return {
    id: value.id,
    status: value.status,
    stage: typeof value.stage === "string" ? value.stage : null,
    message: typeof value.message === "string" ? value.message : null,
    summary: typeof value.summary === "string" ? value.summary : null,
    error: typeof value.error === "string" ? value.error : null,
    stages: Array.isArray(value.stages) ? value.stages.flatMap(parseStage) : [],
    assets: Array.isArray(value.assets) ? value.assets.flatMap(parseRegion) : [],
    files: Array.isArray(value.files) ? value.files.flatMap(parseFile) : [],
    models,
    switch_result: typeof value.switch_result === "string" ? value.switch_result : null,
    operation: typeof value.operation === "string" ? value.operation : null,
  };
}

async function postTaskAction(taskId: string, action: string, signal?: AbortSignal): Promise<StudioTaskView> {
  const endpoint = `/v1/studio/tasks/${taskId}/${action}`;
  const started = performance.now();
  const response = await fetchOrThrow(endpoint, { method: "POST", signal, headers: { Accept: "application/json" } }, started);
  const durationMs = performance.now() - started;
  const payload = await readBody(response, endpoint, durationMs);
  if (!response.ok) throw httpError(response.status, payload, endpoint, durationMs);
  const task = parseTask(payload);
  if (!task) {
    throw new ApiError("The gateway returned invalid JSON.", {
      endpoint,
      status: response.status,
      errorType: "invalid_json",
      durationMs,
    });
  }
  return task;
}

async function fetchOrThrow(endpoint: string, init: RequestInit, started: number): Promise<Response> {
  try {
    return await fetch(endpoint, init);
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new ApiError("The request was stopped.", {
        endpoint,
        status: null,
        errorType: "cancelled",
        durationMs: performance.now() - started,
      });
    }
    throw new ApiError("The gateway is unreachable.", {
      endpoint,
      status: null,
      errorType: "network",
      durationMs: performance.now() - started,
    });
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

function parseStage(value: unknown) {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.status !== "string") return [];
  return [
    {
      id: value.id,
      status: value.status,
      message: typeof value.message === "string" ? value.message : value.status,
      model: typeof value.model === "string" ? value.model : null,
      provider: typeof value.provider === "string" ? value.provider : null,
    },
  ];
}

function parseRegion(value: unknown): StudioRegion[] {
  if (!isRecord(value) || typeof value.id !== "string") return [];
  const bbox = parseBBox(value.bbox);
  return [
    {
      id: value.id,
      label: typeof value.label === "string" ? value.label : value.id,
      type: typeof value.type === "string" ? value.type : "region",
      bbox,
      confidence: typeof value.confidence === "number" ? value.confidence : 1,
      operation: typeof value.operation === "string" ? value.operation : "extract",
      edit_instruction: typeof value.edit_instruction === "string" ? value.edit_instruction : null,
    },
  ];
}

function parseBBox(value: unknown) {
  if (!isRecord(value)) return null;
  const x = value.x;
  const y = value.y;
  const width = value.width;
  const height = value.height;
  if (
    typeof x !== "number" ||
    typeof y !== "number" ||
    typeof width !== "number" ||
    typeof height !== "number"
  ) {
    return null;
  }
  return { x, y, width, height };
}

function parseFile(value: unknown) {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.role !== "string") return [];
  return [
    {
      id: value.id,
      role: value.role,
      name: typeof value.name === "string" ? value.name : value.id,
      media_type: typeof value.media_type === "string" ? value.media_type : "application/octet-stream",
      width: typeof value.width === "number" ? value.width : null,
      height: typeof value.height === "number" ? value.height : null,
      byte_size: typeof value.byte_size === "number" ? value.byte_size : 0,
      label: typeof value.label === "string" ? value.label : null,
      asset_id: typeof value.asset_id === "string" ? value.asset_id : null,
    },
  ];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
