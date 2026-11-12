import { Download } from "lucide-react";

import { formatBytes } from "../../services/attachments";
import { archiveUrl, fileUrl } from "../../services/api/studioTasks";
import type { StudioFileMeta, StudioRegion, StudioTaskView } from "../../types/api";
import { RegionEditor } from "./RegionEditor";
import styles from "./TaskResult.module.css";

export function TaskResult({
  task,
  onRegionsChange,
  onApprove,
  busy,
}: {
  task: StudioTaskView;
  onRegionsChange?: (regions: StudioRegion[]) => void;
  onApprove?: () => void;
  busy?: boolean;
}) {
  const oriented = task.files.find((item) => item.role === "oriented") ?? null;
  const extracted = task.files.filter((item) => item.role === "extracted");
  const generated = task.files.filter((item) => item.role === "generated");
  const awaiting = task.status === "awaiting_approval";

  return (
    <div className={styles.panel}>
      {task.stages.length > 0 ? (
        <ol className={styles.stages} aria-label="Task stages">
          {task.stages.map((stage) => (
            <li key={stage.id} className={statusClass(stage.status)}>
              <span>{stage.message}</span>
              {stage.model || stage.provider ? (
                <span className={styles.meta}>
                  {[stage.provider, stage.model].filter(Boolean).join(" · ")}
                </span>
              ) : null}
            </li>
          ))}
        </ol>
      ) : null}

      {awaiting && onRegionsChange && onApprove ? (
        <RegionEditor
          imageUrl={oriented ? fileUrl(oriented.id) : null}
          regions={task.assets}
          onChange={onRegionsChange}
          onApprove={onApprove}
          disabled={busy}
        />
      ) : null}

      {task.summary ? <p className={styles.summary}>{task.summary}</p> : null}

      {task.assets.length > 0 && !awaiting ? (
        <div className={styles.section}>
          <h3>Detected objects</h3>
          <ul>
            {task.assets.map((asset) => (
              <li key={asset.id}>
                {asset.label}
                {asset.bbox
                  ? ` · ${Math.round(asset.bbox.width * 100)}% × ${Math.round(asset.bbox.height * 100)}%`
                  : ""}
                {` · ${asset.operation}`}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {extracted.length > 0 ? <FileGallery title="Extracted crops" files={extracted} /> : null}
      {generated.length > 0 ? <FileGallery title="Generated images" files={generated} compareWith={oriented} /> : null}

      {(task.models.analysis || task.models.image || task.models.review) ? (
        <div className={styles.section}>
          <h3>Models</h3>
          <ul>
            {Object.entries(task.models).map(([key, value]) => (
              <li key={key}>
                {key}: {value}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {task.switch_result ? <p className={styles.switch}>{task.switch_result}</p> : null}

      {task.files.length > 0 ? (
        <a className={styles.download} href={archiveUrl(task.id)}>
          <Download size={14} aria-hidden="true" />
          Download ZIP
        </a>
      ) : null}
    </div>
  );
}

function FileGallery({
  title,
  files,
  compareWith,
}: {
  title: string;
  files: StudioFileMeta[];
  compareWith?: StudioFileMeta | null;
}) {
  return (
    <div className={styles.section}>
      <h3>{title}</h3>
      <div className={styles.gallery}>
        {compareWith ? (
          <figure>
            <img src={fileUrl(compareWith.id)} alt="Original" />
            <figcaption>
              Original · {dimensions(compareWith)} · {formatBytes(compareWith.byte_size)}
            </figcaption>
          </figure>
        ) : null}
        {files.map((file) => (
          <figure key={file.id}>
            <a href={fileUrl(file.id)} download={file.name}>
              <img src={fileUrl(file.id)} alt={file.label || file.name} />
            </a>
            <figcaption>
              {file.label || file.name} · {dimensions(file)} · {formatBytes(file.byte_size)}
            </figcaption>
          </figure>
        ))}
      </div>
    </div>
  );
}

function dimensions(file: StudioFileMeta): string {
  if (file.width && file.height) return `${file.width}×${file.height}`;
  return "size unknown";
}

function statusClass(status: string): string {
  if (status === "succeeded") return styles.ok;
  if (status === "failed") return styles.bad;
  if (status === "running") return styles.run;
  return styles.wait;
}
