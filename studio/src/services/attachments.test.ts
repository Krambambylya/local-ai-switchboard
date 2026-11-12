import { describe, expect, it } from "vitest";

import { IMAGE_ANALYSIS_MESSAGE, inspectAttachment, MAX_ATTACHMENT_BYTES } from "./attachments";

describe("attachments", () => {
  it("accepts png, jpeg, and webp inside the size limit", () => {
    expect(inspectAttachment({ name: "shot.png", size: 12, type: "image/png" }).supported).toBe(true);
    expect(inspectAttachment({ name: "shot.jpg", size: 12, type: "image/jpeg" }).supported).toBe(true);
    expect(inspectAttachment({ name: "shot.webp", size: 12, type: "" }).supported).toBe(true);
  });

  it("rejects other types and files over 8 MB", () => {
    expect(inspectAttachment({ name: "notes.txt", size: 12, type: "text/plain" }).reason).toBe("This file type is not supported.");
    expect(inspectAttachment({ name: "huge.png", size: MAX_ATTACHMENT_BYTES + 1, type: "image/png" }).reason).toBe("File is larger than 8 MB.");
  });

  it("uses the image-analysis message for blocked sends", () => {
    expect(IMAGE_ANALYSIS_MESSAGE).toBe("Анализ изображений пока не подключён");
  });
});
