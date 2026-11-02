import type { AgentThread } from "../core/types";

/** What an empty thread shows before its first message. */
export type ThreadWelcome = {
  title: string;
  subtitle: string;
  /** The agent's capabilities, as the backend renders them from its granted verbs. */
  lines: readonly string[];
  /** Starters that fill the composer; none sends. */
  prompts: readonly string[];
};

/**
 * The welcome for a thread with no messages yet. Pip's third starter names the
 * member's first project, or offers to set one up when there is none.
 */
export function threadWelcome(
  thread: Pick<AgentThread, "kind" | "agent_name" | "project_name" | "capabilities">,
  firstProjectName: string | null,
): ThreadWelcome {
  const lines = thread.capabilities ?? [];
  if (thread.kind === "space")
    return {
      title: thread.agent_name || "Pip",
      subtitle: "Your agent across every project",
      lines,
      prompts: [
        "What changed in my projects this week?",
        "Remind me tomorrow at 9 to review results",
        firstProjectName
          ? `Start an experiment in ${firstProjectName}`
          : "Help me set up a project",
      ],
    };
  return {
    title: thread.agent_name || thread.project_name || "Project agent",
    subtitle: thread.project_name ?? "",
    lines,
    prompts: [
      "Summarize this project's open questions",
      "What should we try next?",
      "Prepare an experiment for the main hypothesis",
    ],
  };
}
