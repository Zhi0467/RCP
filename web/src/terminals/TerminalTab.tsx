import { SquareTerminal } from "lucide-react";
import { useProjectResource } from "../projects/projectResources";
import { terminalsProfile } from "./terminalResources";

export function TerminalTab({
  projectId,
  active,
  onClick,
}: {
  projectId: string;
  refreshKey: string;
  active: boolean;
  onClick: () => void;
  onError: (message: string) => void;
}) {
  const resource = useProjectResource(terminalsProfile(projectId));
  const available = Boolean(
    resource.value?.sessions.length ||
    resource.value?.repositories.some(
      (repository) => repository.eligible || repository.probe_state,
    ),
  );
  if (!available) return null;
  return (
    <button
      className={active ? "active" : ""}
      aria-current={active ? "page" : undefined}
      onClick={onClick}
    >
      <SquareTerminal size={14} />
      <span>Terminals</span>
    </button>
  );
}
