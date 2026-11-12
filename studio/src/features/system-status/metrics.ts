export interface MetricSlot {
  id: string;
  label: string;
  value: string;
  detail: string;
  connected: boolean;
}

export const HARDWARE_METRICS: MetricSlot[] = [
  { id: "cpu", label: "CPU Usage", value: "—", detail: "Not connected", connected: false },
  { id: "ram", label: "RAM Usage", value: "—", detail: "Not connected", connected: false },
  { id: "gpu", label: "GPU Usage", value: "—", detail: "Not connected", connected: false },
  { id: "vram", label: "VRAM Usage", value: "—", detail: "Not connected", connected: false },
  { id: "temperature", label: "GPU Temperature", value: "—", detail: "Not connected", connected: false },
  { id: "power", label: "GPU Power", value: "—", detail: "Not connected", connected: false },
  { id: "scheduler", label: "Scheduler State", value: "—", detail: "Not connected", connected: false },
  { id: "jobs", label: "Active Jobs", value: "—", detail: "Not connected", connected: false },
];

export function gatewayLabel(phase: "loading" | "online" | "offline"): string {
  if (phase === "loading") return "Checking";
  if (phase === "online") return "Online";
  return "Offline";
}

export function apiLabel(api: "loading" | "ready" | "degraded" | "offline"): string {
  if (api === "loading") return "Checking";
  if (api === "ready") return "Ready";
  if (api === "degraded") return "Degraded";
  return "Offline";
}

export function modelCountLabel(phase: "loading" | "ready" | "error", count: number): string {
  if (phase !== "ready") return "—";
  return String(count);
}
