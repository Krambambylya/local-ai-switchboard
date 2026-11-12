import { Paperclip, SendHorizontal, Square } from "lucide-react";
import { useState, type DragEvent, type FormEvent, type KeyboardEvent } from "react";

import { AttachmentPreview } from "../attachments/AttachmentPreview";
import { IMAGE_ANALYSIS_MESSAGE, type DraftAttachment } from "../../services/attachments";
import controls from "../../styles/controls.module.css";
import styles from "./Composer.module.css";

export function Composer({
  value,
  attachments,
  canSend,
  generating,
  notice,
  onChange,
  onSubmit,
  onStop,
  onAttach,
  onRemove,
}: {
  value: string;
  attachments: DraftAttachment[];
  canSend: boolean;
  generating: boolean;
  notice: string | null;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onStop: () => void;
  onAttach: (files: File[]) => void;
  onRemove: (id: string) => void;
}) {
  const [dragging, setDragging] = useState(false);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (canSend) onSubmit();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (canSend) onSubmit();
    }
  }

  function onDrop(event: DragEvent<HTMLFormElement>) {
    event.preventDefault();
    setDragging(false);
    onAttach(Array.from(event.dataTransfer.files));
  }

  return (
    <form
      className={dragging ? styles.dragging : styles.form}
      onSubmit={submit}
      onDragEnter={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
    >
      {attachments.length > 0 ? (
        <>
          <AttachmentPreview items={attachments} onRemove={onRemove} />
          <p className={styles.notice} role="status">{IMAGE_ANALYSIS_MESSAGE}</p>
        </>
      ) : null}
      {notice ? <p className={styles.notice} role="alert">{notice}</p> : null}
      <div className={styles.row}>
        <label className={styles.attach}>
          <Paperclip size={16} aria-hidden="true" />
          <input
            aria-label="Attach"
            className={styles.file}
            type="file"
            accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp"
            multiple
            onChange={(event) => {
              onAttach(Array.from(event.target.files ?? []));
              event.target.value = "";
            }}
          />
        </label>
        <textarea
          aria-label="Message"
          className={styles.input}
          rows={2}
          value={value}
          placeholder="Message"
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={onKeyDown}
          onPaste={(event) => {
            const images = Array.from(event.clipboardData.files).filter((file) => file.type.startsWith("image/"));
            if (images.length === 0) return;
            event.preventDefault();
            onAttach(images);
          }}
        />
        {generating ? (
          <button type="button" className={`${controls.button} ${styles.send}`} aria-label="Stop" onClick={onStop}>
            <Square size={14} aria-hidden="true" />
            Stop
          </button>
        ) : (
          <button type="submit" className={`${controls.button} ${controls.primary} ${styles.send}`} aria-label="Send" disabled={!canSend}>
            <SendHorizontal size={14} aria-hidden="true" />
            Send
          </button>
        )}
      </div>
    </form>
  );
}
