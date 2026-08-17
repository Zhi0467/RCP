import type { Health } from "./types";

export const BACKEND_IDENTITY_EVENT = "rcp:backend-identity";
export const DESKTOP_FOLDER_ACCESS_ACK_KEY = "rcp:desktop-folder-access-acknowledgement";
export const DESKTOP_FOLDER_ACCESS_ACK_VERSION = 1;

export interface DesktopStatus {
  desktop: boolean;
  version: string;
  base_url: string;
  instance_id: string;
  data_dir_id: string;
  owner_kind: string;
  active_agent_tasks: number;
  owned: boolean;
}

export interface DesktopUpdate {
  enabled: boolean;
  available: boolean;
  version?: string;
  current_version?: string;
  reason?: string;
  active_agent_tasks: number;
}

export interface BackendIdentityResult {
  ok: boolean;
  health: Health | null;
  message: string | null;
}

export interface BackendIdentityEventDetail extends BackendIdentityResult {
  reason: string;
}

export interface ArtifactCommand {
  projectId: string;
  taskId: string;
  artifactId: string;
}

export interface EpisodeReportCommand {
  projectId: string;
  episodeId: string;
}

export interface RepositoryFileCommand {
  projectId: string;
  path: string;
  line: number | null;
}

export interface DictationResultEvent {
  session_id: string;
  text: string;
  is_final: boolean;
}

export interface DictationStateEvent {
  session_id: string;
  state: "recording" | "stopped" | "error";
  error?: string | null;
}

interface BackendIdentity {
  version: string;
  instance_id: string;
  data_dir_id: string;
}

let expectedIdentity: BackendIdentity | null = null;
let identityCheck: Promise<BackendIdentityResult> | null = null;
let identityCheckAcceptsCurrent = false;

export function isDesktopRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function identityMismatch(
  expected: BackendIdentity,
  observed: BackendIdentity,
): string | null {
  const changed: string[] = [];
  if (expected.version !== observed.version) {
    changed.push(`version ${expected.version} became ${observed.version}`);
  }
  if (expected.instance_id !== observed.instance_id) {
    changed.push(`instance ${expected.instance_id} became ${observed.instance_id}`);
  }
  if (expected.data_dir_id !== observed.data_dir_id) {
    changed.push(`data directory ${expected.data_dir_id} became ${observed.data_dir_id}`);
  }
  return changed.length ? `RCP's backend changed: ${changed.join("; ")}.` : null;
}

export async function establishBackendIdentity(): Promise<BackendIdentityResult> {
  return runIdentityCheck("startup", true);
}

export async function acceptCurrentBackendIdentity(): Promise<BackendIdentityResult> {
  return runIdentityCheck("reconnect", true);
}

export async function reverifyBackendIdentity(reason: string): Promise<BackendIdentityResult> {
  return runIdentityCheck(reason, false);
}

export async function verifyIdentityAfterMutationFailure(path: string): Promise<void> {
  if (path === "/api/health") return;
  await reverifyBackendIdentity("mutation-failure");
}

export async function desktopStatus(): Promise<DesktopStatus | null> {
  if (!isDesktopRuntime()) return null;
  return invokeDesktop<DesktopStatus>("desktop_status");
}

export async function desktopReconnectBackend(): Promise<DesktopStatus> {
  if (!isDesktopRuntime())
    throw new Error("Desktop backend recovery is unavailable in this browser.");
  return invokeDesktop<DesktopStatus>("desktop_reconnect_backend");
}

export function backendReconnectLabel(desktop: boolean): string {
  return desktop ? "Start or reconnect" : "Reconnect";
}

export function needsDesktopFolderAccessAcknowledgement(
  desktop: boolean,
  storedValue: string | null,
): boolean {
  if (!desktop) return false;
  try {
    const parsed = storedValue ? (JSON.parse(storedValue) as { version?: unknown }) : null;
    return parsed?.version !== DESKTOP_FOLDER_ACCESS_ACK_VERSION;
  } catch {
    return true;
  }
}

export function desktopFolderAccessAcknowledgementValue(): string {
  return JSON.stringify({ version: DESKTOP_FOLDER_ACCESS_ACK_VERSION });
}

export async function desktopShowReady(): Promise<void> {
  if (!isDesktopRuntime()) return;
  await invokeDesktop("desktop_show_ready");
}

export async function setDesktopWebviewZoom(scale: number): Promise<void> {
  if (!isDesktopRuntime()) return;
  const { getCurrentWebview } = await import("@tauri-apps/api/webview");
  await getCurrentWebview().setZoom(scale);
}

export async function openDesktopArtifactPreview(command: ArtifactCommand): Promise<void> {
  if (!isDesktopRuntime())
    throw new Error("Desktop artifact preview is unavailable in this browser.");
  const result = await invokeDesktop<{ opened: boolean; error?: string }>(
    "open_artifact_preview",
    command,
  );
  if (!result.opened)
    throw new Error(result.error || "The desktop host could not open this artifact.");
}

export async function openDesktopEpisodeReportPreview(
  command: EpisodeReportCommand,
): Promise<void> {
  if (!isDesktopRuntime())
    throw new Error("Desktop episode report preview is unavailable in this browser.");
  const result = await invokeDesktop<{ opened: boolean; error?: string }>(
    "open_episode_report_preview",
    command,
  );
  if (!result.opened)
    throw new Error(result.error || "The desktop host could not open this episode report.");
}

/**
 * Claim a report link only in the desktop shell. In an ordinary browser the
 * caller's target=_blank link remains entirely native browser behavior.
 */
export async function openEpisodeReportFromLink(
  event: Pick<Event, "preventDefault">,
  command: EpisodeReportCommand,
): Promise<boolean> {
  if (!isDesktopRuntime()) return false;
  event.preventDefault();
  await openDesktopEpisodeReportPreview(command);
  return true;
}

