import { X } from "lucide-react";

import { formatBytes, type DraftAttachment } from "../../services/attachments";
import styles from "./AttachmentPreview.module.css";

export function AttachmentPreview({
  items,
  onRemove,
}: {
  items: DraftAttachment[];
  onRemove: (id: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <ul className={styles.list} aria-label="Attachments">
      {items.map((item) => (
        <li key={item.id} className={item.supported ? styles.item : styles.unsupported}>
          {item.previewUrl ? <img src={item.previewUrl} alt="" /> : <span className={styles.placeholder} aria-hidden="true" />}
          <span className={styles.meta}>
            <span className={styles.name}>{item.name}</span>
            <span className={styles.size}>{formatBytes(item.size)}</span>
            {item.reason ? <span>{item.reason}</span> : null}
          </span>
          <button type="button" className={styles.remove} aria-label={`Remove ${item.name}`} onClick={() => onRemove(item.id)}>
            <X size={14} aria-hidden="true" />
          </button>
        </li>
      ))}
    </ul>
  );
}
