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
    void api<TerminalRepository[]>(
      `/api/projects/${encodeURIComponent(projectId)}/terminals/repositories`,
      { signal: controller.signal },
    ).then(
      (repositories) => {
        if (!controller.signal.aborted)
          setAvailable(repositories.some((repository) => repository.eligible));
      },
      (error) => {
        if (!controller.signal.aborted) {
          setAvailable(false);
          onError(
            `Terminal availability could not be loaded: ${error instanceof Error ? error.message : String(error)}`,
          );
        }
      },
    );
    return () => controller.abort();
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
