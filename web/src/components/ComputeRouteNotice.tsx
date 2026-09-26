import type { Machine } from "../types";

interface Props {
  machines: Machine[];
  onOpenSettings: () => void;
}

/** One notice per compute route that RCP checked and found unable to run jobs. */
export function ComputeRouteNotice({ machines, onOpenSettings }: Props) {
  const failures = machines.flatMap((machine) =>
    (machine.compute?.job_manager ? (["scheduler", "helper"] as const) : (["helper"] as const))
      .map((route) => ({ machine, route, probe: machine.compute_probes[route] }))
      .filter(({ probe }) => probe && !probe.ready),
  );
  return (
    <>
      {failures.map(({ machine, route, probe }) => (
        <div className="provider-login-notice" role="status" key={`${machine.alias}:${route}`}>
          <p>
            {route === "scheduler" ? "The job scheduler" : "The long-running job helper"} is not
            ready on {machine.alias}: {probe!.diagnostic}
          </p>
          {probe!.required_action ? <p>Fix: {probe!.required_action}</p> : null}
          <button className="button secondary compact" type="button" onClick={onOpenSettings}>
            Open Settings
          </button>
        </div>
      ))}
    </>
  );
}
