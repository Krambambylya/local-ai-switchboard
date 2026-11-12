import styles from "./EmptyState.module.css";

export const EXAMPLES = [
  "Проанализируй изображение.",
  "Помоги разработать веб-интерфейс.",
  "Объясни архитектуру проекта.",
  "Составь план обработки графических ассетов.",
];

export function EmptyState({ model, onExample }: { model: string; onExample: (example: string) => void }) {
  return (
    <div className={styles.empty}>
      <h1>Local AI Studio</h1>
      <p>Local AI workspace</p>
      <p className={styles.model}>
        Model <span>{model || "—"}</span>
      </p>
      <div className={styles.examples}>
        {EXAMPLES.map((example) => (
          <button key={example} type="button" className={styles.example} onClick={() => onExample(example)}>
            {example}
          </button>
        ))}
      </div>
    </div>
  );
}
