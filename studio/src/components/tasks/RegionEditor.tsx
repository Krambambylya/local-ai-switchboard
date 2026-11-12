import { useMemo, useState } from "react";

import type { StudioBBox, StudioRegion } from "../../types/api";
import styles from "./RegionEditor.module.css";

export function RegionEditor({
  imageUrl,
  regions,
  onChange,
  onApprove,
  disabled,
}: {
  imageUrl: string | null;
  regions: StudioRegion[];
  onChange: (regions: StudioRegion[]) => void;
  onApprove: () => void;
  disabled?: boolean;
}) {
  const [selected, setSelected] = useState(regions[0]?.id ?? null);
  const active = regions.find((item) => item.id === selected) ?? regions[0] ?? null;

  const boxes = useMemo(
    () => regions.filter((item): item is StudioRegion & { bbox: StudioBBox } => item.bbox !== null),
    [regions],
  );

  function updateActive(patch: Partial<StudioRegion>) {
    if (!active) return;
    onChange(regions.map((item) => (item.id === active.id ? { ...item, ...patch } : item)));
  }

  function updateBBox(patch: Partial<StudioBBox>) {
    if (!active?.bbox) return;
    updateActive({ bbox: clampBox({ ...active.bbox, ...patch }) });
  }

  function removeActive() {
    if (!active) return;
    const next = regions.filter((item) => item.id !== active.id);
    onChange(next);
    setSelected(next[0]?.id ?? null);
  }

  function addRegion() {
    const id = `region_${regions.length + 1}`;
    const next: StudioRegion = {
      id,
      label: `Region ${regions.length + 1}`,
      type: "region",
      bbox: { x: 0.1, y: 0.1, width: 0.3, height: 0.3 },
      confidence: 1,
      operation: "extract",
      edit_instruction: null,
    };
    onChange([...regions, next]);
    setSelected(id);
  }

  return (
    <div className={styles.panel}>
      <p className={styles.title}>Confirm regions</p>
      <div className={styles.canvas}>
        {imageUrl ? <img src={imageUrl} alt="Source for region editing" className={styles.image} /> : null}
        {boxes.map((item) => (
          <button
            key={item.id}
            type="button"
            className={item.id === active?.id ? styles.boxActive : styles.box}
            style={{
              left: `${item.bbox.x * 100}%`,
              top: `${item.bbox.y * 100}%`,
              width: `${item.bbox.width * 100}%`,
              height: `${item.bbox.height * 100}%`,
            }}
            aria-label={item.label}
            onClick={() => setSelected(item.id)}
          />
        ))}
      </div>
      {active?.bbox ? (
        <div className={styles.controls}>
          <label>
            Label
            <input
              value={active.label}
              disabled={disabled}
              onChange={(event) => updateActive({ label: event.target.value })}
            />
          </label>
          <label>
            X
            <input
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={active.bbox.x}
              disabled={disabled}
              onChange={(event) => updateBBox({ x: Number(event.target.value) })}
            />
          </label>
          <label>
            Y
            <input
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={active.bbox.y}
              disabled={disabled}
              onChange={(event) => updateBBox({ y: Number(event.target.value) })}
            />
          </label>
          <label>
            Width
            <input
              type="number"
              min={0.01}
              max={1}
              step={0.01}
              value={active.bbox.width}
              disabled={disabled}
              onChange={(event) => updateBBox({ width: Number(event.target.value) })}
            />
          </label>
          <label>
            Height
            <input
              type="number"
              min={0.01}
              max={1}
              step={0.01}
              value={active.bbox.height}
              disabled={disabled}
              onChange={(event) => updateBBox({ height: Number(event.target.value) })}
            />
          </label>
        </div>
      ) : null}
      <div className={styles.actions}>
        <button type="button" disabled={disabled} onClick={addRegion}>
          Add region
        </button>
        <button type="button" disabled={disabled || !active} onClick={removeActive}>
          Remove
        </button>
        <button type="button" className={styles.primary} disabled={disabled || boxes.length === 0} onClick={onApprove}>
          Confirm plan
        </button>
      </div>
    </div>
  );
}

function clampBox(box: StudioBBox): StudioBBox {
  const x = Math.min(0.99, Math.max(0, box.x));
  const y = Math.min(0.99, Math.max(0, box.y));
  const width = Math.min(1 - x, Math.max(0.01, box.width));
  const height = Math.min(1 - y, Math.max(0.01, box.height));
  return { x, y, width, height };
}
