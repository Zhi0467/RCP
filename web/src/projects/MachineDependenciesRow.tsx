import { useEffect, useState } from "react";
import { loadMachineDependencies } from "../core/api";
import { errorMessage } from "../core/errors";
import type { DependencyStatus } from "../core/types";
import { dependencyLabel, dependencyView } from "./machineDependenciesModel";

export function MachineDependenciesRow({ machineId }: { machineId: string }) {
  const [status, setStatus] = useState<DependencyStatus | null>(null);
  const [pending, setPending] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let cancelled = false;
    void loadMachineDependencies(machineId)
      .then(
        (result) => {
          if (!cancelled) setStatus(result);
        },
        (failure) => {
          if (!cancelled) setError(errorMessage(failure));
        },
      )
      .finally(() => {
        if (!cancelled) setPending(false);
      });
    return () => {
      cancelled = true;
    };
  }, [machineId]);
  async function checkAgain() {
    setPending(true);
    setError(null);
    setCopied(false);
    try {
      setStatus(await loadMachineDependencies(machineId, true));
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setPending(false);
    }
  }
  const view = status ? dependencyView(status) : null;
  const command = view?.command ?? null;
  return (
    <section
      className="machine-browser-row machine-dependencies-row"
      aria-label="Dependencies"
      aria-busy={pending}
      data-dependency-outcome={view?.outcome}
    >
      <header>
        <strong>Dependencies</strong>
        <span role="status">{pending ? "Checking…" : view && dependencyLabel(view)}</span>
        {view?.system && (
          <span className="machine-dependencies-system">
            {view.system}
            {view.untested && (
              <span className="machine-dependencies-untested" data-dependency-untested="">
                {" "}
                (not tested)
              </span>
            )}
          </span>
        )}
      </header>
      {view?.reason && <p>{view.reason}</p>}
      {view && view.required.length > 0 && (
        <ul className="machine-dependencies-required" data-dependency-list="required">
          {view.required.map((program) => (
            <li key={program.name} data-dependency-program={program.name}>
              <code>{program.name}</code> {program.purpose}
            </li>
          ))}
        </ul>
      )}
      {view && view.optional.length > 0 && (
        <ul className="machine-dependencies-optional" data-dependency-list="optional">
          {view.optional.map((program) => (
            <li key={program.name} data-dependency-program={program.name}>
              <code>{program.name}</code> {program.feature} {program.fallback}
            </li>
          ))}
        </ul>
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
      {view && view.notes.length > 0 && (
        <ul className="machine-dependencies-notes">
          {view.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      <div className="provider-login-notice-actions">
        <button
          type="button"
          className="button secondary compact"
          disabled={pending}
          onClick={() => void checkAgain()}
        >
          Check again
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
