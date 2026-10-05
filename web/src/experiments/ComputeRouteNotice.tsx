import { useState } from "react";
import { checkMachineCompute } from "../api";
import type { Machine } from "../types";

interface Props {
  apiBase: string;
  machines: Machine[];
  onOpenSettings: () => void;
}

/** One notice per compute route that RCP checked and found unable to run jobs. */
export function ComputeRouteNotice({ apiBase, machines, onOpenSettings }: Props) {
  // A check answers before the next project refresh. Its result applies only
  // to the project and probe snapshot it was taken against.
  const [checked, setChecked] = useState<
    Record<
      string,
      { apiBase: string; basis: Machine["compute_probes"]; probes: Machine["compute_probes"] }
    >
  >({});
  const [checking, setChecking] = useState<string | null>(null);
  const [error, setError] = useState<{ alias: string; message: string } | null>(null);
  const failures = machines.flatMap((machine) =>
    (machine.compute?.job_manager ? (["scheduler", "helper"] as const) : (["helper"] as const))
      .map((route) => ({
        machine,
        route,
        // A project cached by an older version has no split probes.
        probe: currentProbes(machine)?.[route],
      }))
      .filter(({ probe }) => probe && !probe.ready),
  );
  function currentProbes(machine: Machine) {
    const override = checked[machine.alias];
    return override?.apiBase === apiBase && override.basis === machine.compute_probes
      ? override.probes
      : machine.compute_probes;
  }
  async function check(machine: Machine) {
    const alias = machine.alias;
    const basis = machine.compute_probes;
    setChecking(alias);
    setError(null);
    try {
      const probes = await checkMachineCompute(apiBase, alias);
      setChecked((current) => ({ ...current, [alias]: { apiBase, basis, probes } }));
    } catch (failure) {
      setError({ alias, message: failure instanceof Error ? failure.message : String(failure) });
    } finally {
      setChecking(null);
    }
  }
  return (
    <>
      {failures.map(({ machine, route, probe }) => (
        <div className="provider-login-notice" role="status" key={`${machine.alias}:${route}`}>
          <p>
            {route === "scheduler" ? "The job scheduler" : "The long-running job helper"} is not
            ready on {machine.alias}: {probe!.diagnostic}
          </p>
          {probe!.required_action ? <p>Fix: {probe!.required_action}</p> : null}
          <div className="provider-login-notice-actions">
            <button className="button secondary compact" type="button" onClick={onOpenSettings}>
              Open Settings
            </button>
            <button
              className="button secondary compact"
              type="button"
              disabled={checking !== null}
              onClick={() => void check(machine)}
            >
              {checking === machine.alias ? "Checking…" : "Fixed it? Check again"}
            </button>
          </div>
          {error?.alias === machine.alias && <p role="alert">{error.message}</p>}
        </div>
      ))}
    </>
  );
}
