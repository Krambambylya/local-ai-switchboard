import type { ChatMessage, HistoryTurn } from "../../types/api";

export const TOKEN_BUDGET = 6000;

export function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

export function selectHistory(messages: HistoryTurn[], budget = TOKEN_BUDGET): HistoryTurn[] {
  const source = messages.filter((message) => message.content.length > 0);
  const selected: HistoryTurn[] = [];
  let used = 0;
  for (let index = source.length - 1; index >= 0; index -= 1) {
    const message = source[index];
    if (!message) continue;
    const cost = estimateTokens(message.content);
    if (selected.length > 0 && used + cost > budget) break;
    selected.push({ role: message.role, content: message.content });
    used += cost;
  }
  return selected.reverse();
}

export function historyFromMessages(messages: ChatMessage[]): HistoryTurn[] {
  return messages
    .filter((message) => message.status !== "pending" && message.content.length > 0)
    .map((message) => ({ role: message.role, content: message.content }));
}
