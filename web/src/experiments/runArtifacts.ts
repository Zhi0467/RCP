import type { RunArtifactEntry } from "../types";

/** Preserve the server's run-report-first order, including child reports. */
export function orderRunArtifacts(artifacts: readonly RunArtifactEntry[]): RunArtifactEntry[] {
  return [...artifacts];
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
