import type { MachineSettingsRecord } from "../projects/spaceMachines";
import type {
  ChatBrowserPreference,
  MachineBrowserReadiness,
  DependencyStatus,
  AgentQuestion,
  AnswerQuestionRequest,
  UpdateNotice,
  MachinePowerStatus,
  Machine,
  ExternalWatcherRecord,
  ChatAttachmentDescriptor,
  ChatDisplay,
  ChatReads,
  ChatMessage,
  SteerRequest,
  Episode,
  EpisodeMessage,
  EpisodeTimelineResponse,
  MergeEpisodeBody,
  MergePreview,
  EpisodeTimelineText,
  EpisodeMode,
  ExperimentLoopIndexEntry,
  ExperimentStartResponse,
  AgentTaskRequest,
  IdentityResponse,
  MachineDirectoryListing,
  MachineDirectoryRequest,
  AllProjectCacheClearResult,
  ProjectCacheClearResult,
  ProjectDigest,
  ProjectDigestMark,
  ProjectProvisioningCreateRequest,
  ProjectProvisioningResponse,
  ProjectSnapshot,
  ProviderLoginAccount,
  ProviderLoginState,
  ProviderSignInStatus,
  ProviderResumeSummary,
  ServerStatus,
  ServiceConnection,
  ServiceAddress,
  ServiceConnectionCreateRequest,
  ServiceConnections,
  ServiceConnectionUpdate,
  ServiceModels,
  TranscriptionResult,
  SpaceMachineCreateRequest,
  SpaceMachineUpdateRequest,
  SpaceRunIndexEntry,
  SpaceUserSummary,
  StartEpisodeRequest,
  TeamInvitation,
  TeamInvitationIssue,
  TeamDevicePairing,
  TeamDevicePairingStatus,
  TeamSession,
  VoiceSessionResponse,
  VoiceSettings,
} from "./types";

type MutationFailureHandler = (path: string) => Promise<void>;
type IdentityNameRequiredHandler = () => Promise<boolean>;
type TransportFailureHandler = () => void;
type AccessLossHandler = () => void;

let mutationFailureHandler: MutationFailureHandler | null = null;
let transportFailureHandler: TransportFailureHandler | null = null;
let identityNameRequiredHandler: IdentityNameRequiredHandler | null = null;
let accessLossHandler: AccessLossHandler | null = null;
let pinnedInstanceId: string | null = null;

export const TEAM_SHELL_PROTOCOL_HEADER = "RCP-Team-Shell-Protocol";
export const TEAM_SHELL_PROTOCOL_VERSION = 3;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | undefined;
  /** The route's structured `detail`, for callers that read its fields. */
  readonly detail: Record<string, unknown> | undefined;

  constructor(message: string, status: number, detail?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.code = typeof detail?.code === "string" ? detail.code : undefined;
  }
}

