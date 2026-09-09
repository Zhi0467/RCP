import { parseProjectHash } from "./experimentBoard";

/** The trusted preview shell asks the existing app window to open its chat. */
export function listenForArtifactChatNavigation(open: (hash: string) => Promise<void>) {
  const channel = new BroadcastChannel("rcp-artifact-chat-navigation");
  channel.onmessage = async ({ data }) => {
    if (typeof data?.requestId !== "string" || typeof data.hash !== "string") return;
    const route = parseProjectHash(data.hash);
    if (!route.projectId || route.view !== "chats" || !route.chatId) return;
    try {
      await open(data.hash);
      channel.postMessage({ requestId: data.requestId });
    } catch (error) {
      channel.postMessage({ requestId: data.requestId, error: String(error) });
    }
  };
  return () => channel.close();
}
