import { parseProjectHash } from "./experimentBoard";

/** The trusted preview shell asks the existing app window to open its chat. */
export function listenForArtifactChatNavigation(
  open: (hash: string, expiresAt: number) => Promise<void>,
) {
  const channel = new BroadcastChannel("rcp-artifact-chat-navigation");
  let closed = false;
  channel.onmessage = async ({ data }) => {
    if (
      typeof data?.requestId !== "string" ||
      typeof data.hash !== "string" ||
      !Number.isFinite(data.expiresAt)
    )
      return;
    const route = parseProjectHash(data.hash);
    if (!route.projectId || route.view !== "chats" || !route.chatId) return;
    try {
      await open(data.hash, data.expiresAt);
      if (!closed) channel.postMessage({ requestId: data.requestId });
    } catch (error) {
      if (!closed) channel.postMessage({ requestId: data.requestId, error: String(error) });
    }
  };
  return () => {
    closed = true;
    channel.close();
  };
}
