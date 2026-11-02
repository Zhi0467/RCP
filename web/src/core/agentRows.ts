import type { AgentExecutionProfile } from "./types";

/** Seed and Refresh run on one profile. The Seed row sets both, so Refresh has
 * no row of its own and every save writes it the Seed choice. */
export function rowOf(profile: AgentExecutionProfile): AgentExecutionProfile {
  return profile === "refresh" ? "seed" : profile;
}

export function hasRow(profile: AgentExecutionProfile): boolean {
  return rowOf(profile) === profile;
}
