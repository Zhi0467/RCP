import { useEffect, useState } from "react";
import { checkMachineCompute } from "../core/api";
import type { ComputeBackendProbe, ComputeRoute, Machine } from "../core/types";
import { spaceMachineById } from "../projects/spaceMachines";
import { useSpaceMachines } from "../projects/useSpaceMachines";
import { Banner } from "../ui/Banner";
import { forgetBanners } from "../ui/bannerDismissal";
import { CompactNotice } from "../ui/CompactNotice";

interface Props {
  apiBase: string;
  machines: Machine[];
  onOpenSettings: () => void;
}

interface RouteFailure {
  machine: Machine;
  route: ComputeRoute;
  probe: ComputeBackendProbe;
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
  const [error, setError] = useState<{ machineId: string; message: string } | null>(null);
  const failures = machines.flatMap((machine) =>
    (machine.compute?.job_manager ? (["scheduler", "helper"] as const) : (["helper"] as const))
      .map((route) => ({
        machine,
        route,
        // A project cached by an older version has no split probes.
        probe: currentProbes(machine)?.[route],
      }))
      .filter((item): item is RouteFailure => Boolean(item.probe && !item.probe.ready)),
  );
  // A route back to ready ends its failure; a later failure shows again.
  const readyRoutes = machines
    .flatMap((machine) =>
      (["scheduler", "helper"] as const)
        .filter((route) => currentProbes(machine)?.[route]?.ready)
        .map((route) => `compute-route:${apiBase}:${machine.machine_id}:${route}:`),
    )
    .join("\n");
  useEffect(() => {
    for (const prefix of readyRoutes.split("\n")) if (prefix) forgetBanners(prefix);
  }, [readyRoutes]);
  function currentProbes(machine: Machine) {
    const override = checked[machine.machine_id];
    return override?.apiBase === apiBase && override.basis === machine.compute_probes
      ? override.probes
      : machine.compute_probes;
  }
  async function check(machine: Machine) {
    const machineId = machine.machine_id;
    const basis = machine.compute_probes;
    setChecking(machineId);
    setError(null);
    try {
      const probes = await checkMachineCompute(apiBase, machineId);
      setChecked((current) => ({ ...current, [machineId]: { apiBase, basis, probes } }));
    } catch (failure) {
      setError({
        machineId,
        message: failure instanceof Error ? failure.message : String(failure),
      });
    } finally {
      setChecking(null);
    }
  }
  return failures.length ? (
    <RouteFailureNotices
      apiBase={apiBase}
      failures={failures}
      checking={checking}
      error={error}
      onCheck={(machine) => void check(machine)}
      onOpenSettings={onOpenSettings}
    />
  ) : null;
}

/** The notices themselves; only a failing route reads the card names. */
function RouteFailureNotices({
  apiBase,
  failures,
  checking,
  error,
  onCheck,
  onOpenSettings,
}: {
  apiBase: string;
  failures: RouteFailure[];
  checking: string | null;
  error: { machineId: string; message: string } | null;
  onCheck: (machine: Machine) => void;
  onOpenSettings: () => void;
}) {
  const cards = useSpaceMachines().machines ?? [];
  return (
    <>
      {failures.map(({ machine, route, probe }) => {
        const name = spaceMachineById(cards, machine.machine_id)?.name;
        const where = name ? ` on ${name}` : "";
        return (
          <Banner
            // One failure of one route: a different diagnostic shows again.
            dismissKey={`compute-route:${apiBase}:${machine.machine_id}:${route}:${probe.diagnostic}`}
            className="provider-login-notice"
            role="status"
            key={`${machine.machine_id}:${route}`}
          >
            <CompactNotice
              summary={`${route === "scheduler" ? "Job scheduler" : "Job helper"} not ready${where}`}
              footer={
                error?.machineId === machine.machine_id ? <p role="alert">{error.message}</p> : null
              }
            >
              <p>
                {route === "scheduler" ? "The job scheduler" : "The long-running job helper"} is not
                ready{where}: {probe.diagnostic}
              </p>
              {probe.required_action ? <p>Fix: {probe.required_action}</p> : null}
              <div className="provider-login-notice-actions">
                <button className="button secondary compact" type="button" onClick={onOpenSettings}>
                  Open Settings
                </button>
                <button
                  className="button secondary compact"
                  type="button"
                  disabled={checking !== null}
                  onClick={() => onCheck(machine)}
                >
                  {checking === machine.machine_id ? "Checking…" : "Fixed it? Check again"}
                </button>
              </div>
            </CompactNotice>
          </Banner>
        );
      })}
    </>
  );
}
