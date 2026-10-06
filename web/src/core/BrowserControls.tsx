import { useEffect, useId, useState } from "react";
import { loadChatBrowser, loadProjectMachineBrowser, setChatBrowser } from "./api";
import { browserReason } from "./browserStatus";
import { errorMessage } from "./errors";
import type { BrowserTurnStatus, MachineBrowserReadiness } from "./types";

export function BrowserToggle({
  checked,
  disabled = false,
  subject = "chat",
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  /** A chat's toggle changes between turns; a run's is chosen at launch. */
  subject?: "chat" | "run";
  onChange: (checked: boolean) => void;
}) {
  const consentId = useId();
  return (
    <div className="browser-consent">
      <label className="browser-consent-switch">
        <input
          type="checkbox"
          role="switch"
          checked={checked}
          disabled={disabled}
          aria-describedby={consentId}
          onChange={(event) => onChange(event.target.checked)}
        />{" "}
        Browser
      </label>
      {subject === "chat" ? (
        <p id={consentId}>
          Runs agent-written code outside this chat's folders, from the next turn. Off deletes its
          logins.
        </p>
      ) : (
        <p id={consentId}>
          The agents in this run can use a headless browser on the machine the run uses. Browser
          actions can run agent-written code and write files outside the run's folders.
        </p>
      )}
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
  const [requested, setRequested] = useState<boolean | null>(null);
  useEffect(() => onRequestedChange?.(requested ?? false), [requested, onRequestedChange]);
  const [pending, setPending] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    void loadChatBrowser(apiBase, chatId).then(
      (value) => {
        if (!cancelled) setRequested(value.browser_requested);
      },
      (failure) => {
        if (!cancelled) setError(errorMessage(failure));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [apiBase, chatId]);
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
    setPending(true);
    setError(null);
    try {
      const result =
        value === undefined
          ? await loadChatBrowser(apiBase, chatId)
          : await setChatBrowser(apiBase, chatId, value);
      setRequested(result.browser_requested);
      setUncertain(false);
    } catch (failure) {
      setError(errorMessage(failure));
      // A disconnected write may have committed. Reconcile before enabling another change.
      if (value !== undefined) {
        try {
          setRequested((await loadChatBrowser(apiBase, chatId)).browser_requested);
        } catch {
          setUncertain(true);
        }
      }
    } finally {
      setPending(false);
    }
  }
  return (
    <div className="chat-browser-control">
      <BrowserToggle
        checked={requested ?? false}
        disabled={disabled || pending || uncertain || requested === null}
        onChange={(value) => void change(value)}
      />
      {pending && <p role="status">Saving Browser preference…</p>}
      {warning && (
        <p role="status" className="chat-browser-warning">
          {warning.reason}
          {warning.fix ? ` ${warning.fix}` : ""}
        </p>
      )}
      {error && <p role="alert">{error}</p>}
      {error && (
        <button
          type="button"
          className="button secondary compact"
          disabled={pending}
          onClick={() => void change()}
        >
          Check again
        </button>
      )}
    </div>
  );
}

export function BrowserTurnNotice({ status }: { status?: BrowserTurnStatus | null }) {
  if (!status || (status.status !== "unavailable" && status.status !== "lost")) return null;
  const reason = browserReason(status.reason_code);
  return (
    <div className="provider-login-notice" role="status" data-browser-status={status.status}>
      <strong>
        {status.status === "lost" ? "Browser connection lost" : "Browser unavailable"}
      </strong>
      <p>
        {reason.reason}
        {reason.fix ? ` ${reason.fix}` : ""}
      </p>
      {status.detail && <p>{status.detail}</p>}
    </div>
  );
}
