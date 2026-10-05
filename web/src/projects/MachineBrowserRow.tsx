import { useEffect, useState } from "react";
import { installMachineBrowser, loadMachineBrowser } from "../core/api";
import { browserReason } from "../core/browserStatus";
import { errorMessage } from "../core/errors";
import type { MachineBrowserReadiness } from "../core/types";

export function MachineBrowserRow({
  machineId,
  disabled,
}: {
  machineId: string;
  disabled: boolean;
}) {
  const [readiness, setReadiness] = useState<MachineBrowserReadiness | null>(null);
  const [pending, setPending] = useState<"check" | "install" | null>("check");
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let cancelled = false;
    void loadMachineBrowser(machineId)
      .then(
        (result) => {
          if (!cancelled) setReadiness(result);
        },
        (failure) => {
          if (!cancelled) setError(errorMessage(failure));
        },
      )
      .finally(() => {
        if (!cancelled) setPending(null);
      });
    return () => {
      cancelled = true;
    };
  }, [machineId]);
  async function run(action: "check" | "install") {
    setPending(action);
    setError(null);
    setCopied(false);
    try {
      setReadiness(
        await (action === "install"
          ? installMachineBrowser(machineId)
          : loadMachineBrowser(machineId)),
      );
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setPending(null);
    }
  }
  const reason = readiness ? browserReason(readiness.status) : null;
  return (
    <section className="machine-browser-row" aria-label="Browser" aria-busy={pending !== null}>
      <header>
        <strong>Browser</strong>
        <span role="status">
          {pending === "install"
            ? "Installing… This can take up to 15 minutes."
            : pending === "check"
              ? "Checking…"
              : reason?.label}
        </span>
      </header>
      {readiness?.detail && <p>{readiness.detail}</p>}
      {reason?.fix && readiness?.status !== "not_installed" && <p>{reason.fix}</p>}
      {readiness?.status === "system_libraries_missing" && readiness.apt_command && (
        <div className="browser-install-command">
          <code>{readiness.apt_command}</code>
          <button
            type="button"
            className="button secondary compact"
            onClick={async () => {
              setError(null);
              try {
                await navigator.clipboard.writeText(readiness.apt_command!);
                setCopied(true);
              } catch {
                setError("Could not copy the command. Select it and copy it manually.");
              }
            }}
          >
            {copied ? "Copied" : "Copy command"}
          </button>
        </div>
      )}
      <div className="provider-login-notice-actions">
        {readiness?.status === "not_installed" && (
          <button
            type="button"
            className="button secondary compact"
            disabled={disabled || pending !== null}
            onClick={() => void run("install")}
          >
            Install
          </button>
        )}
        <button
          type="button"
          className="button secondary compact"
          disabled={pending !== null}
          onClick={() => void run("check")}
        >
          Check again
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
