import { useEffect, useState } from "react";
import { loadMachinePower } from "../api";
import { errorMessage } from "../errors";
import type { MachinePowerStatus } from "../types";
import { EXPERIMENT_BOARD_POLL_DELAY_MS } from "../hooks/useProjectTabs";

export function startMachinePowerPolling(
  receive: (status: MachinePowerStatus) => void,
  fail: (error: unknown) => void,
) {
  let active = true;
  let timer = 0;
  const poll = async () => {
    try {
      const next = await loadMachinePower();
      if (active) receive(next);
    } catch (failure) {
      if (active) fail(failure);
    } finally {
      if (active) {
        timer = window.setTimeout(() => void poll(), EXPERIMENT_BOARD_POLL_DELAY_MS);
      }
    }
  };
  void poll();
  return () => {
    active = false;
    window.clearTimeout(timer);
  };
}

/** Polls while mounted, so the status follows work starting and ending. */
export function useMachinePower(spaceKind: "personal" | "team" | undefined) {
  const [status, setStatus] = useState<MachinePowerStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (spaceKind !== "personal") {
      setStatus(null);
      setError(null);
      return;
    }
    return startMachinePowerPolling(
      (next) => {
        setStatus(next);
        setError(null);
      },
      (failure) => setError(errorMessage(failure)),
    );
  }, [spaceKind]);
  return { status: spaceKind === "personal" ? status : null, setStatus, error };
}
