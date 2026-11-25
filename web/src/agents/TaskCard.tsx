import {
  Bot,
  ChevronRight,
  FileText,
  FlaskConical,
  Telescope,
  Trash2,
  type LucideIcon,
} from "lucide-react";
import { useState, type ComponentProps, type ReactNode } from "react";
import { openEpisodeReport } from "../artifacts/artifactViewerModel";
import { errorMessage } from "../core/errors";
import type { AgentQuestion, ApprovalCard, DockEpisode } from "../core/types";
import { EpisodeControls } from "../experiments/EpisodeControls";
import { useEpisodeActions } from "../experiments/episodeActions";
import type { GlossaryIndex } from "../graph/glossary";
import { graphProfile } from "../graph/graphResources";
import { relativeTime } from "../projects/commentMentions";
import { useResource } from "../core/useResource";
import type { OverflowMenuItem } from "../ui/OverflowMenu";
import { ApprovalCardShell } from "./ApprovalCard";
import { approvalCardTitle } from "./agentThreadModel";
import { EpisodePlanCard, HandoffText, PlanNodeChip } from "./EpisodePlanCard";
import { planCardAction, planGraphTarget, tapPlanCard } from "./experimentPlanModel";
import { TaskStateBadge } from "./TaskCardHeader";
import { taskCardView } from "./taskCardModel";

/**
 * One run's controls for `EpisodeControls`, with the busy and error state a
 * card keeps while a control is in flight.
 */
