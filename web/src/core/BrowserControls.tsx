import { chatBrowserProfile } from "../chat/chatResources";
import { resourceKeyId } from "./resourceStore";
import { useRefreshRuntime, useResource } from "./useResource";
import { useEffect, useState } from "react";
import { loadProjectMachineBrowser, setChatBrowser } from "./api";
import { browserReason } from "./browserStatus";
import { Banner } from "../ui/Banner";
import { errorMessage } from "./errors";
import type { BrowserTurnStatus, MachineBrowserReadiness } from "./types";

/** The one Browser use switch for chats and launches; its name carries no caption. */
export function BrowserToggle({
  checked,
  disabled = false,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <div className="browser-consent">
      <label className="browser-consent-switch">
        <input
          type="checkbox"
          role="switch"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
        />{" "}
        Browser use
      </label>
    </div>
  );
}

const RECHECK_MS = 15_000;

// The caller keys this by project and chat, so a late response cannot change another chat.
export function ChatBrowserControl({
  apiBase,
  chatId,
  machine,
  disabled = false,
  onRequestedChange,
}: {
  apiBase: string;
  chatId: string;
  /** The project machine alias the next turn runs on, whose browser is checked while on. */
  machine: string;
  disabled?: boolean;
  onRequestedChange?: (requested: boolean) => void;
}) {
  const { store } = useRefreshRuntime();
  const projectId = decodeURIComponent(apiBase.split("/projects/")[1]?.split("/")[0] ?? "");
  const profile = chatBrowserProfile(projectId, apiBase, chatId);
  const preference = useResource(`${projectId}:browser:${chatId}`, profile);
  const requested = preference.value?.browser_requested ?? null;
  const pending = preference.pending.length > 0;
  const [error, setError] = useState<string | null>(null);
  useEffect(() => onRequestedChange?.(requested ?? false), [requested, onRequestedChange]);
  const [checked, setChecked] = useState<{
    machine: string;
    readiness: MachineBrowserReadiness;
  } | null>(null);
  const [recheck, setRecheck] = useState(0);
  const [failedChecks, setFailedChecks] = useState(0);
  useEffect(() => {
    if (!requested || !machine) return;
    let cancelled = false;
    // A failed check shows nothing new here; the turn itself reports an unavailable
    // browser. Counting failures keeps re-checking until one check succeeds.
    void loadProjectMachineBrowser(apiBase, machine).then(
      (readiness) => {
        if (cancelled) return;
        setChecked({ machine, readiness });
        setFailedChecks(0);
      },
      () => {
        if (!cancelled) setFailedChecks((count) => count + 1);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [apiBase, machine, requested, recheck]);
  const warning =
    requested && checked?.machine === machine && checked.readiness.status !== "ready"
      ? browserReason(checked.readiness.status)
      : null;
  // Installing from Settings finishes elsewhere; keep checking while the warning shows
  // or while the last check failed, including a first check that never succeeded.
  const warningCode = warning?.code ?? null;
  useEffect(() => {
    if (!requested || (!warningCode && failedChecks === 0)) return;
    const timer = window.setTimeout(() => setRecheck((count) => count + 1), RECHECK_MS);
    return () => window.clearTimeout(timer);
  }, [requested, warningCode, checked, failedChecks]);
  async function change(value?: boolean) {
    const id = resourceKeyId(profile.key);
    if (value === undefined) {
      store.invalidate(id);
      return;
    }
    setError(null);
    const mutation = store.beginMutation(
      crypto.randomUUID(),
      new Map([
        [
          id,
          (current: unknown) => ({
            ...(current as object),
            browser_requested: value,
          }),
        ],
      ]),
    );
    try {
      const result = await setChatBrowser(apiBase, chatId, value);
      mutation.settle(new Map([[id, result]]));
    } catch (failure) {
      mutation.fail();
      store.invalidate(id);
      setError(errorMessage(failure));
    }
  }
  return (
    <div
      className="chat-browser-control"
      style={error || preference.error ? { outline: "1px solid var(--danger, #c44)" } : undefined}
    >
      <BrowserToggle
        checked={requested ?? false}
        disabled={disabled || pending || requested === null}
        onChange={(value) => void change(value)}
      />
      {warning && (
        <span role="status" title={warning.reason} aria-label={warning.reason}>
          ⚠
        </span>
      )}
      {Boolean(error || preference.error) && (
        <button
          type="button"
          className="button icon"
          aria-label="Retry browser preference"
          title={error ?? String(preference.error)}
          disabled={pending}
          onClick={() => void change()}
        >
          ↻
        </button>
      )}
    </div>
  );
}

export function BrowserTurnNotice({
  status,
  turnId,
}: {
  status?: BrowserTurnStatus | null;
  /** The turn this status belongs to, so another turn's loss shows again. */
  turnId: string;
}) {
  if (!status || (status.status !== "unavailable" && status.status !== "lost")) return null;
  const reason = browserReason(status.reason_code);
  return (
    <Banner
      dismissKey={`browser-turn:${turnId}:${status.status}`}
      className="provider-login-notice"
      role="status"
      data-browser-status={status.status}
    >
      <strong>
        {status.status === "lost" ? "Browser connection lost" : "Browser unavailable"}
      </strong>
      <p>
        {reason.reason}
        {reason.fix ? ` ${reason.fix}` : ""}
      </p>
      {status.detail && <p>{status.detail}</p>}
    </Banner>
  );
}
