import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, archiveEpisode } from "../../src/api";
import { SpaceRuns } from "../../src/components/SpaceRuns";
import { parseProjectHash } from "../../src/experimentBoard";
import { useEpisodeDialogs } from "../../src/hooks/useEpisodeDialogs";
import { useProjectTabs } from "../../src/hooks/useProjectTabs";
import { ExecutionView } from "../../src/views/GraphViews";
import type { ProjectSnapshot } from "../../src/types";
import "../../src/styles.css";

const projectId = "project-one";
const apiBase = `/api/projects/${projectId}`;
const noop = () => undefined;
const resolved = () => Promise.resolve();
const initialRoute = parseProjectHash(window.location.hash);

function Fixture() {
  const [snapshot, setSnapshot] = useState<ProjectSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const tabs = useProjectTabs({
    initialProjectId: projectId,
    initialSetupOpen: false,
    projectIndexReady: false,
    project: null,
    reportError: setError,
  });
  const episodeState = useEpisodeDialogs({
    projectId,
    apiBase,
    selectedAutoResearchEpisodeId: null,
    isActiveProject: tabs.isActiveProject,
    runsVisible: true,
  });
  useEffect(() => {
    void Promise.all([
      api<ProjectSnapshot>(apiBase).then(setSnapshot),
      tabs.refreshProjectExperimentLoops(projectId),
      tabs.refreshSpaceRuns(),
    ]).catch((caught) => setError(String(caught)));
  }, []);
  const onArchive = async (requestedProjectId: string, episodeId: string, archived: boolean) => {
    const episode = await archiveEpisode(
      `/api/projects/${requestedProjectId}`,
      episodeId,
      archived,
    );
    episodeState.replaceEpisode(episode);
    tabs.replaceEpisodeRunArchive(episode);
    await Promise.all([
      episodeState.refreshEpisodes(),
      tabs.refreshProjectExperimentLoops(projectId),
      tabs.refreshSpaceRuns(),
    ]);
  };
  if (!snapshot) return null;
  return (
    <main style={{ padding: 24 }}>
      {error && <div role="alert">{error}</div>}
      <button
        type="button"
        onClick={() => {
          void Promise.all([
            episodeState.refreshEpisodes(),
            tabs.refreshProjectExperimentLoops(projectId),
            tabs.refreshSpaceRuns(),
          ]);
        }}
      >
        Refresh runs
      </button>
      <div data-surface="project">
        <ExecutionView
          graph={snapshot.graph}
          episodes={episodeState.episodes}
          episodeMessages={episodeState.episodeMessages}
          episodeAction={null}
          tasks={[]}
          watchers={[]}
          experimentControl={snapshot.experiment_control}
          experimentEntries={tabs.experimentLoops}
          exactExperimentRoute={initialRoute.experimentRoute}
          selectedExperimentId={null}
          focusExperimentId={null}
          runBusy={false}
          stopBusyId={null}
          watcherCheckBusyId={null}
          taskActionId={null}
          onInspectTask={noop}
          onLoadEpisodeMessages={episodeState.refreshEpisodeMessages}
          onStopEpisode={resolved}
          onArchiveEpisode={onArchive}
          onMergeEpisode={resolved}
          onReauthorizeEpisode={resolved}
          onSendEpisodeMessage={resolved}
          onOperateEpisodeTask={resolved}
          onSelectExperiment={noop}
          onOpenExperimentEntry={noop}
          onDetailFocused={noop}
          onOpenHistory={() => setHistoryOpen(true)}
          onRunExperiment={noop}
          onStopExperiment={noop}
          onCheckExperimentWatcher={noop}
          onRecoverExperiment={noop}
          onSwitchExperimentProvider={noop}
          episodeReportHref={() => "#"}
        />
      </div>
      {historyOpen && (
        <div role="dialog" aria-label="Project History">
          Retained episode history
        </div>
      )}
      <div data-surface="space">
        <SpaceRuns entries={tabs.spaceRuns} onOpen={noop} onArchive={onArchive} />
      </div>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
