import { useState } from "react";
import { createRoot } from "react-dom/client";
import { useTheme } from "../../src/hooks/useTheme";
import { ProjectLanding } from "../../src/views/ProjectLanding";
import type { ProjectCreationControl, SpaceRunIndexEntry } from "../../src/types";
import "../../src/styles.css";

const noop = () => undefined;
const resolved = async () => undefined;
const creation: ProjectCreationControl = {
  requires_authenticated_member: false,
  intents: [
    {
      intent: "use_existing_checkout_personally",
      eligible: true,
      preselected: true,
      primary_action_label: "Use existing checkout",
      required_fields: ["repositories"],
      pinned_source_project_id: null,
      unavailable_reason: null,
    },
  ],
};
const runs: SpaceRunIndexEntry[] = [
  {
    episode_id: "episode-1",
    project_id: "project-1",
    project_name: "Research project",
    project_reachable: true,
    mode: "experiment_loop",
    title: "Measure transfer",
    graph_target: { kind: "main", branch_id: null },
    parent_episode_id: null,
    experiment_id: "experiment/transfer",
    started_at: new Date().toISOString(),
    last_activity_at: new Date().toISOString(),
    health_label: "Needs action",
    health_tone: "actionable",
    run_section: "actionable",
    archived: false,
    can_archive: false,
    authorized_by: null,
  },
];

function Fixture() {
  const appearance = useTheme();
  const [spaceRuns, setSpaceRuns] = useState(runs);
  Object.assign(window, { refreshSpaceRuns: () => setSpaceRuns((current) => [...current]) });
  return (
    <ProjectLanding
      themeChoice={appearance.theme}
      colorModeChoice={appearance.mode}
      onThemeChoiceChange={appearance.setTheme}
      onColorModeChoiceChange={appearance.setMode}
      palette={appearance.palette}
      projects={[]}
      invitations={[]}
      onAnswerInvitation={resolved}
      spaceRuns={spaceRuns}
      onOpen={noop}
      onOpenExperiment={noop}
      onArchiveEpisode={resolved}
      onCreate={noop}
      projectCreation={creation}
      onDelete={noop}
      openProjectTabs={[]}
      onActivateProjectTab={noop}
      onCloseProjectTab={noop}
      identity={{
        space_id: "personal-space",
        space_kind: "personal",
        space_name: "Personal space",
        user: {
          user_id: "researcher",
          display_name: "Ada",
          identity_kind: "local_owner",
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          removal_started_at: null,
          removed_at: null,
        },
      }}
      identityError={null}
      onRequestIdentityName={noop}
    />
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