export async function openDesktopRepositoryFilePreview(
  command: RepositoryFileCommand,
): Promise<void> {
  if (!isDesktopRuntime())
    throw new Error("Desktop repository file preview is unavailable in this browser.");
  const result = await invokeDesktop<{ opened: boolean; error?: string }>(
    "open_repository_file_preview",
    command,
  );
  if (!result.opened)
    throw new Error(result.error || "The desktop host could not open this repository file.");
}

export async function downloadDesktopArtifact(
  command: ArtifactCommand & { suggestedName: string },
): Promise<string | null> {
  if (!isDesktopRuntime())
    throw new Error("Desktop artifact download is unavailable in this browser.");
  const result = await invokeDesktop<{ saved: boolean; path?: string | null; error?: string }>(
    "download_artifact",
    command,
  );
  return desktopDownloadPath(result);
}

export async function startDesktopDictation(sessionId: string): Promise<void> {
  if (!isDesktopRuntime()) throw new Error("Dictation is available in the desktop app.");
  await invokeDesktop("desktop_start_dictation", { sessionId });
}

export async function stopDesktopDictation(sessionId: string): Promise<void> {
  if (!isDesktopRuntime()) return;
  await invokeDesktop("desktop_stop_dictation", { sessionId });
}

export function desktopDownloadPath(result: {
  saved: boolean;
  path?: string | null;
  error?: string;
}): string | null {
  if (result.saved) return result.path ?? null;
  if (result.path === null && !result.error) return null;
  throw new Error(result.error || "The desktop host could not save this artifact.");
}

export async function requestDesktopQuit(): Promise<void> {
  if (!isDesktopRuntime()) return;
  await invokeDesktop("request_quit");
}

export async function checkDesktopUpdate(): Promise<DesktopUpdate | null> {
  if (!isDesktopRuntime()) return null;
  return invokeDesktop<DesktopUpdate>("check_for_update");
}

export async function applyDesktopUpdate(confirmActiveWork: boolean): Promise<void> {
  if (!isDesktopRuntime()) return;
  const result = await invokeDesktop<{ started: boolean; error?: string }>("apply_update", {
    confirmActiveWork,
  });
  if (!result.started) throw new Error(result.error || "The desktop update could not be started.");
}

export async function listenDesktopEvent<T>(
  name:
    | "rcp://prepare-show"
    | "rcp://backend-mismatch"
    | "rcp://update-ready"
    | "rcp://dictation-result"
    | "rcp://dictation-state",
  handler: (payload: T) => void | Promise<void>,
): Promise<() => void> {
  if (!isDesktopRuntime()) return () => undefined;
  const { listen } = await import("@tauri-apps/api/event");
  return listen<T>(name, (event) => void handler(event.payload));
}

async function runIdentityCheck(
  reason: string,
  replaceExpected: boolean,
): Promise<BackendIdentityResult> {
  if (identityCheck) {
    if (!replaceExpected || identityCheckAcceptsCurrent) return identityCheck;
    await identityCheck;
    return runIdentityCheck(reason, true);
  }
  identityCheckAcceptsCurrent = replaceExpected;
  identityCheck = checkBackendIdentity(replaceExpected)
    .then((result) => {
      dispatchIdentityResult({ ...result, reason });
      return result;
    })
    .finally(() => {
      identityCheck = null;
      identityCheckAcceptsCurrent = false;
    });
  return identityCheck;
}

async function checkBackendIdentity(replaceExpected: boolean): Promise<BackendIdentityResult> {
  let health: Health;
  try {
    health = await fetchHealth();
  } catch (error) {
    return {
      ok: false,
      health: null,
      message: `RCP could not verify its backend: ${error instanceof Error ? error.message : String(error)}`,
    };
  }

  const observed = toIdentity(health);
  let desktopHostVerified = false;
  if (isDesktopRuntime()) {
    let status: DesktopStatus;
    try {
      status = await invokeDesktop<DesktopStatus>("desktop_status");
    } catch (error) {
      return {
        ok: false,
        health,
        message: `RCP could not verify its desktop host: ${error instanceof Error ? error.message : String(error)}`,
      };
    }
    const shellMismatch = identityMismatch(toIdentity(status), observed);
    if (shellMismatch) return { ok: false, health, message: shellMismatch };
    desktopHostVerified = true;
  }

  // The frontend can load before the Rust shell has finished connecting. Its
  // first explicit startup check then fails at desktop_status, and the later
  // prepare-show event is a verification rather than an acceptance request.
  // When no identity has ever been accepted, an exact shell/health match is the
  // bootstrap authority; once one exists, only an explicit reconnect replaces it.
  if (replaceExpected || (!expectedIdentity && desktopHostVerified)) {
    expectedIdentity = observed;
  }
  if (!expectedIdentity) {
    return { ok: false, health, message: "RCP has not accepted this backend identity." };
  }
  const mismatch = identityMismatch(expectedIdentity, observed);
  return mismatch ? { ok: false, health, message: mismatch } : { ok: true, health, message: null };
}

async function fetchHealth(): Promise<Health> {
  const response = await fetch("/api/health", { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`health check returned HTTP ${response.status}`);
  return response.json() as Promise<Health>;
}

function toIdentity(value: BackendIdentity): BackendIdentity {
  return {
    version: value.version,
    instance_id: value.instance_id,
    data_dir_id: value.data_dir_id,
  };
}

function dispatchIdentityResult(detail: BackendIdentityEventDetail): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent<BackendIdentityEventDetail>(BACKEND_IDENTITY_EVENT, { detail }),
  );
}

async function invokeDesktop<T>(command: string, args?: object): Promise<T> {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(command, args as Record<string, unknown> | undefined);
}
