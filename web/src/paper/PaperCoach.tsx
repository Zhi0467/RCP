import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { History, MessageCirclePlus } from "lucide-react";
import { isActiveTask } from "../agents/agentTasks";
import { AgentChatView } from "../chat/AgentChatView";
import { chatResourceProfile, chatsResourceProfile } from "../chat/chatResources";
import type { StartConversationTask } from "../chat/chatWorkspace";
import type { SendThreads } from "../comments/react/useComments";
import { MAIN_GRAPH } from "../core/graphTarget";
import type { AgentTask, GraphUpdateRecovery, ProjectSnapshot } from "../core/types";
import { useLayerDismiss } from "../core/useLayerDismiss";
import { useResource } from "../core/useResource";
import { panelTools } from "./panelTools";
import { PaperTurnChanges, PaperTurnDiff } from "./PaperTurn";
import "./paper-coach.css";

/** The open Coach chat, kept with the Paper's place; null opens the newest one. */
export interface CoachOpening {
  chatId: string | null;
}

/** A Coach turn's file shown as a diff. */
export interface CoachDiff {
  chatId: string;
  operationId: string;
  path: string;
}

/** The ordinary chat handlers the app gives every chat view. */
export interface CoachChatActions {
  actorId: string | null;
  onResumeTask: (task: AgentTask) => void;
  onRetryTask: (task: AgentTask) => void;
  onRefreshTask: (taskId: string) => Promise<AgentTask>;
  onInspectTask: (taskId: string) => void;
  onOpenInbox: () => void;
  onRepairGraphUpdate: (taskId: string, action?: GraphUpdateRecovery) => Promise<void>;
}

/** The Coach: the member's Paper chats in the ordinary chat view. Each finished turn
 *  lists the files it changed; a file's diff opens beside the chat and takes comments. */
export function PaperCoach({
  apiBase,
  project,
  tasks,
  onStartTask,
  actions,
  toolbar,
  threadSender,
  opening,
  onOpening,
  diff,
  onDiff,
  inlineDiff,
}: {
  apiBase: string;
  project: ProjectSnapshot;
  tasks: AgentTask[];
  onStartTask: StartConversationTask;
  actions: CoachChatActions;
  /** Where History and New chat sit: the Paper view row, while the Coach shows. */
  toolbar: HTMLElement | null;
  /** Set to the chat composer's sender, which Paper's comment threads ride. */
  threadSender: RefObject<SendThreads | null>;
  opening?: CoachOpening;
  onOpening: (opening: CoachOpening) => void;
  diff: CoachDiff | null;
  onDiff: (diff: CoachDiff | null) => void;
  /** The diff stacks above the chat here, as on a phone; else the host shows it beside. */
  inlineDiff: boolean;
}) {
  const inventory = useResource(
    `${project.id}:paper-chats`,
    useMemo(() => chatsResourceProfile(project.id, apiBase), [project.id, apiBase]),
  );
  const chats = useMemo(
    () =>
      (inventory.value?.summaries ?? [])
        .filter((summary) => summary.focus === "paper")
        .sort((left, right) => right.updated_at.localeCompare(left.updated_at)),
    [inventory.value],
  );
  const [picked, setPicked] = useState<string | null>(opening?.chatId ?? null);
  const [fresh] = useState(() => crypto.randomUUID());
  const chatId = picked ?? chats[0]?.chat_id ?? fresh;
  const summary = chats.find((chat) => chat.chat_id === chatId);
  useEffect(() => onOpening({ chatId: picked }), [picked, onOpening]);
  const transcript = useResource(
    `${project.id}:paper-chat`,
    useMemo(
      () => (summary ? chatResourceProfile(project.id, MAIN_GRAPH, chatId, apiBase) : null),
      [summary, project.id, chatId, apiBase],
    ),
  );
  const [historyOpen, setHistoryOpen] = useState(false);
  const historyButton = useRef<HTMLButtonElement>(null);
  const historyList = useRef<HTMLDivElement>(null);
  useLayerDismiss(
    historyOpen,
    () => setHistoryOpen(false),
    [historyList, historyButton],
    historyButton,
  );
  const open = (next: string) => {
    setPicked(next);
    onDiff(null);
    setHistoryOpen(false);
  };
  const startTask = useCallback<StartConversationTask>(
    (kind, request, requestId) =>
      onStartTask(
        kind,
        { ...request, chat_scope: "project", focus: "paper" },
        requestId,
        MAIN_GRAPH,
      ),
    [onStartTask],
  );
  const send = useCallback<SendThreads>(
    (ids, key) => {
      const sender = threadSender.current;
      return sender ? sender(ids, key) : Promise.reject(new Error("The Coach is not ready."));
    },
    [threadSender],
  );
  const turnTrail = (taskId: string) => {
    const task = tasks.find((item) => item.operation_id === taskId);
    if (task && isActiveTask(task)) return null;
    return (
      <PaperTurnChanges
        apiBase={apiBase}
        operationId={taskId}
        openPath={diff?.operationId === taskId ? diff.path : null}
        onOpen={(path: string) => onDiff({ chatId, operationId: taskId, path })}
      />
    );
  };
  return (
    <div className="paper-coach">
      <div className="paper-coach-body" data-diff={inlineDiff && diff ? "open" : undefined}>
        {panelTools(
          toolbar,
          <>
            <button
              ref={historyButton}
              className="icon-button compact"
              aria-label="Coach chat history"
              aria-expanded={historyOpen && chats.length > 0}
              disabled={!chats.length}
              onClick={() => setHistoryOpen(!historyOpen)}
            >
              <History size={15} />
            </button>
            <button
              className="icon-button compact"
              aria-label="New coach chat"
              onClick={() => open(crypto.randomUUID())}
            >
              <MessageCirclePlus size={15} />
            </button>
          </>,
        )}
        {historyOpen && chats.length > 0 && (
          <div className="paper-session-list" ref={historyList}>
            {chats.map((chat) => (
              <button
                key={chat.chat_id}
                aria-pressed={chat.chat_id === chatId}
                onClick={() => open(chat.chat_id)}
              >
                {chat.title || new Date(chat.updated_at).toLocaleString()}
              </button>
            ))}
          </div>
        )}
        <AgentChatView
          key={chatId}
          project={project}
          actorId={actions.actorId}
          graphTarget={MAIN_GRAPH}
          conversationTitle="Coach"
          tasks={tasks}
          historyMessages={transcript.value?.messages}
          agentSetup={summary?.agent_setup}
          chatId={chatId}
          presentation="workspace"
          profileName="paper_coach"
          fixedConversation
          onStartTask={startTask}
          onInspectTask={actions.onInspectTask}
          onOpenInbox={actions.onOpenInbox}
          onRepairGraphUpdate={actions.onRepairGraphUpdate}
          onResumeTask={actions.onResumeTask}
          onRetryTask={actions.onRetryTask}
          onRefreshTask={actions.onRefreshTask}
          onNewSession={() => open(crypto.randomUUID())}
          onClose={() => undefined}
          turnTrail={turnTrail}
          threadSender={threadSender}
        />
        {inlineDiff && diff && (
          <PaperTurnDiff
            key={`${diff.operationId}:${diff.path}`}
            projectId={project.id}
            apiBase={apiBase}
            chatId={chatId}
            operationId={diff.operationId}
            path={diff.path}
            send={send}
            canPost
            onClose={() => onDiff(null)}
          />
        )}
      </div>
    </div>
  );
}
