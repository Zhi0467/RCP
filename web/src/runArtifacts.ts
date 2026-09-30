import type { RunArtifactEntry } from "./types";

/** Reports lead the list; the remaining artifacts retain their creation order. */
export function orderRunArtifacts(artifacts: readonly RunArtifactEntry[]): RunArtifactEntry[] {
  return [...artifacts].sort((a, b) => {
    const reportOrder =
      Number(b.supplier === "episode_ending") - Number(a.supplier === "episode_ending");
    return reportOrder || a.created_at.localeCompare(b.created_at);
  });
}

export function artifactsForOperations(
  artifacts: readonly RunArtifactEntry[],
  operationIds: readonly string[],
): RunArtifactEntry[] {
  const operations = new Set(operationIds.filter(Boolean));
  return orderRunArtifacts(
    artifacts.filter(
      (artifact) =>
        artifact.supplier === "turn" &&
        artifact.origin_operation_id !== null &&
        operations.has(artifact.origin_operation_id),
    ),
  );
}
