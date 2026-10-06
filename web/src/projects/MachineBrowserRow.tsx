import { useEffect, useState } from "react";
import { enableMachineLinger, installMachineBrowser, loadMachineBrowser } from "../core/api";
import { browserReason } from "../core/browserStatus";
import { errorMessage } from "../core/errors";
import type { MachineBrowserReadiness } from "../core/types";

const INSTALL_POLL_MS = 15_000;

export function MachineBrowserRow({
  machineId,
  disabled,
}: {
  machineId: string;
  disabled: boolean;
}) {
  const [readiness, setReadiness] = useState<MachineBrowserReadiness | null>(null);
  const [pending, setPending] = useState<"check" | "install" | "linger" | null>("check");
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
  // An install started earlier, maybe from another page, finishes on its own.
  useEffect(() => {
    if (readiness?.status !== "installing" || pending !== null) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void loadMachineBrowser(machineId).then(
        (result) => {
          if (!cancelled) setReadiness(result);
        },
        (failure) => {
          if (!cancelled) setError(errorMessage(failure));
        },
      );
    }, INSTALL_POLL_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [machineId, readiness, pending]);
  async function run(action: "check" | "install" | "linger") {
    setPending(action);
    setError(null);
    setCopied(false);
    try {
      setReadiness(
        await (action === "install"
          ? installMachineBrowser(machineId)
          : action === "linger"
            ? enableMachineLinger(machineId)
            : loadMachineBrowser(machineId)),
      );
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setPending(null);
    }
  }
  const reason = readiness ? browserReason(readiness.status) : null;
  // A command the member must hand to an administrator, when RCP cannot run it itself.
  const command =
    (readiness?.status === "system_libraries_missing" && readiness.apt_command) ||
    (readiness?.status === "linger_disabled" && readiness.admin_command) ||
    null;
  return (
    <section className="machine-browser-row" aria-label="Browser" aria-busy={pending !== null}>
      <header>
        <strong>Browser</strong>
        <span role="status">
          {pending === "install"
            ? "Installing… This can take up to 15 minutes."
            : pending === "linger"
              ? "Allowing…"
              : pending === "check"
                ? "Checking…"
                : reason?.label}
        </span>
      </header>
      {readiness?.detail && <p>{readiness.detail}</p>}
      {reason?.fix && !["not_installed", "linger_disabled"].includes(readiness!.status) && (
        <p>{reason.fix}</p>
      )}
      {command && (
        <div className="browser-install-command">
          <code>{command}</code>
          <button
            type="button"
            className="button secondary compact"
            onClick={async () => {
              setError(null);
              try {
                await navigator.clipboard.writeText(command);
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
        {readiness?.status === "linger_disabled" && !readiness.admin_command && (
          <button
            type="button"
            className="button secondary compact"
            disabled={disabled || pending !== null}
            onClick={() => void run("linger")}
          >
            Allow background processes
          </button>
        )}
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
