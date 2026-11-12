import { Copy, RotateCcw } from "lucide-react";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { formatBytes } from "../../services/attachments";
import type { ChatMessage, StudioRegion } from "../../types/api";
import { TaskResult } from "../tasks/TaskResult";
import styles from "./MessageBubble.module.css";

export function MessageBubble({
  message,
  onRetry,
  onRegionsChange,
  onApproveTask,
}: {
  message: ChatMessage;
  onRetry?: () => void;
  onRegionsChange?: (regions: StudioRegion[]) => void;
  onApproveTask?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const pending = message.status === "pending";
  const task = message.task;

  async function copy() {
    try {
      await navigator.clipboard.writeText(message.content || task?.summary || "");
      setCopied(true);
    } catch {
      console.info("studio api", { endpoint: "clipboard", status: null, errorType: "clipboard", durationMs: 0 });
    }
  }

  return (
    <article className={message.role === "user" ? styles.user : styles.assistant}>
      <header className={styles.meta}>
        <span>{message.role === "user" ? "You" : "Assistant"}</span>
        <time dateTime={message.createdAt}>{formatTime(message.createdAt)}</time>
        {message.role === "assistant" && message.status === "complete" && (message.content || task?.summary) ? (
          <button type="button" className={styles.action} aria-label="Copy response" onClick={() => void copy()}>
            <Copy size={13} aria-hidden="true" />
            {copied ? "Copied" : "Copy"}
          </button>
        ) : null}
        {message.status === "error" && onRetry ? (
          <button type="button" className={styles.action} onClick={onRetry}>
            <RotateCcw size={13} aria-hidden="true" />
            Retry
          </button>
        ) : null}
      </header>
      {message.attachments && message.attachments.length > 0 ? (
        <ul className={styles.attachments} aria-label="Attached files">
          {message.attachments.map((item) => (
            <li key={`${item.name}-${item.size}`}>
              {item.name} · {formatBytes(item.size)}
            </li>
          ))}
        </ul>
      ) : null}
      {message.reasoning ? (
        <details className={styles.thinking}>
          <summary>Thinking</summary>
          <div className={styles.markdown}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.reasoning}</ReactMarkdown>
          </div>
        </details>
      ) : null}
      {message.role === "user" ? (
        <p className={styles.text}>{message.content}</p>
      ) : message.content && !task ? (
        <div className={styles.markdown}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ img: ImageFallback }}>
            {message.content}
          </ReactMarkdown>
        </div>
      ) : null}
      {task ? (
        <TaskResult
          task={task}
          onRegionsChange={onRegionsChange}
          onApprove={onApproveTask}
          busy={pending && task.status !== "awaiting_approval"}
        />
      ) : null}
      {pending && !task ? <p className={styles.pending}>Generating</p> : null}
      {pending && task && task.status !== "awaiting_approval" ? (
        <p className={styles.pending}>{task.message || "Working"}</p>
      ) : null}
      {message.error ? <p className={styles.error} role="alert">{message.error}</p> : null}
    </article>
  );
}

function ImageFallback({ alt }: { alt?: string }) {
  return <span>{alt || "image"}</span>;
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
