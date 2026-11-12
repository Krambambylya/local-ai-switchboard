import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { resolveSelectedModel } from "../models/modelSelection";
import { ApiError, getReady, listModels, streamChat } from "../../services/api/client";
import { createDiagnostic } from "../../services/api/diagnostics";
import { historyFromMessages, selectHistory } from "../../services/api/history";
import {
  approveStudioTask,
  cancelStudioTask,
  createStudioTask,
  getStudioTask,
  isTerminalStatus,
  patchStudioRegions,
  retryStudioTask,
  TASK_POLL_MS,
  TASK_TIMEOUT_MS,
} from "../../services/api/studioTasks";
import { createId } from "../../services/id";
import { initialState, saveState, createChat, titleFromMessage } from "../../services/storage/chats";
import type {
  ChatMessage,
  ChatRecord,
  DiagnosticEvent,
  GatewayView,
  HistoryTurn,
  ModelInfo,
  ModelsPhase,
  StudioRegion,
  StudioTaskView,
} from "../../types/api";

interface ChatContextValue {
  chats: ChatRecord[];
  activeChat: ChatRecord | null;
  selectedModel: string;
  models: ModelInfo[];
  modelsPhase: ModelsPhase;
  modelMissing: boolean;
  gateway: GatewayView;
  diagnostics: DiagnosticEvent[];
  inflightChatId: string | null;
  storageWarning: boolean;
  setSelectedModel: (model: string) => void;
  newChat: () => void;
  openChat: (chatId: string) => void;
  deleteChat: (chatId: string) => void;
  sendMessage: (text: string, files?: File[]) => void;
  retryMessage: (chatId: string) => void;
  stop: () => void;
  updateTaskRegions: (messageId: string, regions: StudioRegion[]) => void;
  approveTask: (messageId: string) => void;
}

const ChatContext = createContext<ChatContextValue | null>(null);

