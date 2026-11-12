import { useEffect, useRef, useState } from "react";

import { useChat } from "../../features/chat/ChatProvider";
import { createDraftAttachment, revokeAttachment, type DraftAttachment } from "../../services/attachments";
import { Composer } from "./Composer";
import { EmptyState } from "./EmptyState";
import { MessageList } from "./MessageList";
import styles from "./ChatWorkspace.module.css";

export function ChatWorkspace() {
  const chat = useChat();
  const active = chat.activeChat;
  const chatId = active?.id ?? "";
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [attachments, setAttachments] = useState<Record<string, DraftAttachment[]>>({});
  const attachmentsRef = useRef(attachments);
  attachmentsRef.current = attachments;
  const bottomRef = useRef<HTMLDivElement>(null);
  const draft = drafts[chatId] ?? "";
  const files = attachments[chatId] ?? [];
  const generating = chat.inflightChatId !== null;
  const readyModel = chat.modelsPhase === "ready" && !chat.modelMissing;
  const canSend = draft.trim().length > 0 && !generating && readyModel && files.length === 0;
  const notice = noticeFor(chat.gateway.phase, chat.modelsPhase, chat.models.length, chat.modelMissing);

  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ block: "end" });
  }, [active?.messages]);

  useEffect(() => () => {
    for (const group of Object.values(attachmentsRef.current)) {
      for (const item of group) revokeAttachment(item);
    }
  }, []);

  function addFiles(list: File[]) {
    if (!chatId || list.length === 0) return;
    const next = list.slice(0, 8).map(createDraftAttachment);
    setAttachments((current) => {
      const existing = current[chatId] ?? [];
      return { ...current, [chatId]: [...existing, ...next].slice(0, 8) };
    });
  }

  function removeFile(id: string) {
    setAttachments((current) => {
      const existing = current[chatId] ?? [];
      const target = existing.find((item) => item.id === id);
      if (target) revokeAttachment(target);
      return { ...current, [chatId]: existing.filter((item) => item.id !== id) };
    });
  }

  function submit() {
    if (!canSend) return;
    const text = draft;
    setDrafts((current) => ({ ...current, [chatId]: "" }));
    chat.sendMessage(text);
  }

  return (
    <section className={styles.workspace} aria-label="Chat">
      <div className={styles.scroll}>
        {active && active.messages.length > 0 ? (
          <MessageList messages={active.messages} onRetry={() => chat.retryMessage(active.id)} />
        ) : (
          <EmptyState model={chat.selectedModel} onExample={(example) => setDrafts((current) => ({ ...current, [chatId]: example }))} />
        )}
        <div ref={bottomRef} />
      </div>
      {chat.storageWarning ? <p className={styles.warning} role="alert">This browser could not save the chat history.</p> : null}
      <Composer
        value={draft}
        attachments={files}
        canSend={canSend}
        generating={generating}
        notice={notice}
        onChange={(value) => setDrafts((current) => ({ ...current, [chatId]: value }))}
        onSubmit={submit}
        onStop={chat.stop}
        onAttach={addFiles}
        onRemove={removeFile}
      />
    </section>
  );
}

function noticeFor(
  phase: "loading" | "online" | "offline",
  modelsPhase: "loading" | "ready" | "error",
  modelCount: number,
  modelMissing: boolean,
): string | null {
  if (phase === "offline") return "The gateway is unreachable.";
  if (modelsPhase === "error" || (modelsPhase === "ready" && modelCount === 0)) return "No models are available.";
  if (modelMissing) return "The selected model is no longer available.";
  return null;
}
