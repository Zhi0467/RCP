import type { ProjectSnapshot } from "../core/types";

/** The repositories a worktree on `runOn` may check out: writable ones on that machine. */
export function worktreeRepositoryChoices(
  project: Pick<ProjectSnapshot, "repositories" | "project_truth_scope">,
  runOn: string,
): string[] {
  return project.repositories
    .filter(
      (repository) =>
        repository.machine === runOn && project.project_truth_scope.includes(repository.alias),
    )
    .map((repository) => repository.alias);
}