export function ChatProvider({ children }: { children: ReactNode }) {
  const starting = useRef(initialState());
  const [chats, setChats] = useState<ChatRecord[]>(starting.current.chats);
  const [activeChatId, setActiveChatId] = useState(starting.current.activeChatId);
  const [selectedModel, setSelectedModelState] = useState(starting.current.selectedModel);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [modelsPhase, setModelsPhase] = useState<ModelsPhase>("loading");
  const [gateway, setGateway] = useState<GatewayView>({ phase: "loading", api: "loading" });
  const [diagnostics, setDiagnostics] = useState<DiagnosticEvent[]>([]);
  const [inflightChatId, setInflightChatId] = useState<string | null>(null);
  const [storageWarning, setStorageWarning] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const inflightRef = useRef<string | null>(null);
  const stateRef = useRef({ chats, activeChatId, selectedModel, models, modelsPhase });
  stateRef.current = { chats, activeChatId, selectedModel, models, modelsPhase };

  useEffect(() => {
    const saved = saveState({ version: 1, selectedModel, activeChatId, chats });
    setStorageWarning(!saved);
  }, [selectedModel, activeChatId, chats]);

  const pushDiagnostic = useCallback((event: Omit<DiagnosticEvent, "id" | "at">) => {
    setDiagnostics((current) => [createDiagnostic(event), ...current].slice(0, 30));
  }, []);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const ready = await getReady(signal);
      pushDiagnostic({
        endpoint: ready.endpoint,
        status: ready.status,
        errorType: ready.value,
        durationMs: ready.durationMs,
      });
      setGateway({ phase: "online", api: ready.value === "ok" ? "ready" : "degraded" });
    } catch (error) {
      if (isCancelled(error)) return;
      const failure = asApiError(error, "/health/ready");
      pushDiagnostic(failure);
      setGateway({ phase: "offline", api: "offline" });
    }
    try {
      const listed = await listModels(signal);
      pushDiagnostic({
        endpoint: listed.endpoint,
        status: listed.status,
        errorType: "ok",
        durationMs: listed.durationMs,
      });
      setModels(listed.value);
      setModelsPhase("ready");
      setSelectedModelState((current) => resolveSelectedModel(current, listed.value).selected);
    } catch (error) {
      if (isCancelled(error)) return;
      const failure = asApiError(error, "/v1/models");
      pushDiagnostic(failure);
      setModelsPhase("error");
    }
  }, [pushDiagnostic]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    const timer = window.setInterval(() => {
      void refresh(controller.signal);
    }, 20_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [refresh]);

  const applyTask = useCallback((chatId: string, assistantId: string, task: StudioTaskView, status?: ChatMessage["status"], error?: string | null) => {
    setChats((current) => updateMessage(current, chatId, assistantId, (message) => ({
      ...message,
      content: task.summary || message.content,
      task,
      status: status ?? (isTerminalStatus(task.status)
        ? task.status === "succeeded"
          ? "complete"
          : "error"
        : task.status === "awaiting_approval"
          ? "complete"
          : "pending"),
      error: error ?? (task.status === "failed" || task.status === "interrupted" ? task.error : null),
    })));
  }, []);

  const pollTask = useCallback(async (
    chatId: string,
    assistantId: string,
    taskId: string,
    signal: AbortSignal,
  ) => {
    const started = performance.now();
    while (!signal.aborted) {
      if (performance.now() - started > TASK_TIMEOUT_MS) {
        throw new ApiError("The request timed out.", {
          endpoint: `/v1/studio/tasks/${taskId}`,
          status: null,
          errorType: "timeout",
          durationMs: performance.now() - started,
        });
      }
      const result = await getStudioTask(taskId, signal);
      applyTask(chatId, assistantId, result.value);
      if (result.value.status === "awaiting_approval" || isTerminalStatus(result.value.status)) {
        pushDiagnostic({
          endpoint: result.endpoint,
          status: result.status,
          errorType: result.value.status === "succeeded" || result.value.status === "awaiting_approval" ? "ok" : "task_error",
          durationMs: result.durationMs,
        });
        return result.value;
      }
      await sleep(TASK_POLL_MS, signal);
    }
    throw new ApiError("The request was stopped.", {
      endpoint: `/v1/studio/tasks/${taskId}`,
      status: null,
      errorType: "cancelled",
      durationMs: performance.now() - started,
    });
  }, [applyTask, pushDiagnostic]);

  const runCompletion = useCallback(async (chatId: string, assistantId: string, model: string, history: HistoryTurn[]) => {
    const controller = new AbortController();
    abortRef.current = controller;
    inflightRef.current = chatId;
    setInflightChatId(chatId);
    try {
      const result = await streamChat({
        model,
        messages: history,
        signal: controller.signal,
        onDelta: (delta) => {
          setChats((current) => updateMessage(current, chatId, assistantId, (message) => ({
            ...message,
            content: message.content + delta.content,
            reasoning: message.reasoning + delta.reasoning,
          })));
        },
      });
      setChats((current) => updateMessage(current, chatId, assistantId, (message) => {
        if (!message.content && !message.reasoning) {
          return { ...message, status: "error", error: "The model returned an empty response." };
        }
        return { ...message, status: "complete", error: null };
      }));
      pushDiagnostic({
        endpoint: result.endpoint,
        status: result.status,
        errorType: "ok",
        durationMs: result.durationMs,
      });
    } catch (error) {
      const failure = asApiError(error, "/v1/chat/completions");
      if (failure.errorType === "cancelled") {
        setChats((current) => current.map((chat) => {
          if (chat.id !== chatId) return chat;
          const message = chat.messages.find((item) => item.id === assistantId);
          if (!message || (!message.content && !message.reasoning && !message.task)) {
            return { ...chat, messages: chat.messages.filter((item) => item.id !== assistantId) };
          }
          return {
            ...chat,
            messages: chat.messages.map((item) => item.id === assistantId ? { ...item, status: "complete", error: null } : item),
          };
        }));
        return;
      }
      pushDiagnostic(failure);
      setChats((current) => updateMessage(current, chatId, assistantId, (message) => ({
        ...message,
        status: "error",
        error: failure.message,
      })));
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      inflightRef.current = null;
      setInflightChatId(null);
    }
  }, [pushDiagnostic]);

  const runStudioTask = useCallback(async (chatId: string, assistantId: string, prompt: string, files: File[]) => {
    const controller = new AbortController();
    abortRef.current = controller;
    inflightRef.current = chatId;
    setInflightChatId(chatId);
    try {
      const created = await createStudioTask({
        prompt,
        conversationId: chatId,
        files,
        signal: controller.signal,
      });
      applyTask(chatId, assistantId, created.value, "pending");
      pushDiagnostic({
        endpoint: created.endpoint,
        status: created.status,
        errorType: "ok",
        durationMs: created.durationMs,
      });
      const task = await pollTask(chatId, assistantId, created.value.id, controller.signal);
      if (task.status === "failed" || task.status === "interrupted") {
        setChats((current) => updateMessage(current, chatId, assistantId, (message) => ({
          ...message,
          status: "error",
          error: task.error || "The task failed.",
        })));
      }
    } catch (error) {
      const failure = asApiError(error, "/v1/studio/tasks");
      if (failure.errorType === "cancelled") {
        const current = stateRef.current.chats
          .find((chat) => chat.id === chatId)
          ?.messages.find((message) => message.id === assistantId);
        if (current?.task?.id) {
          try {
            await cancelStudioTask(current.task.id);
          } catch {
            // The local abort already stopped polling.
          }
        }
        setChats((current) => current.map((chat) => {
          if (chat.id !== chatId) return chat;
          const message = chat.messages.find((item) => item.id === assistantId);
          if (!message || (!message.content && !message.task)) {
            return { ...chat, messages: chat.messages.filter((item) => item.id !== assistantId) };
          }
          return {
            ...chat,
            messages: chat.messages.map((item) => item.id === assistantId ? { ...item, status: "complete", error: null } : item),
          };
        }));
        return;
      }
      pushDiagnostic(failure);
      setChats((current) => updateMessage(current, chatId, assistantId, (message) => ({
        ...message,
        status: "error",
        error: failure.message,
      })));
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      inflightRef.current = null;
      setInflightChatId(null);
    }
  }, [applyTask, pollTask, pushDiagnostic]);

  const sendMessage = useCallback((text: string, files: File[] = []) => {
    if (inflightRef.current || text.trim().length === 0) return;
    const snapshot = stateRef.current;
    if (snapshot.modelsPhase !== "ready" || !snapshot.models.some((model) => model.id === snapshot.selectedModel)) return;
    const chat = snapshot.chats.find((item) => item.id === snapshot.activeChatId);
    if (!chat) return;
    const now = new Date().toISOString();
    const userMessage: ChatMessage = {
      id: createId(),
      role: "user",
      content: text,
      reasoning: "",
      createdAt: now,
      status: "complete",
      error: null,
      attachments: files.map((file) => ({ name: file.name, size: file.size, type: file.type })),
    };
    const assistantId = createId();
    const assistant: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      reasoning: "",
      createdAt: now,
      status: "pending",
      error: null,
      task: null,
    };
    const nextMessages = [...chat.messages, userMessage, assistant];
    setChats((current) => current.map((item) => {
      if (item.id !== chat.id) return item;
      const hasUser = item.messages.some((message) => message.role === "user");
      return {
        ...item,
        title: hasUser ? item.title : titleFromMessage(text),
        updatedAt: now,
        messages: nextMessages,
      };
    }));
    if (files.length > 0) {
      void runStudioTask(chat.id, assistantId, text, files);
      return;
    }
    void runCompletion(chat.id, assistantId, snapshot.selectedModel, selectHistory(historyFromMessages(nextMessages)));
  }, [runCompletion, runStudioTask]);

  const retryMessage = useCallback((chatId: string) => {
    if (inflightRef.current) return;
    const snapshot = stateRef.current;
    if (snapshot.modelsPhase !== "ready" || !snapshot.models.some((model) => model.id === snapshot.selectedModel)) return;
    const chat = snapshot.chats.find((item) => item.id === chatId);
    if (!chat) return;
    const last = chat.messages[chat.messages.length - 1];
    if (last?.role === "assistant" && last.status === "error" && last.task?.id) {
      const controller = new AbortController();
      abortRef.current = controller;
      inflightRef.current = chatId;
      setInflightChatId(chatId);
      void (async () => {
        try {
          const restarted = await retryStudioTask(last.task!.id, controller.signal);
          applyTask(chatId, last.id, restarted, "pending");
          const task = await pollTask(chatId, last.id, restarted.id, controller.signal);
          if (task.status === "failed" || task.status === "interrupted") {
            setChats((current) => updateMessage(current, chatId, last.id, (message) => ({
              ...message,
              status: "error",
              error: task.error || "The task failed.",
            })));
          }
        } catch (error) {
          const failure = asApiError(error, "/v1/studio/tasks");
          if (failure.errorType !== "cancelled") {
            pushDiagnostic(failure);
            setChats((current) => updateMessage(current, chatId, last.id, (message) => ({
              ...message,
              status: "error",
              error: failure.message,
            })));
          }
        } finally {
          if (abortRef.current === controller) abortRef.current = null;
          inflightRef.current = null;
          setInflightChatId(null);
        }
      })();
      return;
    }
    const kept = [...chat.messages];
    if (last?.role === "assistant" && last.status === "error") kept.pop();
    const now = new Date().toISOString();
    const assistantId = createId();
    const assistant: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      reasoning: "",
      createdAt: now,
      status: "pending",
      error: null,
    };
    const nextMessages = [...kept, assistant];
    setChats((current) => current.map((item) => item.id === chatId ? { ...item, updatedAt: now, messages: nextMessages } : item));
    void runCompletion(chatId, assistantId, snapshot.selectedModel, selectHistory(historyFromMessages(nextMessages)));
  }, [applyTask, pollTask, pushDiagnostic, runCompletion]);

  const updateTaskRegions = useCallback((messageId: string, regions: StudioRegion[]) => {
    const snapshot = stateRef.current;
    const chat = snapshot.chats.find((item) => item.id === snapshot.activeChatId);
    const message = chat?.messages.find((item) => item.id === messageId);
    if (!chat || !message?.task) return;
    setChats((current) => updateMessage(current, chat.id, messageId, (item) => ({
      ...item,
      task: item.task ? { ...item.task, assets: regions } : item.task,
    })));
    void patchStudioRegions(message.task.id, regions).then((task) => {
      applyTask(chat.id, messageId, task, "complete");
    }).catch((error) => {
      const failure = asApiError(error, "/v1/studio/tasks");
      pushDiagnostic(failure);
    });
  }, [applyTask, pushDiagnostic]);

  const approveTask = useCallback((messageId: string) => {
    if (inflightRef.current) return;
    const snapshot = stateRef.current;
    const chat = snapshot.chats.find((item) => item.id === snapshot.activeChatId);
    const message = chat?.messages.find((item) => item.id === messageId);
    if (!chat || !message?.task) return;
    const controller = new AbortController();
    abortRef.current = controller;
    inflightRef.current = chat.id;
    setInflightChatId(chat.id);
    void (async () => {
      try {
        const approved = await approveStudioTask(message.task!.id, controller.signal);
        applyTask(chat.id, messageId, approved, "pending");
        const task = await pollTask(chat.id, messageId, approved.id, controller.signal);
        if (task.status === "failed" || task.status === "interrupted") {
          setChats((current) => updateMessage(current, chat.id, messageId, (item) => ({
            ...item,
            status: "error",
            error: task.error || "The task failed.",
          })));
        }
      } catch (error) {
        const failure = asApiError(error, "/v1/studio/tasks");
        if (failure.errorType !== "cancelled") {
          pushDiagnostic(failure);
          setChats((current) => updateMessage(current, chat.id, messageId, (item) => ({
            ...item,
            status: "error",
            error: failure.message,
          })));
        }
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
        inflightRef.current = null;
        setInflightChatId(null);
      }
    })();
  }, [applyTask, pollTask, pushDiagnostic]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const newChat = useCallback(() => {
    const chat = createChat();
    setChats((current) => [chat, ...current]);
    setActiveChatId(chat.id);
  }, []);

  const openChat = useCallback((chatId: string) => {
    setActiveChatId(chatId);
  }, []);

  const deleteChat = useCallback((chatId: string) => {
    if (inflightRef.current === chatId) abortRef.current?.abort();
    const remaining = stateRef.current.chats.filter((chat) => chat.id !== chatId);
    const next = remaining.length > 0 ? remaining : [createChat()];
    const first = next[0];
    if (!first) return;
    setChats(next);
    setActiveChatId((current) => current === chatId ? first.id : current);
  }, []);

  const setSelectedModel = useCallback((model: string) => {
    setSelectedModelState(model);
  }, []);

  const activeChat = chats.find((chat) => chat.id === activeChatId) ?? null;
  const modelMissing = modelsPhase === "ready" && !models.some((model) => model.id === selectedModel);

  const value = useMemo<ChatContextValue>(() => ({
    chats,
    activeChat,
    selectedModel,
    models,
    modelsPhase,
    modelMissing,
    gateway,
    diagnostics,
    inflightChatId,
    storageWarning,
    setSelectedModel,
    newChat,
    openChat,
    deleteChat,
    sendMessage,
    retryMessage,
    stop,
    updateTaskRegions,
    approveTask,
  }), [
    chats,
    activeChat,
    selectedModel,
    models,
    modelsPhase,
    modelMissing,
    gateway,
    diagnostics,
    inflightChatId,
    storageWarning,
    setSelectedModel,
    newChat,
    openChat,
    deleteChat,
    sendMessage,
    retryMessage,
    stop,
    updateTaskRegions,
    approveTask,
  ]);

  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>;
}

