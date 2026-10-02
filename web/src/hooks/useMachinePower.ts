import { useEffect, useState } from "react";
import { loadMachinePower } from "../api";
import { errorMessage } from "../errors";
import type { MachinePowerStatus } from "../types";

/** Settings mounts afresh; the home page supplies its existing refresh result. */
export function useMachinePower(spaceKind: "personal" | "team" | undefined, refresh?: unknown) {
  const [status, setStatus] = useState<MachinePowerStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (spaceKind !== "personal") {
      setStatus(null);
      setError(null);
      return;
    }
    let active = true;
    loadMachinePower().then(
      (next) => {
        if (!active) return;
        setStatus(next);
        setError(null);
      },
      (failure) => {
        if (active) setError(errorMessage(failure));
      },
    );
    return () => {
      active = false;
    };
  }, [spaceKind, refresh]);
  return { status: spaceKind === "personal" ? status : null, setStatus, error };
}
