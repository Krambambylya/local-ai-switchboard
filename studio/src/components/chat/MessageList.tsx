import type { ChatMessage } from "../../types/api";
import styles from "./MessageList.module.css";
import { MessageBubble } from "./MessageBubble";

export function MessageList({
  messages,
  onRetry,
}: {
  messages: ChatMessage[];
  onRetry: () => void;
}) {
  return (
    <div className={styles.list} aria-live="polite">
      {messages.map((message, index) => (
        <MessageBubble
          key={message.id}
          message={message}
          onRetry={index === messages.length - 1 ? onRetry : undefined}
        />
      ))}
    </div>
  );
}
