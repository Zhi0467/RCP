import { useState } from "react";
import { createRoot } from "react-dom/client";
import { mergeEpisodeToMain } from "../../src/api";
import { AutoResearchEpisodeCard } from "../../src/components/CampaignRuns";
import type { Episode } from "../../src/types";
import "../../src/styles.css";

const initialEpisode: Episode = await (await fetch("/fixture/episode")).json();
const idle = async () => {};

function Fixture() {
  const [episode, setEpisode] = useState(initialEpisode);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  Object.assign(window, {
    refreshMergeEpisode: async () => setEpisode(await (await fetch("/fixture/episode")).json()),
  });
  return (
    <main style={{ padding: 24 }}>
      <AutoResearchEpisodeCard
        episode={episode}
        messages={[]}
        initiallyExpanded
        busyAction={busyAction}
        taskActionId={null}
        onOpenExperimentEntry={idle}
        onInspectTask={idle}
        onLoadMessages={idle}
        onStop={idle}
        onMerge={async (episodeId) => {
          setBusyAction(`merge:${episodeId}`);
          try {
            setEpisode(await mergeEpisodeToMain(`/api/projects/${episode.project_id}`, episodeId));
          } finally {
            setBusyAction(null);
          }
        }}
        onReauthorize={idle}
        onSendMessage={idle}
        onOperateTask={idle}
        onArchive={idle}
      />
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
