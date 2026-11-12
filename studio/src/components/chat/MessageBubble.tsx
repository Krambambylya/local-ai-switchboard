import { Copy, RotateCcw } from "lucide-react";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { ChatMessage } from "../../types/api";
import styles from "./MessageBubble.module.css";

export function MessageBubble({ message, onRetry }: { message: ChatMessage; onRetry?: () => void }) {
  const [copied, setCopied] = useState(false);
  const pending = message.status === "pending";

  async function copy() {
    try {
      await navigator.clipboard.writeText(message.content);
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
        {message.role === "assistant" && message.status === "complete" && message.content ? (
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
      ) : message.content ? (
        <div className={styles.markdown}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ img: ImageFallback }}>
            {message.content}
          </ReactMarkdown>
        </div>
      ) : null}
      {pending ? <p className={styles.pending}>Generating</p> : null}
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