function useRunControls(run: DockEpisode | null, onChanged: () => void | Promise<unknown>) {
  const episode = run?.episode ?? null;
  const actions = useEpisodeActions(episode?.project_id ?? null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [taskActionId, setTaskActionId] = useState<string | null>(null);
  const [additionalTurns, setAdditionalTurns] = useState("");
  const [error, setError] = useState<string | null>(null);
  const settle = async (act: () => Promise<unknown>) => {
    try {
      await act();
    } finally {
      await onChanged();
      setBusyAction(null);
      setTaskActionId(null);
    }
  };
  const report = episode?.presentation.actions.find((action) => action.kind === "open_report");
  const reportEpisodeId = report?.report_episode_id;
  const controls: ComponentProps<typeof EpisodeControls> = {
    presentation: episode?.presentation ?? null,
    episode,
    busyAction,
    taskActionId,
    additionalTurns,
    onAdditionalTurnsChange: setAdditionalTurns,
    onOperateTask: (task, action) => {
      setTaskActionId(task.operation_id);
      return settle(() => actions.operateTask(task, action));
    },
    onContinue: (episodeId, turns) => {
      setBusyAction(`continue:${episodeId}`);
      return settle(() => actions.addTurns(episodeId, turns));
    },
    onStop: (episodeId) => {
      setBusyAction(`stop:${episodeId}`);
      return settle(() => actions.stop(episodeId, episode?.tasks ?? []));
    },
    onError: setError,
  };
  return {
    controls,
    error,
    openReport:
      episode && reportEpisodeId
        ? () => {
            setError(null);
            openEpisodeReport({
              projectId: episode.project_id,
              episodeId: reportEpisodeId,
            }).catch((failure: unknown) => setError(errorMessage(failure)));
          }
        : null,
  };
}

function cardIcon(card: ApprovalCard | null | undefined, run: DockEpisode | null): LucideIcon {
  if (card) return card.action.kind === "experiment_start" ? FlaskConical : Telescope;
  return run?.episode.control_node_id ? FlaskConical : Telescope;
}

function cardHandoff(card: ApprovalCard | null | undefined, run: DockEpisode | null): string {
  const action = planCardAction(card);
  if (action?.kind === "experiment_start") return action.goal;
  return action?.starting_instruction ?? run?.episode.starting_instruction ?? "";
}

export interface TaskCardProps {
  /** The plan card; absent for a run started from its node page. */
  question?: AgentQuestion | null;
  /** The run the card started, or the run alone. */
  run?: DockEpisode | null;
  actorId: string | null;
  glossaryIndex?: GlossaryIndex;
  onOpenNode?: (nodeId: string) => void;
  /** Scrolls the chat to a turn; a Run in this chat card links its turn through it. */
  onOpenTurn?: (operationId: string) => void;
  onChanged: () => void | Promise<unknown>;
}

/**
 * One task from proposal to result. While its plan is open it is the plan
 * editor; from Start on it shows the run as the server presents it.
 */
export function TaskCard(props: TaskCardProps) {
  const { question, run = null } = props;
  const card = question?.card;
  const view = taskCardView(card, run?.episode.presentation);
  if (question && view.open && planCardAction(card))
    return (
      <EpisodePlanCard
        question={question}
        actorId={props.actorId}
        glossaryIndex={props.glossaryIndex}
        onOpenNode={props.onOpenNode}
        onSettled={props.onChanged}
      />
    );
  return <StartedTaskCard {...props} run={run} />;
}

function StartedTaskCard({
  question,
  run,
  onOpenNode,
  onOpenTurn,
  onChanged,
}: TaskCardProps & { run: DockEpisode | null }) {
  const card = question?.card;
  const view = taskCardView(card, run?.episode.presentation);
  const menu = useRunControls(run, onChanged);
  const [handoffShown, setHandoffShown] = useState(false);
  const action = planCardAction(card);
  const handoff = cardHandoff(card, run);
  const episode = run?.episode;
  const turn = card?.run_here_operation_id;
  const sub = episode && (
    <>
      {card && `${run.title} · `}
      {episode.ended_at ? "ended " : "started "}
      <time dateTime={episode.ended_at ?? episode.created_at}>
        {relativeTime(episode.ended_at ?? episode.created_at)}
      </time>
    </>
  );
  const actions: ReactNode =
    run || turn ? (
      <>
        {run && handoff && (
          <button
            className="approval-see"
            type="button"
            aria-expanded={handoffShown}
            onClick={() => setHandoffShown(!handoffShown)}
          >
            Show handoff
            <ChevronRight size={14} aria-hidden="true" />
          </button>
        )}
        {run && (
          <EpisodeControls {...menu.controls} variant="menu" menuLabel={`${run.title} actions`} />
        )}
        {menu.openReport && (
          <button className="button secondary" type="button" onClick={menu.openReport}>
            Open report
          </button>
        )}
        {run && !episode?.ended_at && (
          <a className="button secondary" href={run.link}>
            Open agent
          </a>
        )}
        {turn && onOpenTurn && (
          <button className="button secondary" type="button" onClick={() => onOpenTurn(turn)}>
            Open turn
          </button>
        )}
      </>
    ) : null;
  return (
    <ApprovalCardShell
      icon={cardIcon(card, run)}
      title={card ? approvalCardTitle(card) : (run?.title ?? "")}
      className={`plan task${handoffShown ? " open" : ""}`}
      cardId={question?.question_id ?? episode?.episode_id}
      state={view.state}
      stateLabel={view.label}
      tone={view.tone}
      project={null}
      chip={
        action?.kind === "experiment_start" ? (
          <PlanNodeChip action={action} onOpenNode={onOpenNode} />
        ) : undefined
      }
      askedAt={question?.created_at ?? episode?.created_at ?? ""}
      sub={sub}
      body={
        handoffShown ? (
          <div className="approval-details plan-body">
            <dl className="plan-section run">
              <dt>Handoff</dt>
              <dd>
                <HandoffText text={handoff} />
              </dd>
            </dl>
          </div>
        ) : undefined
      }
      error={menu.error}
      actions={actions}
    />
  );
}

/** A parked plan's title in a compact row: its Experiment's, once the graph is read. */
function usePlanTitle(question: AgentQuestion | null): string {
  const action = planCardAction(question?.card);
  const graph = useResource(
    `task-row:${question?.question_id ?? ""}`,
    action?.kind === "experiment_start"
      ? graphProfile(action.project_id, planGraphTarget(action))
      : null,
  );
  if (!question?.card) return "";
  const node =
    action?.kind === "experiment_start" ? graph.value?.graph.nodes[action.node_id] : undefined;
  return node?.title ?? approvalCardTitle(question.card);
}

/**
 * A task card in one line, for the pane. A press shows the full card: in its
 * turn when it has one, else right here under the row.
 */
export function TaskCardRow({
  question = null,
  run = null,
  actorId,
  glossaryIndex,
  onShow,
  onChanged,
}: Pick<TaskCardProps, "question" | "run" | "actorId" | "glossaryIndex" | "onChanged"> & {
  /** Scrolls the chat to the full card; false when the chat does not show it. */
  onShow: () => boolean;
}) {
  const card = question?.card;
  const view = taskCardView(card, run?.episode.presentation);
  const menu = useRunControls(run, onChanged);
  const planTitle = usePlanTitle(run ? null : question);
  const [expanded, setExpanded] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const title = run?.title ?? planTitle;
  const Icon = cardIcon(card, run);
  const items: OverflowMenuItem[] = [];
  if (run)
    items.push({
      label: "Open agent",
      icon: Bot,
      onSelect: () => window.location.assign(run.link),
    });
  if (menu.openReport)
    items.push({ label: "Open report", icon: FileText, onSelect: menu.openReport });
  if (!run && question?.card && question.can_answer) {
    const pinned = question.card.pinned_sha256;
    items.push({
      label: "Delete",
      icon: Trash2,
      disabled: deleting,
      onSelect: () => {
        setDeleting(true);
        setDeleteError(null);
        void tapPlanCard(question.question_id, { decision: "decline", pinned_sha256: pinned })
          .then((outcome) => {
            if (outcome.kind === "pending") setDeleteError(outcome.reason);
            return onChanged();
          })
          .finally(() => setDeleting(false));
      },
    });
  }
  const error = menu.error ?? deleteError;
  return (
    <li className="task-row" data-phase={view.phase} data-open={expanded ? "" : undefined}>
      <div className="task-row-line">
        <button
          type="button"
          className="task-row-main"
          aria-expanded={expanded}
          onClick={() => {
            if (expanded) setExpanded(false);
            else if (!onShow()) setExpanded(true);
          }}
        >
          <span className="approval-icon" aria-hidden="true">
            <Icon size={14} />
          </span>
          <strong title={title}>{title}</strong>
          {error ? (
            <span className="task-row-error" role="alert" title={error}>
              {error}
            </span>
          ) : run ? (
            <TaskStateBadge presentation={run.episode.presentation} />
          ) : (
            <span className="question-state">{view.badge}</span>
          )}
        </button>
        {run && (
          <a
            className="icon-button task-row-agent"
            href={run.link}
            aria-label="Open agent"
            title="Open agent"
          >
            <Bot size={15} aria-hidden="true" />
          </a>
        )}
        <EpisodeControls
          {...menu.controls}
          variant="menu"
          menuLabel={`${title} actions`}
          menuItems={items}
        />
      </div>
      {expanded && (
        <TaskCard
          question={question}
          run={run}
          actorId={actorId}
          glossaryIndex={glossaryIndex}
          onChanged={onChanged}
        />
      )}
    </li>
  );
}
