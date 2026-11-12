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

export interface MessageAttachmentMeta {
  name: string;
  size: number;
  type: string;
}

export interface StudioBBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface StudioRegion {
  id: string;
  label: string;
  type: string;
  bbox: StudioBBox | null;
  confidence: number;
  operation: string;
  edit_instruction: string | null;
}

export interface StudioFileMeta {
  id: string;
  role: string;
  name: string;
  media_type: string;
  width: number | null;
  height: number | null;
  byte_size: number;
  label: string | null;
  asset_id: string | null;
}

export interface StudioStage {
  id: string;
  status: string;
  message: string;
  model?: string | null;
  provider?: string | null;
}

export interface StudioTaskView {
  id: string;
  status: string;
  stage: string | null;
  message: string | null;
  summary: string | null;
  error: string | null;
  stages: StudioStage[];
  assets: StudioRegion[];
  files: StudioFileMeta[];
  models: Record<string, string>;
  switch_result: string | null;
  operation: string | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  reasoning: string;
  createdAt: string;
  status: "complete" | "pending" | "error";
  error: string | null;
  attachments?: MessageAttachmentMeta[];
  task?: StudioTaskView | null;
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
