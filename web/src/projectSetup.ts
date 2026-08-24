export function stateRepositoryAfterRemoval(
  repositories: Array<{ id: number; alias: string }>,
  removedId: number,
  stateRepository: string,
): string {
  const removed = repositories.find((repository) => repository.id === removedId);
  if (removed?.alias !== stateRepository) return stateRepository;
  return repositories.find((repository) => repository.id !== removedId)?.alias ?? "";
}

export function repositoryPickerPresentation(location: "local" | "ssh", desktop: boolean) {
  return {
    showPicker: location === "local" && desktop,
    hint:
      location === "local" && !desktop
        ? "Paste an absolute path. Finder selection is available in the desktop app."
        : null,
  };
}
