export function stateRepositoryAfterRemoval(
  repositories: Array<{ id: number; alias: string }>,
  removedId: number,
  stateRepository: string,
): string {
  const removed = repositories.find((repository) => repository.id === removedId);
  if (removed?.alias !== stateRepository) return stateRepository;
  return repositories.find((repository) => repository.id !== removedId)?.alias ?? "";
}