export async function api<T>(
  path: string,
  init?: RequestInit,
  options: { retryIdentity?: boolean } = {},
): Promise<T> {
  const mutation = isMutationRequest(init);
  const headers = new Headers(init?.headers);
  if (!headers.has("Content-Type") && !(init?.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (
    path === "/api/projects" ||
    path === "/api/team/session/exchange" ||
    (init?.method === "DELETE" && /^\/api\/projects\/[^/]+$/.test(path))
  ) {
    headers.set(TEAM_SHELL_PROTOCOL_HEADER, String(TEAM_SHELL_PROTOCOL_VERSION));
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
    notifyTransportFailure(error);
    if (mutation) await notifyMutationFailure(path);
    throw error;
  }
  if (!response.ok) {
    if (
      typeof window !== "undefined" &&
      response.status === 401 &&
      path !== "/api/owner/exchange" &&
      path !== "/api/owner/redeem"
    ) {
      window.dispatchEvent(new Event("rcp:session-required"));
    }
    if (!mutation && (response.status === 401 || response.status === 403)) accessLossHandler?.();
    const body = await readErrorBody(response);
    if (
      mutation &&
      options.retryIdentity !== false &&
      identityNameIsRequired(response.status, body) &&
      identityNameRequiredHandler
    ) {
      const originalError = apiError(response.status, body);
      if (!(await identityNameRequiredHandler())) throw originalError;
      try {
        response = await request();
      } catch (error) {
        notifyTransportFailure(error);
        await notifyMutationFailure(path);
        throw error;
      }
      if (response.ok) return readJson<T>(response);
      const retryBody = await readErrorBody(response);
      await notifyMutationFailure(path);
      throw apiError(response.status, retryBody);
    }
    if (mutation) await notifyMutationFailure(path);
    throw apiError(response.status, body);
  }
  return readJson<T>(response);
}

// A body can also be cut off by a dropped transport; a parse error cannot.
async function readJson<T>(response: Response): Promise<T> {
  if (response.status === 204) return undefined as T;
  try {
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof TypeError) notifyTransportFailure(error);
    throw error;
  }
}

function readErrorBody(response: Response): Promise<unknown> {
  return readJson(response).catch(() => ({ detail: response.statusText }));
}

export function isMutationRequest(init?: RequestInit): boolean {
  return !["GET", "HEAD", "OPTIONS"].includes((init?.method ?? "GET").toUpperCase());
}

export function registerMutationFailureHandler(handler: MutationFailureHandler | null): void {
  mutationFailureHandler = handler;
}

/** Called whenever a read is refused with 401 or 403: the page lost its identity or access. */
export function registerAccessLossHandler(handler: AccessLossHandler | null): void {
  accessLossHandler = handler;
}

/** Called, without waiting, whenever a request never reached the backend. */
export function registerTransportFailureHandler(handler: TransportFailureHandler | null): void {
  transportFailureHandler = handler;
}

function notifyTransportFailure(error: unknown): void {
  if (error instanceof DOMException && error.name === "AbortError") return;
  transportFailureHandler?.();
}

export function registerIdentityNameRequiredHandler(
  handler: IdentityNameRequiredHandler | null,
): void {
  identityNameRequiredHandler = handler;
}

export function pinApiInstance(instanceId: string | null): void {
  pinnedInstanceId = instanceId;
}

export function exchangeTeamSession(token: string): Promise<IdentityResponse> {
  return api<IdentityResponse>("/api/team/session/exchange", {
    method: "POST",
    body: JSON.stringify({ token }),
  });
}

export function loadTeamInvitations(): Promise<TeamInvitation[]> {
  return api<TeamInvitation[]>("/api/team/invitations");
}

export function loadTeamSessions(): Promise<TeamSession[]> {
  return api<TeamSession[]>("/api/team/sessions");
}

export function createTeamDevicePairing(): Promise<TeamDevicePairing> {
  return api<TeamDevicePairing>("/api/team/devices/pairings", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function loadTeamDevicePairing(pairingId: string): Promise<TeamDevicePairingStatus> {
  return api<TeamDevicePairingStatus>(
    `/api/team/devices/pairings/${encodeURIComponent(pairingId)}`,
  );
}

export function pairTeamDevice(code: string, label: string): Promise<IdentityResponse> {
  return api<IdentityResponse>("/api/team/devices/pair", {
    method: "POST",
    body: JSON.stringify({ code, label }),
  });
}

export function revokeTeamSession(sessionId: string): Promise<{ ok: boolean }> {
  return api<{ ok: boolean }>(`/api/team/sessions/${encodeURIComponent(sessionId)}/revoke`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function loadSpaceUsers(): Promise<SpaceUserSummary[]> {
  return api<SpaceUserSummary[]>("/api/space/users");
}

export function createTeamInvitation(): Promise<TeamInvitationIssue> {
  return api<TeamInvitationIssue>("/api/team/invitations", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function revokeTeamInvitation(invitationId: string): Promise<TeamInvitation> {
  return api<TeamInvitation>(`/api/team/invitations/${encodeURIComponent(invitationId)}/revoke`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function createTeamProjectProvisioning(
  body: ProjectProvisioningCreateRequest,
): Promise<ProjectProvisioningResponse> {
  return api<ProjectProvisioningResponse>("/api/project-provisioning/requests", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function loadUpdateNotice(): Promise<UpdateNotice> {
  return api<UpdateNotice>("/api/update-notice");
}

/** `refresh` asks the server to look up the latest release again first. */
export function loadServerStatus(refresh = false): Promise<ServerStatus> {
  return api<ServerStatus>(refresh ? "/api/server-status?refresh=true" : "/api/server-status");
}

export function loadProjectProvisioningRequests(): Promise<ProjectProvisioningResponse[]> {
  return api<ProjectProvisioningResponse[]>("/api/project-provisioning/requests");
}

export function loadProjectProvisioningRequest(
  requestId: string,
): Promise<ProjectProvisioningResponse> {
  return api<ProjectProvisioningResponse>(
    `/api/project-provisioning/requests/${encodeURIComponent(requestId)}`,
  );
}

export function cancelProjectProvisioningRequest(
  requestId: string,
): Promise<ProjectProvisioningResponse> {
  return api<ProjectProvisioningResponse>(
    `/api/project-provisioning/requests/${encodeURIComponent(requestId)}/cancel`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function completeProjectProvisioningRequest(
  requestId: string,
  finalReviewDigest: string,
): Promise<ProjectProvisioningResponse> {
  return api<ProjectProvisioningResponse>(
    `/api/project-provisioning/requests/${encodeURIComponent(requestId)}/complete`,
    {
      method: "POST",
      body: JSON.stringify({ final_review_digest: finalReviewDigest }),
    },
  );
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
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const { message } = detail as { message?: unknown };
    return new ApiError(
      typeof message === "string" ? message : JSON.stringify(detail),
      status,
      detail as Record<string, unknown>,
    );
  }
  if (Array.isArray(detail) && typeof detail[0]?.msg === "string") {
    return new ApiError(detail[0].msg, status);
  }
  return new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail), status);
}

export function clearProjectCaches(apiBase: string): Promise<ProjectCacheClearResult> {
  return api<ProjectCacheClearResult>(`${apiBase}/caches`, { method: "DELETE" });
}

export function clearAllProjectCaches(projectId: string): Promise<AllProjectCacheClearResult> {
  return api<AllProjectCacheClearResult>(
    `/api/projects/${encodeURIComponent(projectId)}/caches/all`,
    {
      method: "DELETE",
    },
  );
}

export async function loadSpaceMachines(): Promise<MachineSettingsRecord[]> {
  return (await api<{ machines: MachineSettingsRecord[] }>("/api/space/machines")).machines;
}

export function createSpaceMachine(
  request: SpaceMachineCreateRequest,
): Promise<MachineSettingsRecord> {
  return api<MachineSettingsRecord>("/api/space/machines", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function updateSpaceMachine(
  machineId: string,
  request: SpaceMachineUpdateRequest,
): Promise<MachineSettingsRecord> {
  return api<MachineSettingsRecord>(`/api/space/machines/${encodeURIComponent(machineId)}`, {
    method: "PATCH",
    body: JSON.stringify(request),
  });
}

export function deleteSpaceMachine(machineId: string): Promise<unknown> {
  return api<unknown>(`/api/space/machines/${encodeURIComponent(machineId)}`, {
    method: "DELETE",
  });
}

export function listMachineDirectory(
  machineId: string,
  request: MachineDirectoryRequest,
  signal?: AbortSignal,
): Promise<MachineDirectoryListing> {
  return api<MachineDirectoryListing>(
    `/api/space/machines/${encodeURIComponent(machineId)}/directories`,
    { method: "POST", body: JSON.stringify(request), signal },
  );
}

/** Appends a space machine to a project's manifest under a project alias. */
export function addProjectMachine(
  projectId: string,
  machineId: string,
  alias: string,
): Promise<ProjectSnapshot> {
  return api<ProjectSnapshot>(`/api/projects/${encodeURIComponent(projectId)}/machines`, {
    method: "POST",
    body: JSON.stringify({ machine_id: machineId, alias }),
  });
}

export function loadProjectReadiness(
  apiBase: string,
  refresh = false,
): Promise<
  Pick<
    ProjectSnapshot,
    | "compute_status"
    | "provider_logins"
    | "provider_readiness"
    | "providers"
    | "provider_skill_inventories"
    | "agent_profiles"
  >
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

export function loadEpisodes(
  apiBase: string,
  mode?: EpisodeMode,
  episodeId?: string,
): Promise<Episode[]> {
  const query = new URLSearchParams();
  if (mode) query.set("mode", mode);
  if (episodeId) query.set("episode_id", episodeId);
  const suffix = query.size ? `?${query}` : "";
  return api<Episode[]>(`${apiBase}/episodes${suffix}`);
}

/** Start on the supplied graph route and retain the server's overlap receipt. */
export function startExperimentRun(
  path: string,
  request: AgentTaskRequest,
): Promise<ExperimentStartResponse> {
  return api<ExperimentStartResponse>(path, {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function loadExperimentEpisodes(): Promise<ExperimentLoopIndexEntry[]> {
  return api<ExperimentLoopIndexEntry[]>("/api/episodes?mode=experiment_loop");
}

export function loadProjectExperimentEpisodes(
  projectId: string,
): Promise<ExperimentLoopIndexEntry[]> {
  return api<ExperimentLoopIndexEntry[]>(
    `/api/projects/${encodeURIComponent(projectId)}/experiment-episodes?mode=experiment_loop`,
  );
}

export function loadSpaceRuns(): Promise<SpaceRunIndexEntry[]> {
  return api<SpaceRunIndexEntry[]>("/api/space/runs");
}

export function startEpisode(apiBase: string, request: StartEpisodeRequest): Promise<Episode> {
  return api<Episode>(`${apiBase}/episodes`, {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function stopEpisode(apiBase: string, episodeId: string): Promise<Episode> {
  return api<Episode>(`${apiBase}/episodes/${encodeURIComponent(episodeId)}/stop`, {
    method: "POST",
  });
}

export function archiveEpisode(
  apiBase: string,
  episodeId: string,
  archived: boolean,
): Promise<Episode> {
  return api<Episode>(`${apiBase}/episodes/${encodeURIComponent(episodeId)}/archive`, {
    method: "POST",
    body: JSON.stringify({ archived }),
  });
}

/** Add turns to an ended episode; the request id makes a retried click return the same continuation. */
export function continueEpisode(
  apiBase: string,
  episodeId: string,
  invocationCeiling: number,
  requestId: string,
): Promise<Episode> {
  return api<Episode>(`${apiBase}/episodes/${encodeURIComponent(episodeId)}/continue`, {
    method: "POST",
    body: JSON.stringify({ invocation_ceiling: invocationCeiling, request_id: requestId }),
  });
}

export function mergeEpisodeToMain(
  apiBase: string,
  episodeId: string,
  body: MergeEpisodeBody = {},
): Promise<Episode> {
  return api<Episode>(`${apiBase}/episodes/${encodeURIComponent(episodeId)}/merge`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function loadMergePreview(
  apiBase: string,
  episodeId: string,
  targetBranch: string | null,
): Promise<MergePreview> {
  const query = targetBranch ? `?target_branch=${encodeURIComponent(targetBranch)}` : "";
  return api<MergePreview>(
    `${apiBase}/episodes/${encodeURIComponent(episodeId)}/merge-preview${query}`,
  );
}

export function fetchEpisodeTimeline(
  apiBase: string,
  episodeId: string,
): Promise<EpisodeTimelineResponse> {
  return api<EpisodeTimelineResponse>(
    `${apiBase}/episodes/${encodeURIComponent(episodeId)}/timeline`,
  );
}

export function fetchTimelineText(
  apiBase: string,
  episodeId: string,
  textRef: string,
): Promise<EpisodeTimelineText> {
  return api<EpisodeTimelineText>(
    `${apiBase}/episodes/${encodeURIComponent(episodeId)}/timeline/text/${encodeURIComponent(textRef)}`,
  );
}

export function loadEpisodeMessages(apiBase: string, episodeId: string): Promise<EpisodeMessage[]> {
  return api<EpisodeMessage[]>(`${apiBase}/episodes/${encodeURIComponent(episodeId)}/messages`);
}

export function sendEpisodeMessage(
  apiBase: string,
  episodeId: string,
  body: string,
): Promise<EpisodeMessage> {
  return api<EpisodeMessage>(`${apiBase}/episodes/${encodeURIComponent(episodeId)}/messages`, {
    method: "POST",
    body: JSON.stringify({ body }),
  });
}

export function steerChatTurn(
  projectId: string,
  operationId: string,
  request: SteerRequest,
): Promise<ChatMessage> {
  return api<ChatMessage>(
    `/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(operationId)}/steer`,
    { method: "POST", body: JSON.stringify(request) },
    { retryIdentity: false },
  );
}

export function checkMachineCompute(
  apiBase: string,
  alias: string,
): Promise<Machine["compute_probes"]> {
  return api(`${apiBase}/machines/${encodeURIComponent(alias)}/compute/check`, {
    method: "POST",
  });
}

export function loadChatDisplay(apiBase: string): Promise<ChatDisplay> {
  return api(`${apiBase}/chat-display`);
}

export function setChatArchived(
  apiBase: string,
  chatId: string,
  archived: boolean,
): Promise<ChatDisplay> {
  return api(`${apiBase}/chats/${encodeURIComponent(chatId)}/archive`, {
    method: "POST",
    body: JSON.stringify({ archived }),
  });
}

/** `path` already names the graph target, as the chat list's does. */
export function loadChatReads(path: string): Promise<ChatReads> {
  return api(path);
}

/** `readThrough` is a turn's server-reported finish time; the marker never moves back. */
export function markChatRead(path: string, readThrough: string): Promise<ChatReads> {
  return api(path, { method: "POST", body: JSON.stringify({ read_through: readThrough }) });
}

export function loadProjectDigest(projectId: string): Promise<ProjectDigest> {
  return api(`/api/projects/${encodeURIComponent(projectId)}/digest`);
}

/** `seq` is the cursor of the digest on screen, never "now"; the mark never moves back. */
export function markDigestCaughtUp(
  projectId: string,
  seq: number,
): Promise<{ mark: ProjectDigestMark }> {
  return api(`/api/projects/${encodeURIComponent(projectId)}/digest/caught-up`, {
    method: "POST",
    body: JSON.stringify({ seq }),
  });
}

export function setChatPinned(
  apiBase: string,
  chatId: string,
  pinned: boolean,
): Promise<ChatDisplay> {
  return api(`${apiBase}/chats/${encodeURIComponent(chatId)}/pin`, {
    method: "POST",
    body: JSON.stringify({ pinned }),
  });
}

/** A blank title returns the chat to its derived name. */
export function setChatTitle(apiBase: string, chatId: string, title: string): Promise<ChatDisplay> {
  return api(`${apiBase}/chats/${encodeURIComponent(chatId)}/title`, {
    method: "POST",
    body: JSON.stringify({ title }),
  });
}

export function cancelWatcher(apiBase: string, watcherId: string): Promise<ExternalWatcherRecord> {
  return api(`${apiBase}/watchers/${encodeURIComponent(watcherId)}/cancel`, { method: "POST" });
}

export function loadProviderLogins(): Promise<ProviderLoginAccount[]> {
  return api("/api/providers/logins");
}

export function startProviderSignIn(provider: string, host: string): Promise<ProviderSignInStatus> {
  return api(`/api/providers/${encodeURIComponent(provider)}/logins/sign-in`, {
    method: "POST",
    body: JSON.stringify({ host }),
  });
}

export function providerSignInStatus(
  provider: string,
  loginId: string,
): Promise<ProviderSignInStatus> {
  return api(
    `/api/providers/${encodeURIComponent(provider)}/logins/sign-in/${encodeURIComponent(loginId)}`,
  );
}

export function cancelProviderSignIn(
  provider: string,
  loginId: string,
): Promise<ProviderSignInStatus> {
  return api(
    `/api/providers/${encodeURIComponent(provider)}/logins/sign-in/${encodeURIComponent(
      loginId,
    )}/cancel`,
    { method: "POST" },
  );
}

export function saveProviderToken(
  provider: string,
  host: string,
  token: string,
): Promise<{ state: ProviderLoginState; resumed: ProviderResumeSummary }> {
  return api(`/api/providers/${encodeURIComponent(provider)}/logins/token`, {
    method: "POST",
    body: JSON.stringify({ host, token }),
  });
}

export function signOutProvider(
  provider: string,
  host: string,
): Promise<{ state: ProviderLoginState }> {
  return api(`/api/providers/${encodeURIComponent(provider)}/logins/sign-out`, {
    method: "POST",
    body: JSON.stringify({ host }),
  });
}

export function verifyProviderLogin(
  provider: string,
  host: string,
): Promise<{
  state: ProviderLoginState;
  resumed: ProviderResumeSummary;
}> {
  return api(`/api/providers/${encodeURIComponent(provider)}/logins/verify`, {
    method: "POST",
    body: JSON.stringify({ host }),
  });
}

export function loadServiceConnections(): Promise<ServiceConnections> {
  return api("/api/service-connections");
}

export function connectServiceConnection(
  request: ServiceConnectionCreateRequest,
): Promise<ServiceConnection> {
  return api("/api/service-connections", { method: "POST", body: JSON.stringify(request) });
}

export function disconnectServiceConnection(connectionId: string): Promise<void> {
  return api(`/api/service-connections/${encodeURIComponent(connectionId)}`, {
    method: "DELETE",
  });
}

export function selectDictationService(dictation: string): Promise<unknown> {
  return api("/api/service-connections/selection", {
    method: "PUT",
    body: JSON.stringify({ dictation }),
  });
}

/** Change uses and models; RCP checks each changed one with the stored key. */
export function updateServiceConnection(
  connectionId: string,
  update: ServiceConnectionUpdate,
): Promise<ServiceConnection> {
  return api(`/api/service-connections/${encodeURIComponent(connectionId)}`, {
    method: "PUT",
    body: JSON.stringify(update),
  });
}

/** The provider's models for a key not yet saved. */
export function loadServiceModels(address: ServiceAddress): Promise<ServiceModels> {
  return api("/api/service-connections/models", {
    method: "POST",
    body: JSON.stringify(address),
  });
}

/** The provider's models, asked with a saved connection's key. */
export function loadConnectionModels(connectionId: string): Promise<ServiceModels> {
  return api(`/api/service-connections/${encodeURIComponent(connectionId)}/models`);
}

export function loadVoiceSettings(): Promise<VoiceSettings> {
  return api("/api/voice/settings");
}

/** Only the confirm toggle; voice models change through the checked connection update. */
export function saveVoiceSettings(
  settings: Pick<VoiceSettings, "confirm">,
): Promise<VoiceSettings> {
  return api("/api/voice/settings", { method: "PUT", body: JSON.stringify(settings) });
}

/** Exchange the page's WebRTC offer; the backend holds the key and keeps no session. */
export function createVoiceSession(
  body: { sdp_offer: string; tools: unknown[] },
  signal?: AbortSignal,
): Promise<VoiceSessionResponse> {
  return api("/api/voice/sessions", { method: "POST", body: JSON.stringify(body), signal });
}

/** Upload one recorded segment as raw audio; the chosen MIME type is the request's type. */
export function transcribeAudio(
  connectionId: string,
  audio: Blob,
  mimeType: string,
): Promise<TranscriptionResult> {
  return api(`/api/service-connections/${encodeURIComponent(connectionId)}/transcribe`, {
    method: "POST",
    headers: { "Content-Type": mimeType },
    body: audio,
  });
}

export function loadMachinePower(): Promise<MachinePowerStatus> {
  return api("/api/machine-power");
}

export function updateMachinePower(body: {
  idle_hold?: boolean;
  lid_mode?: boolean;
}): Promise<MachinePowerStatus> {
  return api("/api/machine-power", { method: "PUT", body: JSON.stringify(body) });
}

export function installMachinePower(): Promise<MachinePowerStatus> {
  return api("/api/machine-power/install", { method: "POST" });
}

export function uninstallMachinePower(): Promise<MachinePowerStatus> {
  return api("/api/machine-power/uninstall", { method: "POST" });
}

export function fetchQuestions(apiBase: string, ownerKind: "chat" | "episode", ownerId: string) {
  return api<AgentQuestion[]>(
    `${apiBase}/${ownerKind === "chat" ? "chats" : "episodes"}/${encodeURIComponent(ownerId)}/questions`,
  );
}

export function answerQuestion(apiBase: string, questionId: string, body: AnswerQuestionRequest) {
  return api<AgentQuestion>(`${apiBase}/questions/${encodeURIComponent(questionId)}/answer`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function dismissQuestion(apiBase: string, questionId: string) {
  return api<AgentQuestion>(`${apiBase}/questions/${encodeURIComponent(questionId)}/dismiss`, {
    method: "POST",
    body: "{}",
  });
}

export function loadChatBrowser(apiBase: string, chatId: string): Promise<ChatBrowserPreference> {
  return api(`${apiBase}/chats/${encodeURIComponent(chatId)}/browser`);
}

export function loadProjectMachineBrowser(
  apiBase: string,
  machineAlias: string,
): Promise<MachineBrowserReadiness> {
  return api(`${apiBase}/machines/${encodeURIComponent(machineAlias)}/browser`);
}

export function setChatBrowser(
  apiBase: string,
  chatId: string,
  browserRequested: boolean,
): Promise<ChatBrowserPreference> {
  return api(`${apiBase}/chats/${encodeURIComponent(chatId)}/browser`, {
    method: "PUT",
    body: JSON.stringify({ browser_requested: browserRequested }),
  });
}

export function loadMachineBrowser(machineId: string): Promise<MachineBrowserReadiness> {
  return api(`/api/space/machines/${encodeURIComponent(machineId)}/browser`);
}

export function loadMachineDependencies(
  machineId: string,
  refresh = false,
): Promise<DependencyStatus> {
  const query = refresh ? "?refresh=1" : "";
  return api(`/api/space/machines/${encodeURIComponent(machineId)}/dependencies${query}`);
}

export function installMachineBrowser(machineId: string): Promise<MachineBrowserReadiness> {
  return api(`/api/space/machines/${encodeURIComponent(machineId)}/browser/install`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function enableMachineLinger(machineId: string): Promise<MachineBrowserReadiness> {
  return api(`/api/space/machines/${encodeURIComponent(machineId)}/linger`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}
