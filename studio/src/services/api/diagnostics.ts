import { createId } from "../id";
import type { DiagnosticEvent } from "../../types/api";

export function createDiagnostic(
  event: Omit<DiagnosticEvent, "id" | "at" | "durationMs"> & { durationMs: number },
): DiagnosticEvent {
  const entry: DiagnosticEvent = {
    id: createId(),
    at: new Date().toISOString(),
    endpoint: event.endpoint,
    status: event.status,
    errorType: event.errorType,
    durationMs: Math.round(event.durationMs),
  };
  if (entry.errorType !== "ok" && entry.errorType !== "degraded") {
    console.info("studio api", {
      endpoint: entry.endpoint,
      status: entry.status,
      errorType: entry.errorType,
      durationMs: entry.durationMs,
    });
  }
  return entry;
}
