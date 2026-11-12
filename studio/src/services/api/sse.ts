export type SsePiece =
  | { kind: "delta"; content: string; reasoning: string }
  | { kind: "done" }
  | { kind: "error"; message: string }
  | { kind: "invalid" };

export function consumeSse(buffer: string): { pieces: SsePiece[]; rest: string } {
  const blocks = buffer.replace(/\r\n/g, "\n").split("\n\n");
  const rest = blocks.pop() ?? "";
  const pieces: SsePiece[] = [];
  for (const block of blocks) {
    const piece = parseSseBlock(block);
    if (piece) pieces.push(piece);
  }
  return { pieces, rest };
}

export function parseSseBlock(block: string): SsePiece | null {
  const data = block
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n")
    .trim();
  if (!data) return null;
  if (data === "[DONE]") return { kind: "done" };
  try {
    return pieceFromPayload(JSON.parse(data) as unknown);
  } catch {
    return { kind: "invalid" };
  }
}

export function readAssistantText(payload: unknown): { content: string; reasoning: string } | null {
  const piece = pieceFromPayload(payload);
  if (piece.kind !== "delta") return null;
  return { content: piece.content, reasoning: piece.reasoning };
}

function pieceFromPayload(payload: unknown): SsePiece {
  if (!isRecord(payload)) return { kind: "invalid" };
  const error = payload.error;
  if (isRecord(error)) {
    const message = typeof error.message === "string" ? error.message : "The gateway returned an error.";
    return { kind: "error", message };
  }
  const choices = payload.choices;
  if (!Array.isArray(choices) || choices.length === 0) {
    return { kind: "delta", content: "", reasoning: "" };
  }
  const first = choices[0];
  if (!isRecord(first)) return { kind: "invalid" };
  const source = isRecord(first.delta) ? first.delta : isRecord(first.message) ? first.message : null;
  if (!source) return { kind: "delta", content: "", reasoning: "" };
  return {
    kind: "delta",
    content: typeof source.content === "string" ? source.content : "",
    reasoning: typeof source.reasoning_content === "string" ? source.reasoning_content : "",
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
