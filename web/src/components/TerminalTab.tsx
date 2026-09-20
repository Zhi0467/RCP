import { useEffect, useState } from "react";
import { TerminalSquare } from "lucide-react";
import { api } from "../api";
import type { TerminalRepository } from "../types";

export function TerminalTab({
  projectId,
  refreshKey,
  active,
  onClick,
  onError,
}: {
  projectId: string;
  refreshKey: string;
  active: boolean;
  onClick: () => void;
  onError: (message: string) => void;
}) {
  const [available, setAvailable] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const repositories = await api<TerminalRepository[]>(
          `/api/projects/${encodeURIComponent(projectId)}/terminals/repositories`,
          { signal: controller.signal },
        );
        if (controller.signal.aborted) return;
        // Remote failures remain reachable here so members can read the reason
        // and explicitly refresh after repairing the machine.
        setAvailable(
          repositories.some((repository) => repository.eligible || repository.probe_state),
        );
        if (repositories.some((repository) => repository.probe_state === "pending"))
          timer = setTimeout(() => void load(), 1000);
      } catch (error) {
        if (!controller.signal.aborted) {
          setAvailable(false);
          onError(
            `Terminal availability could not be loaded: ${error instanceof Error ? error.message : String(error)}`,
          );
        }
      }
    };
    void load();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [projectId, refreshKey, onError]);
  if (!available) return null;
  return (
    <button
      className={active ? "active" : ""}
      aria-current={active ? "page" : undefined}
      onClick={onClick}
    >
      <TerminalSquare size={14} />
      <span>Terminals</span>
    </button>
  );
}
