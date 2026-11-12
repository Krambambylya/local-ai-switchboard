import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { useChat } from "../../features/chat/ChatProvider";
import { gatewayLabel } from "../../features/system-status/metrics";
import controls from "../../styles/controls.module.css";
import styles from "./NavPanel.module.css";

export function NavPanel({ onClose }: { onClose: () => void }) {
  const chat = useChat();
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);

  return (
    <aside className={styles.panel} aria-label="Navigation">
      <div className={styles.brand}>
        <span className={styles.mark} aria-hidden="true" />
        <div>
          <div className={styles.name}>Local AI Studio</div>
          <div className={styles.product}>Local AI workspace</div>
        </div>
      </div>
      <button
        type="button"
        className={`${controls.button} ${controls.primary} ${styles.newChat}`}
        onClick={() => {
          chat.newChat();
          closeIfNarrow(onClose);
        }}
      >
        <Plus size={15} aria-hidden="true" />
        New chat
      </button>
      <ul className={styles.list} aria-label="Chats">
        {chat.chats.map((item) => {
          const active = item.id === chat.activeChat?.id;
          const confirming = pendingDeleteId === item.id;
          return (
            <li key={item.id} className={active ? styles.active : styles.item}>
              <button
                type="button"
                className={styles.open}
                aria-label={`Open ${item.title}`}
                aria-current={active ? "true" : undefined}
                onClick={() => {
                  chat.openChat(item.id);
                  closeIfNarrow(onClose);
                }}
              >
                <span className={styles.chatTitle}>{item.title}</span>
                <time dateTime={item.updatedAt}>{formatTime(item.updatedAt)}</time>
              </button>
              {confirming ? (
                <div className={styles.confirm}>
                  <span>Delete this chat?</span>
                  <button
                    type="button"
                    className={`${controls.button} ${controls.danger}`}
                    onClick={() => {
                      chat.deleteChat(item.id);
                      setPendingDeleteId(null);
                    }}
                  >
                    Delete
                  </button>
                  <button type="button" className={`${controls.button} ${controls.quiet}`} onClick={() => setPendingDeleteId(null)}>
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  className={styles.delete}
                  aria-label={`Delete ${item.title}`}
                  onClick={() => setPendingDeleteId(item.id)}
                >
                  <Trash2 size={14} aria-hidden="true" />
                </button>
              )}
            </li>
          );
        })}
      </ul>
      <div className={styles.footer}>
        <label className={styles.model}>
          Model
          <select
            value={chat.selectedModel}
            disabled={chat.modelsPhase === "loading" || chat.models.length === 0}
            onChange={(event) => chat.setSelectedModel(event.target.value)}
          >
            {chat.models.map((model) => (
              <option key={model.id} value={model.id}>
                {model.id}
              </option>
            ))}
            {chat.modelMissing && chat.selectedModel && chat.models.length > 0 ? (
              <option value={chat.selectedModel}>{chat.selectedModel}</option>
            ) : null}
            {chat.models.length === 0 ? (
              <option value={chat.selectedModel}>{chat.modelsPhase === "loading" ? "Loading models" : "No models"}</option>
            ) : null}
          </select>
        </label>
        <p className={styles.gateway}>
          Gateway <strong>{gatewayLabel(chat.gateway.phase)}</strong>
          {chat.inflightChatId ? <span className={styles.generating}>Generating</span> : null}
        </p>
      </div>
    </aside>
  );
}

function closeIfNarrow(onClose: () => void): void {
  if (window.matchMedia("(max-width: 760px)").matches) onClose();
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
