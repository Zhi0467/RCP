import type { ChatAttachmentDescriptor, ProjectCacheMetrics, ProjectSnapshot } from "./types";

type MutationFailureHandler = (path: string) => Promise<void>;

let mutationFailureHandler: MutationFailureHandler | null = null;
let pinnedInstanceId: string | null = null;

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const mutation = isMutationRequest(init);
  const headers = new Headers(init?.headers);
  if (!headers.has("Content-Type") && !(init?.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (mutation && pinnedInstanceId) headers.set("X-RCP-Instance-ID", pinnedInstanceId);
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers,
    });
  } catch (error) {
    if (mutation) await notifyMutationFailure(path);
    throw error;
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    if (mutation) await notifyMutationFailure(path);
    throw new ApiError(detail, response.status);
  }
  return response.json() as Promise<T>;
}

export function isMutationRequest(init?: RequestInit): boolean {
  return !["GET", "HEAD", "OPTIONS"].includes((init?.method ?? "GET").toUpperCase());
}

export function registerMutationFailureHandler(handler: MutationFailureHandler | null): void {
  mutationFailureHandler = handler;
}

export function pinApiInstance(instanceId: string | null): void {
  pinnedInstanceId = instanceId;
}

async function notifyMutationFailure(path: string): Promise<void> {
  if (mutationFailureHandler) await mutationFailureHandler(path);
}

export function clearProjectCaches(apiBase: string): Promise<ProjectCacheMetrics> {
  return api<ProjectCacheMetrics>(`${apiBase}/caches`, { method: "DELETE" });
}

export function loadProjectReadiness(
  apiBase: string,
  refresh = false,
): Promise<
  Pick<ProjectSnapshot, "provider_readiness" | "providers" | "provider_skill_inventories">
> {
  return api(`${apiBase}/readiness${refresh ? "?refresh=true" : ""}`);
}

export interface ChatAttachmentUpload {
  attachment_set_id: string;
  attachment: ChatAttachmentDescriptor;
}

export function uploadChatAttachment(
  apiBase: string,
  chatId: string,
  file: File,
  clientId: string,
  attachmentSetId?: string | null,
): Promise<ChatAttachmentUpload> {
  const body = new FormData();
  body.append("file", file, file.name);
  body.append("client_id", clientId);
  if (attachmentSetId) body.append("attachment_set_id", attachmentSetId);
  return api<ChatAttachmentUpload>(`${apiBase}/chats/${encodeURIComponent(chatId)}/attachments`, {
    method: "POST",
    body,
  });
}

export function removeChatAttachment(
  apiBase: string,
  chatId: string,
  attachmentSetId: string,
  attachmentId: string,
  clientId: string,
): Promise<{ removed: boolean }> {
  const query = new URLSearchParams({
    attachment_set_id: attachmentSetId,
    client_id: clientId,
  });
  return api<{ removed: boolean }>(
    `${apiBase}/chats/${encodeURIComponent(chatId)}/attachments/${encodeURIComponent(attachmentId)}?${query}`,
    { method: "DELETE" },
  );
}
