import { useEffect, useId, useState } from "react";
import { loadChatBrowser, setChatBrowser } from "../api";
import { browserReason } from "../browserStatus";
import { errorMessage } from "../errors";
import type { BrowserTurnStatus } from "../types";

export function BrowserToggle({
  checked,
  disabled = false,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}) {
  const consentId = useId();
  return (
    <div className="browser-consent">
      <label>
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
      <p id={consentId}>
        The agent can use a headless browser on the machine this chat runs on. Browser actions can
        run agent-written code and write files outside this chat's folders. Changes apply from the
        next turn. Turning it off deletes this chat's browser logins and cookies.
      </p>
    </div>
  );
}

// The caller keys this by project and chat, so a late response cannot change another chat.
export function ChatBrowserControl({
  apiBase,
  chatId,
  disabled = false,
}: {
  apiBase: string;
  chatId: string;
  disabled?: boolean;
}) {
  const [requested, setRequested] = useState<boolean | null>(null);
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
