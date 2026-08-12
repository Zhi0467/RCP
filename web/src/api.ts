import type {
  Campaign,
  CampaignMessage,
  ChatAttachmentDescriptor,
  ProjectCacheMetrics,
  ProjectSnapshot,
  ResultViewDescriptor,
  StartCampaignRequest,
} from "./types";

type MutationFailureHandler = (path: string) => Promise<void>;
type IdentityNameRequiredHandler = () => Promise<boolean>;

let mutationFailureHandler: MutationFailureHandler | null = null;
let identityNameRequiredHandler: IdentityNameRequiredHandler | null = null;
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
  const request = () =>
    fetch(path, {
      ...init,
      headers,
    });
  let response: Response;
  try {
    response = await request();
  } catch (error) {
    if (mutation) await notifyMutationFailure(path);
    throw error;
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    if (mutation && identityNameIsRequired(response.status, body) && identityNameRequiredHandler) {
      const originalError = apiError(response.status, body);
      if (!(await identityNameRequiredHandler())) throw originalError;
      try {
        response = await request();
      } catch (error) {
        await notifyMutationFailure(path);
        throw error;
      }
      if (response.ok) return response.json() as Promise<T>;
      const retryBody = await response.json().catch(() => ({ detail: response.statusText }));
      await notifyMutationFailure(path);
      throw apiError(response.status, retryBody);
    }
    if (mutation) await notifyMutationFailure(path);
    throw apiError(response.status, body);
  }
  return response.json() as Promise<T>;
}

export function isMutationRequest(init?: RequestInit): boolean {
  return !["GET", "HEAD", "OPTIONS"].includes((init?.method ?? "GET").toUpperCase());
}

export function registerMutationFailureHandler(handler: MutationFailureHandler | null): void {
  mutationFailureHandler = handler;
}

export function registerIdentityNameRequiredHandler(
  handler: IdentityNameRequiredHandler | null,
): void {
  identityNameRequiredHandler = handler;
}

export function pinApiInstance(instanceId: string | null): void {
  pinnedInstanceId = instanceId;
}

async function notifyMutationFailure(path: string): Promise<void> {
  if (mutationFailureHandler) await mutationFailureHandler(path);
}

function identityNameIsRequired(status: number, body: unknown): boolean {
  if (status !== 428 || !body || typeof body !== "object") return false;
  const detail = (body as { detail?: unknown }).detail;
  return (
    Boolean(detail) &&
    typeof detail === "object" &&
    (detail as { code?: unknown }).code === "identity_name_required"
  );
}

function apiError(status: number, body: unknown): ApiError {
  const detail =
    body && typeof body === "object" && "detail" in body
      ? (body as { detail: unknown }).detail
      : undefined;
  return new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail), status);
}

export function clearProjectCaches(apiBase: string): Promise<ProjectCacheMetrics> {
  return api<ProjectCacheMetrics>(`${apiBase}/caches`, { method: "DELETE" });
}

export function clearAllProjectCaches(projectId: string): Promise<ProjectCacheMetrics> {
  return api<ProjectCacheMetrics>(`/api/caches?project_id=${encodeURIComponent(projectId)}`, {
    method: "DELETE",
  });
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

export function loadResultViews(
  apiBase: string,
  experimentId: string,
  chatId: string,
): Promise<ResultViewDescriptor[]> {
  const query = new URLSearchParams({ experiment_id: experimentId, chat_id: chatId });
  return api<ResultViewDescriptor[]>(`${apiBase}/result-views?${query}`);
}

export function keepResultView(apiBase: string, viewId: string): Promise<ResultViewDescriptor> {
  return api<ResultViewDescriptor>(`${apiBase}/result-views/${encodeURIComponent(viewId)}/keep`, {
    method: "POST",
  });
}

export function resultViewPreviewUrl(
  projectId: string,
  view: Pick<ResultViewDescriptor, "view_id" | "updated_at">,
): string {
  const version = new URLSearchParams({ updated_at: view.updated_at });
  return `/api/projects/${encodeURIComponent(projectId)}/result-views/${encodeURIComponent(view.view_id)}/preview?${version}`;
}

export function loadCampaigns(apiBase: string): Promise<Campaign[]> {
  return api<Campaign[]>(`${apiBase}/campaigns`);
}

export function startCampaign(apiBase: string, request: StartCampaignRequest): Promise<Campaign> {
  return api<Campaign>(`${apiBase}/campaigns`, {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function stopCampaign(apiBase: string, campaignId: string): Promise<Campaign> {
  return api<Campaign>(`${apiBase}/campaigns/${encodeURIComponent(campaignId)}/stop`, {
    method: "POST",
  });
}

export function reauthorizeCampaign(
  apiBase: string,
  campaignId: string,
  additionalInvocations: number,
): Promise<Campaign> {
  return api<Campaign>(`${apiBase}/campaigns/${encodeURIComponent(campaignId)}/reauthorize`, {
    method: "POST",
    body: JSON.stringify({ additional_invocations: additionalInvocations }),
  });
}

export function loadCampaignMessages(
  apiBase: string,
  campaignId: string,
): Promise<CampaignMessage[]> {
  return api<CampaignMessage[]>(`${apiBase}/campaigns/${encodeURIComponent(campaignId)}/messages`);
}

export function sendCampaignMessage(
  apiBase: string,
  campaignId: string,
  body: string,
): Promise<CampaignMessage> {
  return api<CampaignMessage>(`${apiBase}/campaigns/${encodeURIComponent(campaignId)}/messages`, {
    method: "POST",
    body: JSON.stringify({ body }),
  });
}
