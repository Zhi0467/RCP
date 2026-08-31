import type {
  ProjectCreationControl,
  ProjectCreationIntent,
  ProjectProvisioningCreateRequest,
} from "./types";

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

export function selectedProjectCreationIntent(
  control: ProjectCreationControl,
): ProjectCreationIntent {
  const preselected = control.intents.filter((intent) => intent.eligible && intent.preselected);
  if (preselected.length === 1) return preselected[0].intent;
  const eligible = control.intents.filter((intent) => intent.eligible);
  if (eligible.length === 1) return eligible[0].intent;
  throw new Error("The backend did not select one available project setup intent.");
}

export function assertSupportedProjectCreationIntent(
  control: ProjectCreationControl,
  intent: ProjectCreationIntent,
): void {
  const selected = control.intents.find((item) => item.intent === intent);
  if (!selected?.eligible) {
    throw new Error("The selected project setup intent is not available from this backend.");
  }
  const expectedFields: Record<ProjectCreationIntent, string[]> = {
    use_existing_checkout_personally: [
      "name",
      "repositories",
      "state_repository",
      "execution",
      "confirmed",
    ],
    create_shared_team_project: ["machines", "repositories", "provider_checks"],
    move_personal_project_to_team: [],
  };
  const actualFields = new Set(selected.required_fields);
  if (
    actualFields.size !== selected.required_fields.length ||
    actualFields.size !== expectedFields[intent].length ||
    expectedFields[intent].some((field) => !actualFields.has(field))
  ) {
    throw new Error("This build does not support the backend's project setup field contract.");
  }
  if (intent !== "move_personal_project_to_team" && selected.pinned_source_project_id !== null) {
    throw new Error("Only a personal-to-team move may pin an existing source project.");
  }
}

export function projectCreationPrimaryLabel(control: ProjectCreationControl): string {
  const selected = selectedProjectCreationIntent(control);
  return control.intents.find((intent) => intent.intent === selected)?.primary_action_label ?? "";
}

export function projectProvisioningRequestId(hash: string): string | null {
  if (hash === "#/projects/new") return null;
  if (!hash.startsWith("#/projects/new?")) return null;
  const requestId = new URLSearchParams(hash.slice(hash.indexOf("?") + 1)).get("request");
  return requestId &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(requestId)
    ? requestId
    : null;
}

export function invalidProjectProvisioningHash(hash: string): boolean {
  return hash.startsWith("#/projects/new?") && projectProvisioningRequestId(hash) === null;
}

export function projectProvisioningHash(requestId: string): string {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(requestId)) {
    throw new Error("Project provisioning request identity must be a canonical UUID4.");
  }
  return `#/projects/new?request=${encodeURIComponent(requestId)}`;
}

export function formatCommandArgv(argv: string[]): string {
  return argv
    .map((value) =>
      value && /^[A-Za-z0-9_@%+=:,./-]+$/.test(value)
        ? value
        : `'${value.replaceAll("'", "'\\''")}'`,
    )
    .join(" ");
}

export function buildTeamProvisioningRequest({
  name,
  stateRepository,
  defaultAutoResearchInvocationCeiling,
  machines,
  repositories,
  providerChecks,
}: {
  name: string;
  stateRepository: string;
  defaultAutoResearchInvocationCeiling: number;
  machines: ProjectProvisioningCreateRequest["machines"];
  repositories: Array<
    ProjectProvisioningCreateRequest["repositories"][number] & { default_read: boolean }
  >;
  providerChecks: ProjectProvisioningCreateRequest["provider_checks"];
}): ProjectProvisioningCreateRequest {
  const aliases = repositories.map((repository) => repository.alias);
  return {
    name: name.trim(),
    state_repository: stateRepository,
    project_truth_scope: aliases,
    default_run_truth_scope: repositories
      .filter((repository) => repository.default_read)
      .map((repository) => repository.alias),
    default_auto_research_invocation_ceiling: defaultAutoResearchInvocationCeiling,
    machines,
    repositories: repositories.map(({ default_read: _defaultRead, ...repository }) => repository),
    provider_checks: providerChecks,
  };
}
