import { describe, expect, it } from "vitest";

import { createDraftAttachment, inspectAttachment, MAX_ATTACHMENT_BYTES, MAX_ATTACHMENTS } from "./attachments";

describe("attachments", () => {
  it("accepts png, jpeg, and webp inside the size limit", () => {
    expect(inspectAttachment({ name: "shot.png", size: 12, type: "image/png" }).supported).toBe(true);
    expect(inspectAttachment({ name: "shot.jpg", size: 12, type: "image/jpeg" }).supported).toBe(true);
    expect(inspectAttachment({ name: "shot.webp", size: 12, type: "" }).supported).toBe(true);
  });

  it("rejects other types and files over 12 MB", () => {
    expect(inspectAttachment({ name: "notes.txt", size: 12, type: "text/plain" }).reason).toBe("This file type is not supported.");
    expect(inspectAttachment({ name: "huge.png", size: MAX_ATTACHMENT_BYTES + 1, type: "image/png" }).reason).toBe("File is larger than 12 MB.");
  });

  it("keeps the File for a supported upload", () => {
    const file = new File(["png"], "board.png", { type: "image/png" });
    const draft = createDraftAttachment(file);
    expect(draft.supported).toBe(true);
    expect(draft.file).toBe(file);
    expect(MAX_ATTACHMENTS).toBe(4);
  });
});
