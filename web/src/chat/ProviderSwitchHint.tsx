import type { AgentRunConfig, AgentTask, ProjectSnapshot } from "../core/types";
import { providerSwitchStartsFreshSession } from "./chatWorkspace";

/** Above a composer whose next turn changes provider: that turn starts a new provider session. */
export function ProviderSwitchHint({
  project,
  config,
  tasks,
  sessionId,
}: {
  project: ProjectSnapshot;
  config: AgentRunConfig;
  tasks: AgentTask[];
  sessionId: string | null | undefined;
}) {
  if (!providerSwitchStartsFreshSession(tasks, sessionId, config.provider)) return null;
  const provider = project.provider_readiness[config.run_on]?.[config.provider]?.label;
  return (
    <div className="chat-composer-hint" role="status">
      The next turn starts a fresh {provider || config.provider} session.
    </div>
  );
}