export function useChat(): ChatContextValue {
  const value = useContext(ChatContext);
  if (!value) throw new Error("ChatProvider is missing");
  return value;
}

function updateMessage(
  chats: ChatRecord[],
  chatId: string,
  messageId: string,
  update: (message: ChatMessage) => ChatMessage,
): ChatRecord[] {
  return chats.map((chat) => {
    if (chat.id !== chatId) return chat;
    return {
      ...chat,
      updatedAt: new Date().toISOString(),
      messages: chat.messages.map((message) => message.id === messageId ? update(message) : message),
    };
  });
}

interface Failure {
  endpoint: string;
  status: number | null;
  errorType: string;
  durationMs: number;
  message: string;
}

function asApiError(error: unknown, endpoint: string): Failure {
  if (error instanceof ApiError) {
    return {
      endpoint: error.endpoint,
      status: error.status,
      errorType: error.errorType,
      durationMs: error.durationMs,
      message: error.message,
    };
  }
  return {
    endpoint,
    status: null,
    errorType: "network",
    durationMs: 0,
    message: "The gateway is unreachable.",
  };
}

function isCancelled(error: unknown): boolean {
  return error instanceof ApiError && error.errorType === "cancelled";
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new ApiError("The request was stopped.", {
        endpoint: "/v1/studio/tasks",
        status: null,
        errorType: "cancelled",
        durationMs: 0,
      }));
      return;
    }
    const timer = window.setTimeout(resolve, ms);
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new ApiError("The request was stopped.", {
        endpoint: "/v1/studio/tasks",
        status: null,
        errorType: "cancelled",
        durationMs: 0,
      }));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}
