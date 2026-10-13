import { api } from "../core/api";
import type { ResourceProfile } from "../core/resourceStore";
import { projectResourceKey } from "../projects/projectResources";
import type { TerminalRepository, TerminalSession } from "../core/types";
export interface TerminalResources {
  repositories: TerminalRepository[];
  sessions: TerminalSession[];
}
export function terminalsProfile(projectId: string): ResourceProfile<TerminalResources> {
  const base = `/api/projects/${encodeURIComponent(projectId)}/terminals`;
  return {
    key: projectResourceKey(projectId, "terminals"),
    fetch: async (signal) => {
      const [repositories, sessions] = await Promise.all([
        api<TerminalRepository[]>(`${base}/repositories`, { signal }),
        api<TerminalSession[]>(base, { signal }),
      ]);
      return { repositories, sessions };
    },
  };
}
