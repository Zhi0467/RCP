import { graphTargetUrl, MAIN_GRAPH, sameGraphTarget } from "../core/graphTarget";
import type { GraphTargetRef } from "../core/types";
import type { ChatSummary, ChatSummaryPage, ChatTranscript } from "../core/types";

export type ChatPageRequest = (path: string) => Promise<ChatSummaryPage>;

export const CHAT_SUMMARY_PAGE_SIZE = 200;

export async function loadChatSummaryPage(
  apiBase: string,
  offset: number,
  request: ChatPageRequest,
  graphTarget: GraphTargetRef = MAIN_GRAPH,
): Promise<ChatSummaryPage> {
  const page = await request(
    graphTargetUrl(
      `${apiBase}/chats?offset=${offset}&limit=${CHAT_SUMMARY_PAGE_SIZE}`,
      graphTarget,
    ),
  );
  if (page.items.some((item) => !sameGraphTarget(item.graph_target, graphTarget)))
    throw new Error("Conversation list returned a different graph target.");
  return page;
}

/** Load the whole project before applying viewer filters, including older branches. */
export async function loadChatInventory(
  apiBase: string,
  request: ChatPageRequest,
): Promise<ChatSummary[]> {
  let items: ChatSummary[] = [];
  let offset = 0;
  while (true) {
    const page = await request(
      `${apiBase}/chats?inventory=true&offset=${offset}&limit=${CHAT_SUMMARY_PAGE_SIZE}`,
    );
    items = mergeChatSummaryPage(items, page.items, "append");
    const next = nextChatSummaryOffset(page);
    if (next >= page.total) return items;
    if (next <= offset) throw new Error("Conversation inventory paging made no progress.");
    offset = next;
  }
}

/** Selection is exact-target even though the Agents inventory spans the project. */
export function chatSummariesForTarget(
  summaries: readonly ChatSummary[],
  graphTarget: GraphTargetRef,
): ChatSummary[] {
  return summaries.filter((summary) => sameGraphTarget(summary.graph_target, graphTarget));
}

export function mergeChatSummaryPage(
  current: ChatSummary[],
  page: ChatSummary[],
  placement: "append" | "refresh",
): ChatSummary[] {
  const ordered = placement === "refresh" ? page : [...current, ...page];
  const seen = new Set<string>();
  return ordered.filter((summary) => {
    if (seen.has(summary.chat_id)) return false;
    seen.add(summary.chat_id);
    return true;
  });
}

export function nextChatSummaryOffset(page: ChatSummaryPage): number {
  return page.offset + page.items.length;
}

export interface ChatSelectionReconciliation {
  selectedChatId: string | null;
  retainedSummary: ChatSummary | null;
  deleteTranscript: boolean;
}

export function reconcileChatSelectionAfterRefresh(
  selectedChatId: string | null,
  previousSummary: ChatSummary | null,
  refreshedSummaries: ChatSummary[],
  validation: ChatTranscript | null | undefined,
): ChatSelectionReconciliation {
  if (!selectedChatId) {
    return { selectedChatId: null, retainedSummary: null, deleteTranscript: false };
  }
  if (refreshedSummaries.some((summary) => summary.chat_id === selectedChatId)) {
    return { selectedChatId, retainedSummary: null, deleteTranscript: false };
  }
  if (!previousSummary) {
    return { selectedChatId, retainedSummary: null, deleteTranscript: false };
  }
  if (validation) {
    return { selectedChatId, retainedSummary: validation, deleteTranscript: false };
  }
  if (validation === null) {
    return { selectedChatId: null, retainedSummary: null, deleteTranscript: true };
  }
  return { selectedChatId, retainedSummary: previousSummary, deleteTranscript: false };
}

export async function loadChatTranscript(
  apiBase: string,
  chatId: string,
  request: (path: string) => Promise<ChatTranscript>,
  graphTarget: GraphTargetRef = MAIN_GRAPH,
): Promise<ChatTranscript> {
  const transcript = await request(
    graphTargetUrl(`${apiBase}/chats/${encodeURIComponent(chatId)}`, graphTarget),
  );
  if (!sameGraphTarget(transcript.graph_target, graphTarget))
    throw new Error("Conversation returned a different graph target.");
  return transcript;
}
