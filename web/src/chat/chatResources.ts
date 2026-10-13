import { api } from "../core/api";
import { MAIN_GRAPH } from "../core/graphTarget";
import type { ResourceKey, ResourceProfile } from "../core/resourceStore";
import type {
  ChatBrowserPreference,
  ChatReads,
  ChatSummary,
  ChatTranscript,
  GraphTargetRef,
} from "../core/types";
import { loadChatInventory, loadChatTranscript } from "./chatApi";
import { mergeChatReads } from "./chatWorkspace";

export interface ChatsResource {
  summaries: ChatSummary[];
  reads: ChatReads;
}

export function chatsResourceKey(projectId: string): ResourceKey {
  return { backend: "", member: "", project: projectId, name: "chats" };
}

export function chatResourceKey(
  projectId: string,
  ref: GraphTargetRef | string,
  chatId: string,
): ResourceKey {
  const target =
    typeof ref === "string" ? ref : ref.kind === "main" ? "main" : `branch:${ref.branch_id}`;
  return { backend: "", member: "", project: projectId, name: "chat", ref: target, entity: chatId };
}

export function chatsResourceProfile(
  projectId: string,
  apiBase: string,
): ResourceProfile<ChatsResource> {
  return {
    key: chatsResourceKey(projectId),
    async fetch(signal) {
      const [summaries, reads] = await Promise.all([
        loadChatInventory(apiBase, (path) => api(path, { signal })),
        api<ChatReads>(`${apiBase}/chat-reads?inventory=true`, { signal }),
      ]);
      return { summaries, reads };
    },
    reconcile: (previous, next) => ({
      ...next,
      reads: mergeChatReads(previous?.reads ?? null, next.reads),
    }),
  };
}

export function chatResourceProfile(
  projectId: string,
  ref: GraphTargetRef,
  chatId: string,
  apiBase: string,
): ResourceProfile<ChatTranscript> {
  const target = ref.kind === "main" ? MAIN_GRAPH : { ...ref };
  return {
    key: chatResourceKey(projectId, target, chatId),
    fetch: (signal) => loadChatTranscript(apiBase, chatId, (path) => api(path, { signal }), target),
  };
}

export function chatBrowserProfile(
  projectId: string,
  apiBase: string,
  chatId: string,
): ResourceProfile<ChatBrowserPreference> {
  return {
    key: { ...chatsResourceKey(projectId), query: `browser:${chatId}` },
    fetch: (signal) => api(`${apiBase}/chats/${encodeURIComponent(chatId)}/browser`, { signal }),
  };
}
