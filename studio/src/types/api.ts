export interface ModelInfo {
  id: string;
  object: string;
  created: number;
  owned_by: string;
}

export interface HistoryTurn {
  role: "user" | "assistant";
  content: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  reasoning: string;
  createdAt: string;
  status: "complete" | "pending" | "error";
  error: string | null;
}

export interface ChatRecord {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  messages: ChatMessage[];
}

export interface StoredStudio {
  version: 1;
  selectedModel: string;
  activeChatId: string;
  chats: ChatRecord[];
}

export interface DiagnosticEvent {
  id: string;
  at: string;
  endpoint: string;
  status: number | null;
  errorType: string;
  durationMs: number;
}

export interface GatewayView {
  phase: "loading" | "online" | "offline";
  api: "loading" | "ready" | "degraded" | "offline";
}

export type ModelsPhase = "loading" | "ready" | "error";
