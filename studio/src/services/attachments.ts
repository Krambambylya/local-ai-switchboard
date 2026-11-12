import { createId } from "./id";

export const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;
export const IMAGE_ANALYSIS_MESSAGE = "Анализ изображений пока не подключён";

const EXTENSIONS = new Map<string, string>([
  [".png", "image/png"],
  [".jpg", "image/jpeg"],
  [".jpeg", "image/jpeg"],
  [".webp", "image/webp"],
]);

export interface AttachmentInspection {
  supported: boolean;
  reason: string | null;
}

export interface DraftAttachment {
  id: string;
  name: string;
  size: number;
  type: string;
  previewUrl: string | null;
  supported: boolean;
  reason: string | null;
}

export function inspectAttachment(file: { name: string; size: number; type: string }): AttachmentInspection {
  const extension = extensionOf(file.name);
  const expected = extension ? EXTENSIONS.get(extension) ?? null : null;
  if (!expected || !typeMatches(expected, file.type)) {
    return { supported: false, reason: "This file type is not supported." };
  }
  if (file.size <= 0) return { supported: false, reason: "The file is empty." };
  if (file.size > MAX_ATTACHMENT_BYTES) return { supported: false, reason: "File is larger than 8 MB." };
  return { supported: true, reason: null };
}

export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export function createDraftAttachment(file: File): DraftAttachment {
  const inspection = inspectAttachment(file);
  return {
    id: createId(),
    name: file.name,
    size: file.size,
    type: file.type,
    previewUrl: previewFor(file, inspection.supported),
    supported: inspection.supported,
    reason: inspection.reason,
  };
}

export function revokeAttachment(attachment: DraftAttachment): void {
  if (!attachment.previewUrl || typeof URL.revokeObjectURL !== "function") return;
  URL.revokeObjectURL(attachment.previewUrl);
}

function previewFor(file: File, supported: boolean): string | null {
  if (!supported || typeof URL.createObjectURL !== "function") return null;
  return URL.createObjectURL(file);
}

function extensionOf(name: string): string | null {
  const index = name.lastIndexOf(".");
  if (index <= 0) return null;
  return name.slice(index).toLowerCase();
}

function typeMatches(expected: string, actual: string): boolean {
  if (!actual) return true;
  if (actual === expected) return true;
  return expected === "image/jpeg" && actual === "image/jpg";
}
