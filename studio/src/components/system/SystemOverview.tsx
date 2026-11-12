import { useChat } from "../../features/chat/ChatProvider";
import { HARDWARE_METRICS, apiLabel, gatewayLabel, modelCountLabel, type MetricSlot } from "../../features/system-status/metrics";
import styles from "./SystemOverview.module.css";

export function SystemOverview({ metrics = HARDWARE_METRICS }: { metrics?: MetricSlot[] }) {
  const chat = useChat();
  return (
    <aside className={styles.panel} aria-label="System overview">
      <section className={styles.block}>
        <h2>System Status</h2>
        <dl className={styles.status}>
          <div>
            <dt>Gateway</dt>
            <dd>{gatewayLabel(chat.gateway.phase)}</dd>
          </div>
          <div>
            <dt>API</dt>
            <dd>{apiLabel(chat.gateway.api)}</dd>
          </div>
          <div>
            <dt>Models</dt>
            <dd>{modelCountLabel(chat.modelsPhase, chat.models.length)}</dd>
          </div>
          <div>
            <dt>Active model</dt>
            <dd>{chat.selectedModel || "—"}</dd>
          </div>
        </dl>
      </section>
      <section className={styles.block}>
        <h2>Hardware Metrics</h2>
        <p className={styles.note}>Metrics integration pending</p>
        <div className={styles.grid}>
          {metrics.map((metric) => (
            <article key={metric.id} className={metric.connected ? styles.live : styles.pending} aria-label={metric.label}>
              <h3>{metric.label}</h3>
              <p className={styles.value}>{metric.value}</p>
              <p className={styles.detail}>{metric.detail}</p>
            </article>
          ))}
        </div>
      </section>
      <section className={styles.block}>
        <h2>Hardware Usage History</h2>
        <p className={styles.empty}>Metrics collection is not implemented yet.</p>
      </section>
      <section className={styles.block}>
        <h2>Diagnostics</h2>
        {chat.diagnostics.length === 0 ? <p className={styles.note}>No API calls recorded.</p> : (
          <ol className={styles.log} aria-label="Diagnostics">
            {chat.diagnostics.slice(0, 8).map((event) => (
              <li key={event.id}>
                <span>{event.endpoint}</span>
                <span>{event.status ?? "—"}</span>
                <span>{event.errorType}</span>
                <span>{event.durationMs}ms</span>
              </li>
            ))}
          </ol>
        )}
      </section>
    </aside>
  );
}
