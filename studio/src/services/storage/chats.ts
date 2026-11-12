import { createId } from "../id";
import { parseTask } from "../api/studioTasks";
import type { ChatMessage, ChatRecord, MessageAttachmentMeta, StoredStudio } from "../../types/api";

export const STORAGE_KEY = "las.studio.v1";

export function loadState(): StoredStudio | null {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) return null;
  try {
    return parseStored(JSON.parse(raw) as unknown);
  } catch {
    return null;
  }
}

export function saveState(state: StoredStudio): boolean {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    return true;
  } catch {
    console.info("studio api", { endpoint: "localStorage", status: null, errorType: "storage", durationMs: 0 });
    return false;
  }
}

export function createChat(now = new Date().toISOString()): ChatRecord {
  return {
    id: createId(),
    title: "New chat",
    createdAt: now,
    updatedAt: now,
    messages: [],
  };
}

export function titleFromMessage(content: string): string {
  const line = content.replace(/\s+/g, " ").trim();
  if (!line) return "New chat";
  return line.length > 48 ? `${line.slice(0, 48)}…` : line;
}

export function initialState(): StoredStudio {
  const stored = loadState();
  if (stored && stored.chats.length > 0) {
    const active = stored.chats.some((chat) => chat.id === stored.activeChatId)
      ? stored.activeChatId
      : stored.chats[0]?.id ?? createChat().id;
    return { ...stored, activeChatId: active };
  }
  const chat = createChat();
  return {
    version: 1,
    selectedModel: stored?.selectedModel ?? "",
    activeChatId: chat.id,
    chats: [chat],
  };
}

function parseStored(value: unknown): StoredStudio | null {
  if (!isRecord(value) || value.version !== 1 || !Array.isArray(value.chats)) return null;
  if (typeof value.selectedModel !== "string" || typeof value.activeChatId !== "string") return null;
  const chats = value.chats.flatMap(parseChat);
  if (chats.length !== value.chats.length) return null;
  return { version: 1, selectedModel: value.selectedModel, activeChatId: value.activeChatId, chats };
}

function parseChat(value: unknown): ChatRecord[] {
  if (!isRecord(value)) return [];
  if (typeof value.id !== "string" || typeof value.title !== "string") return [];
  if (typeof value.createdAt !== "string" || typeof value.updatedAt !== "string") return [];
  if (!Array.isArray(value.messages)) return [];
  const messages = value.messages.flatMap(parseMessage);
  if (messages.length !== value.messages.length) return [];
  return [{ id: value.id, title: value.title, createdAt: value.createdAt, updatedAt: value.updatedAt, messages }];
}

function parseMessage(value: unknown): ChatMessage[] {
  if (!isRecord(value)) return [];
  if (typeof value.id !== "string" || typeof value.content !== "string" || typeof value.reasoning !== "string") return [];
  if (typeof value.createdAt !== "string") return [];
  if (value.role !== "user" && value.role !== "assistant") return [];
  if (value.status !== "complete" && value.status !== "pending" && value.status !== "error") return [];
  if (value.error !== null && typeof value.error !== "string") return [];
  const attachments = Array.isArray(value.attachments) ? value.attachments.flatMap(parseAttachment) : undefined;
  const task = value.task == null ? null : parseTask(value.task);
  if (value.task != null && task == null) return [];
  const message: ChatMessage = {
    id: value.id,
    role: value.role,
    content: value.content,
    reasoning: value.reasoning,
    createdAt: value.createdAt,
    status: value.status,
    error: value.error,
    attachments,
    task,
  };
  if (message.status !== "pending") return [message];
  if (message.content || message.reasoning || message.task) return [{ ...message, status: "complete", error: null }];
  return [{ ...message, status: "error", error: "The request was interrupted." }];
}

function parseAttachment(value: unknown): MessageAttachmentMeta[] {
  if (!isRecord(value) || typeof value.name !== "string") return [];
  return [
    {
      name: value.name,
      size: typeof value.size === "number" ? value.size : 0,
      type: typeof value.type === "string" ? value.type : "application/octet-stream",
    },
  ];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
