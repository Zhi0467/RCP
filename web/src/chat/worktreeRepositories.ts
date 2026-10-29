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

/** The repository a worktree launch names: an inherited worktree's own, else the pick, else the first. */
export function chosenWorktreeRepository(
  choices: string[],
  pick: string | null,
  inherited: string | null,
): string | null {
  if (inherited) return inherited;
  return pick && choices.includes(pick) ? pick : (choices[0] ?? null);
}
