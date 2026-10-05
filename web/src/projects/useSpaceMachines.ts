import { useCallback, useEffect, useState } from "react";
import { loadSpaceMachines } from "../core/api";
import { errorMessage } from "../core/errors";
import { replaceSpaceMachine } from "./spaceMachines";
import type { SpaceMachine } from "../core/types";

/** The space machine list, read once per mount and patched in place by each edit. */
export function useSpaceMachines() {
  const [machines, setMachines] = useState<SpaceMachine[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(async () => {
    try {
      setMachines(await loadSpaceMachines());
      setError(null);
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);
  const replace = useCallback((machine: SpaceMachine) => {
    setMachines((current) => replaceSpaceMachine(current ?? [], machine));
  }, []);
  const remove = useCallback((machineId: string) => {
    setMachines(
      (current) => current?.filter((machine) => machine.machine_id !== machineId) ?? null,
    );
  }, []);
  return { machines, error, reload, replace, remove };
}
