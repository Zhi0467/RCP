import { useEffect, useState } from "react";
import { loadMachineDependencies } from "../core/api";
import { CopyCommand } from "../core/CopyCommand";
import { errorMessage } from "../core/errors";
import { Fold } from "../core/Fold";
import type { DependencyStatus } from "../core/types";
import { dependencyLabel, dependencyView } from "./machineDependenciesModel";

export function MachineDependenciesRow({ machineId }: { machineId: string }) {
  const [status, setStatus] = useState<DependencyStatus | null>(null);
  const [pending, setPending] = useState(true);
  const [error, setError] = useState<string | null>(null);
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
  const bad =
    !pending && (error !== null || view?.outcome === "missing" || view?.outcome === "unsupported");
  return (
    <Fold
      className="machine-browser-row machine-dependencies-row"
      title="Dependencies"
      status={
        <span role="status" data-tone={bad ? "bad" : undefined}>
          {pending ? "Checking…" : view && dependencyLabel(view)}
        </span>
      }
      aria-label="Dependencies"
      aria-busy={pending}
      data-dependency-outcome={view?.outcome}
    >
      {view?.system && (
        <p className="machine-dependencies-system">
          {view.system}
          {view.untested && (
            <span className="machine-dependencies-untested" data-dependency-untested="">
              {" "}
              (not tested)
            </span>
          )}
        </p>
      )}
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
      {command && <CopyCommand key={command} command={command} />}
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
    </Fold>
  );
}
