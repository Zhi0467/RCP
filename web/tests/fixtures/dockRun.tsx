import { ResourceRuntime } from "./resourceRuntime";
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { ApprovalCardStack, dockStackCards } from "../../src/agents/ApprovalCardStack";
import { EpisodeControls } from "../../src/experiments/EpisodeControls";
import type { DockEpisode, Episode } from "../../src/core/types";
import { episode as base } from "./campaigns.mjs";
import { withEpisodeAnswers } from "../taskAnswers.mjs";
import "../../src/styles.css";

/**
 * A chat dock with one Experiment run paused for its human beside a running
 * Auto-research run. `?view=menu` draws the running run's controls as a menu.
 */
const ended = new Date().toISOString();
const paused = withEpisodeAnswers(
  {
    ...base,
    mode: "experiment_loop",
    control_node_id: "exp/one",
    health: "needs_action",
    can_stop: false,
    can_continue: true,
    task_control: null,
    ending: "human_pause",
    ended_at: ended,
    wrapup_state: "ready",
    report: { report_id: "r", ending: "human_pause", created_at: ended },
    budget: { ...base.budget, invocations_remaining: 3 },
  },
  "A long Experiment title that wraps beside its state",
) as unknown as Episode;
const running = withEpisodeAnswers({
  ...base,
  episode_id: "episode/beta",
}) as unknown as Episode;
const run = (episode: Episode, link: string): DockEpisode => ({
  episode,
  title: episode.presentation.title,
  source: "chat",
  link,
  questions: [],
});
Object.assign(window, { stopped: [], operated: [] });
const record = (name: "stopped" | "operated", value: unknown) =>
  (window as unknown as Record<string, unknown[]>)[name].push(value);

function Menu() {
  const [turns, setTurns] = useState("");
  return (
    <EpisodeControls
      variant="menu"
      menuLabel="Run actions"
      menuItems={[{ label: "Open agent", onSelect: () => undefined }]}
      presentation={running.presentation}
      episode={running}
      additionalTurns={turns}
      onAdditionalTurnsChange={setTurns}
      busyAction={null}
      taskActionId={null}
      onOperateTask={async (task, action) => void record("operated", [task.operation_id, action])}
      onStop={async (episodeId) => void record("stopped", episodeId)}
    />
  );
}

createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    {new URLSearchParams(location.search).get("view") === "menu" ? (
      <Menu />
    ) : (
      <div className="approval-dock">
        <ApprovalCardStack
          stackKey="dock-run"
          cards={dockStackCards([], [run(paused, "#/run"), run(running, "#/running")], [])}
          onDecided={() => undefined}
          onResolved={() => undefined}
          onAnswered={() => undefined}
          onChanged={async () => undefined}
        />
      </div>
    )}
  </ResourceRuntime>,
);
