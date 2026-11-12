import type { ChatMessage, StudioRegion } from "../../types/api";
import styles from "./MessageList.module.css";
import { MessageBubble } from "./MessageBubble";

export function MessageList({
  messages,
  onRetry,
  onRegionsChange,
  onApproveTask,
}: {
  messages: ChatMessage[];
  onRetry: () => void;
  onRegionsChange?: (messageId: string, regions: StudioRegion[]) => void;
  onApproveTask?: (messageId: string) => void;
}) {
  return (
    <div className={styles.list} aria-live="polite">
      {messages.map((message, index) => (
        <MessageBubble
          key={message.id}
          message={message}
          onRetry={index === messages.length - 1 ? onRetry : undefined}
          onRegionsChange={
            onRegionsChange && message.task?.status === "awaiting_approval"
              ? (regions) => onRegionsChange(message.id, regions)
              : undefined
          }
          onApproveTask={
            onApproveTask && message.task?.status === "awaiting_approval"
              ? () => onApproveTask(message.id)
              : undefined
          }
        />
      ))}
    </div>
  );
}
