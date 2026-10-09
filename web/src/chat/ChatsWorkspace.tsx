import {
  ChevronDown,
  Circle,
  Ellipsis,
  Kanban,
  MessageCircle,
  PanelLeft,
  Pause,
  Pin,
  Search,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  AGENT_LIST_SECTIONS,
  CONVERSATION_AGENT_GROUPS,
  conversationAgentStatus,
  groupConversationAgents,
  type ChatConversation,
  type AgentListSection,
  type ConversationAgentRow,
  type ConversationAgentStatus,
  orchestratorCanUnlock,
} from "./chatWorkspace";
import {
  AGENT_BOARD_COLUMNS,
  agentBoardColumn,
  orderAgentBoardCards,
  readAgentBoardOrder,
  saveAgentBoardColumn,
  writeAgentBoardOrder,
  type AgentBoardColumn,
  type AgentBoardDrop,
} from "./agentBoardModel";
import type { GlossaryIndex } from "../graph/glossary";
import {
  CHAT_LIST_DEFAULT_WIDTH,
  CHAT_LIST_COLLAPSED_WIDTH,
  CHAT_LIST_DIVIDER_WIDTH,
  CHAT_LIST_MIN_WIDTH_COMPACT,
  chatListWidthBounds,
  clampChatListWidth,
  isChatListToggleShortcut,
  type ChatListWidthBounds,
} from "./chatLayout";
import type {
  AgentTask,
  ChatDisplay,
  ChatTranscript,
  GraphNode,
  GraphTargetRef,
  GraphUpdateRecovery,
  ProjectSnapshot,
  StartAgentTask,
  WatcherRecord,
} from "../core/types";
import { AgentBoard, type AgentBoardCard } from "./AgentBoard";
import { ProviderMark, hasProviderLogo } from "../projects/ProviderMark";
import { EpisodeAuthor } from "../experiments/EpisodeRunControls";
import { loadChatDisplay, setChatArchived, setChatPinned, setChatTitle } from "../core/api";
import { NodeChat } from "./NodeChat";
import { sameGraphTarget, MAIN_GRAPH, projectViewHash } from "../core/graphTarget";
import { useNarrowViewport } from "../ui/useNarrowViewport";

interface Props {
  project: ProjectSnapshot;
  /** The signed-in member whose persisted chat drafts this workspace shows. */
  actorId: string | null;
  conversations: ChatConversation[];
  selectedChatId: string | null;
  /** The board of every agent, shown on entry from the Agents tab; a card opens its chat. */
  board: boolean;
  onBoardChange: (board: boolean) => void;
  nodes: Record<string, GraphNode>;
  graphTarget: GraphTargetRef;
  glossaryIndex: GlossaryIndex;
  runScope: string[];
  tasks: AgentTask[];
  watchers: WatcherRecord[];
  graphChangesDisabled: boolean;
  unreadChatIds: ReadonlySet<string>;
  chatTranscripts: ReadonlyMap<string, ChatTranscript>;
  onSelect: (chatId: string) => void;
  onStartTask: StartAgentTask;
  onResumeTask: (task: AgentTask) => void;
  onRetryTask: (task: AgentTask) => void;
  onRefreshTask: (taskId: string) => Promise<AgentTask>;
  onInspectTask: (taskId: string) => void;
  onOpenInbox: () => void;
  onOpenNode?: (nodeId: string) => void;
  onRepairGraphUpdate: (taskId: string, action?: GraphUpdateRecovery) => Promise<void>;
  onStopWatcher?: (watcherId: string) => void;
  onNewSession: (conversation: ChatConversation) => void;
  onRemoveDraft: (chatId: string) => void;
  /** Archive moves a chat in or out of the unread count, which the server derives. */
  onArchiveChange?: () => void;
}

function chatListWidthStorageKey(projectId: string): string {
  return `rcp:chat-list-width:${projectId}`;
}

function chatListCollapsedStorageKey(projectId: string): string {
  return `rcp:chat-list-collapsed:${projectId}`;
}

function readChatListWidth(projectId: string): number {
  if (typeof window === "undefined") return CHAT_LIST_DEFAULT_WIDTH;
  try {
    const value = Number(window.localStorage.getItem(chatListWidthStorageKey(projectId)));
    return Number.isFinite(value) && value >= CHAT_LIST_MIN_WIDTH_COMPACT
      ? value
      : CHAT_LIST_DEFAULT_WIDTH;
  } catch {
    return CHAT_LIST_DEFAULT_WIDTH;
  }
}

function readChatListCollapsed(projectId: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(chatListCollapsedStorageKey(projectId)) === "true";
  } catch {
    return false;
  }
}

type AgentFilter = "all" | "working" | "archived";

const EMPTY_CHAT_DISPLAY: ChatDisplay = { archived: [], titles: {}, pinned: [] };
// The last display set per project survives tab switches, so archived chats
// never flash back into the list while a revisit reloads it.
const cachedDisplays = new Map<string, ChatDisplay>();

const GROUP_LABELS: Record<AgentListSection, string> = {
  pinned: "Pinned",
  new_reply: "New reply",
  failed: "Failed",
  stopped: "Stopped",
  working: "Working",
  done: "Done",
};

function needsHuman(status: ConversationAgentStatus): boolean {
  return status.state === "failed" || status.state === "stopped";
}

function AgentGroupIcon({ group }: { group: AgentListSection }) {
  const Icon = {
    pinned: Pin,
    new_reply: MessageCircle,
    failed: X,
    stopped: Pause,
    working: Circle,
    done: null,
  }[group];
  return Icon ? <Icon className="agent-group-icon" size={14} aria-hidden="true" /> : null;
}

function sinceLabel(timestamp: string | null | undefined, now: number): string {
  const at = timestamp ? Date.parse(timestamp) : Number.NaN;
  if (!Number.isFinite(at)) return "";
  const minutes = Math.max(0, Math.round((now - at) / 60_000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 24 * 60) return `${Math.round(minutes / 60)}h`;
  return new Date(at).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Everything after the provider, which renders as its own mark: model, effort,
 *  task type, then what the latest turn needs. */
function agentMeta(status: ConversationAgentStatus, kind: ChatConversation["kind"]): string {
  const latest = status.latest;
  if (!latest) return "";
  const parts: (string | null | undefined)[] = [
    latest.request.model,
    latest.request.reasoning,
    kind === "project_chat" ? "Project chat" : "Node chat",
  ];
  if (status.state === "working") {
    parts.push(latest.phase, `${Math.max(1, Math.round(latest.elapsed_seconds / 60))}m`);
  } else {
    if (status.state === "failed" && latest.can_retry) parts.push("Retry");
    if (status.state === "stopped") {
      if (latest.can_resume) parts.push("Resume");
      else if (latest.can_retry) parts.push("Retry");
    }
  }
  return parts.filter(Boolean).join(" · ");
}

/** A board card's provider line: provider, model, effort, task type. */
function agentCardMeta(
  status: ConversationAgentStatus,
  kind: ChatConversation["kind"],
  fallbackProvider: string | undefined,
): string {
  const latest = status.latest;
  return [
    latest?.provider_label ?? fallbackProvider,
    latest?.request.model,
    latest?.request.reasoning,
    kind === "project_chat" ? "Project chat" : "Node chat",
  ]
    .filter(Boolean)
    .join(" · ");
}

/** A board card's state line: what the agent is doing, or since when it is idle. */
function agentCardState(status: ConversationAgentStatus, since: string): string {
  const latest = status.latest;
  if (!latest) return "Draft";
  if (status.state === "working")
    return [latest.phase, `${Math.max(1, Math.round(latest.elapsed_seconds / 60))}m`]
      .filter(Boolean)
      .join(" · ");
  if (status.state === "failed") return "Failed";
  if (status.state === "stopped") return "Stopped";
  if (status.unread) return "New reply";
  return since;
}

function AgentRename({ title, onDone }: { title: string; onDone: (title: string | null) => void }) {
  return (
    <form
      className="agent-row-rename"
      onSubmit={(event) => {
        event.preventDefault();
        const value = new FormData(event.currentTarget).get("title");
        onDone(typeof value === "string" ? value.trim() : "");
      }}
    >
      {/* Blank returns the chat to its derived name. */}
      <input
        name="title"
        aria-label="Agent name"
        autoFocus
        defaultValue={title}
        onFocus={(event) => event.currentTarget.select()}
        onBlur={(event) => event.currentTarget.form?.requestSubmit()}
        onKeyDown={(event) => {
          if (event.key === "Escape") onDone(null);
        }}
      />
    </form>
  );
}

interface AgentMenuProps {
  title: string;
  open: boolean;
  draft: boolean;
  archived: boolean;
  pinned: boolean;
  working: boolean;
  onToggle: () => void;
  onRemove: () => void;
  onRename: () => void;
  onPin: () => void;
  onArchive: () => void;
}

function AgentMenu({
  title,
  open,
  draft,
  archived,
  pinned,
  working,
  onToggle,
  onRemove,
  onRename,
  onPin,
  onArchive,
}: AgentMenuProps) {
  return (
    <div className="agent-row-menu">
      <button
        type="button"
        className="agent-row-menu-button"
        aria-label={`More actions for ${title}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={onToggle}
      >
        <Ellipsis size={14} />
      </button>
      {open && (
        <div className="agent-row-menu-list" role="menu">
          {draft ? (
            <button type="button" role="menuitem" onClick={onRemove}>
              Remove
            </button>
          ) : (
            <>
              <button type="button" role="menuitem" onClick={onRename}>
                Rename
              </button>
              {!archived && (
                <button type="button" role="menuitem" onClick={onPin}>
                  {pinned ? "Unpin" : "Pin"}
                </button>
              )}
              <button
                type="button"
                role="menuitem"
                disabled={!archived && working}
                title={!archived && working ? "Archive after this agent finishes" : undefined}
                onClick={onArchive}
              >
                {archived ? "Restore" : "Archive"}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

type BoardCard = AgentBoardCard & {
  conversation: ChatConversation;
  status: ConversationAgentStatus;
};

function ConversationTags({ conversation }: { conversation: ChatConversation }) {
  const kind = conversation.conversationKind ?? "chat";
  return (
    <span className="agent-conversation-tags">
      <span data-graph-target={conversation.graphTarget?.branch_id ?? "main"}>
        {conversation.graphTitle ?? conversation.graphTarget?.branch_id ?? "Main"}
      </span>
      <span data-conversation-kind={kind}>
        {kind === "auto_research_child"
          ? "Auto-research child"
          : kind === "episode"
            ? "Episode"
            : "Chat"}
      </span>
    </span>
  );
}

const BOARD_LABELS: Record<AgentBoardColumn, string> = {
  needs_you: "Needs you",
  working: "Working",
  done: "Done",
  archived: "Archived",
};

export function ChatsWorkspace({
  project,
  actorId,
  conversations: storedConversations,
  selectedChatId,
  board,
  onBoardChange,
  nodes,
  graphTarget,
  glossaryIndex,
  runScope,
  tasks,
  watchers,
  graphChangesDisabled,
  unreadChatIds,
  chatTranscripts,
  onArchiveChange,
  onSelect,
  onStartTask,
  onResumeTask,
  onRetryTask,
  onRefreshTask,
  onInspectTask,
  onOpenInbox,
  onOpenNode,
  onRepairGraphUpdate,
  onStopWatcher,
  onNewSession,
  onRemoveDraft,
}: Props) {
  const narrow = useNarrowViewport();
  const [mobileListOpen, setMobileListOpen] = useState(false);
  const [filter, setFilter] = useState<AgentFilter>("all");
  const [query, setQuery] = useState("");
  const [graphFilter, setGraphFilter] = useState("all");
  const [listWidth, setListWidth] = useState(() => readChatListWidth(project.id));
  const [listCollapsed, setListCollapsed] = useState(() => readChatListCollapsed(project.id));
  const [widthBounds, setWidthBounds] = useState<ChatListWidthBounds>(() =>
    chatListWidthBounds(typeof window === "undefined" ? 1200 : window.innerWidth),
  );
  const workspace = useRef<HTMLElement>(null);
  const apiBase = `/api/projects/${encodeURIComponent(project.id)}`;
  const [display, setDisplayState] = useState<ChatDisplay>(
    () => cachedDisplays.get(apiBase) ?? EMPTY_CHAT_DISPLAY,
  );
  const setDisplay = (next: ChatDisplay) => {
    cachedDisplays.set(apiBase, next);
    setDisplayState(next);
  };
  const archivedChatIds = useMemo(() => new Set(display.archived), [display.archived]);
  // A human-given name replaces the derived one everywhere in this workspace.
  const conversations = useMemo(
    () =>
      storedConversations.map((conversation) =>
        display.titles[conversation.chatId]
          ? { ...conversation, title: display.titles[conversation.chatId] }
          : conversation,
      ),
    [storedConversations, display.titles],
  );
  const graphOptions = new Map<string, string>([["main", "Main"]]);
  for (const conversation of conversations) {
    const branchId = conversation.graphTarget?.branch_id;
    if (branchId) graphOptions.set(branchId, conversation.graphTitle ?? branchId);
  }
  const filteredConversations = conversations.filter(
    (conversation) =>
      graphFilter === "all" || (conversation.graphTarget?.branch_id ?? "main") === graphFilter,
  );
  const graphFilterControl = (
    <select
      className="agent-graph-filter"
      aria-label="Filter agents by graph"
      value={graphFilter}
      onChange={(event) => setGraphFilter(event.target.value)}
    >
      <option value="all">All</option>
      {[...graphOptions].map(([id, title]) => (
        <option key={id} value={id}>
          {title}
        </option>
      ))}
    </select>
  );
  const [menuChatId, setMenuChatId] = useState<string | null>(null);
  const [renamingChatId, setRenamingChatId] = useState<string | null>(null);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  const showingArchived = filter === "archived";
  const listed = filteredConversations.filter(
    (conversation) => archivedChatIds.has(conversation.chatId) === showingArchived,
  );
  // The inventory is complete before a graph filter is applied.
  const archivedCount = filteredConversations.filter((conversation) =>
    archivedChatIds.has(conversation.chatId),
  ).length;
  // Pins lead only the All view; a filter shows each pinned chat in its status group.
  const groups = groupConversationAgents(
    listed,
    unreadChatIds,
    query,
    filter === "all" ? display.pinned : [],
  );
  // Filter counts ignore pins, so Working counts every working agent.
  const activeGroups = groupConversationAgents(
    filteredConversations.filter((conversation) => !archivedChatIds.has(conversation.chatId)),
    unreadChatIds,
    query,
  );
  const pinnedChatIds = new Set(display.pinned);
  const visibleGroups = AGENT_LIST_SECTIONS.filter(
    (group) => filter === "all" || showingArchived || group === filter,
  );
  const now = Date.now();
  const selected =
    conversations.find((conversation) => conversation.chatId === selectedChatId) ?? null;

  // Every read or edit of the display set returns the whole set, so only the
  // latest request's answer may apply; a project switch starts a new request.
  const displayRequest = useRef(0);
  useEffect(() => {
    let current = true;
    const request = ++displayRequest.current;
    setDisplayState(cachedDisplays.get(apiBase) ?? EMPTY_CHAT_DISPLAY);
    loadChatDisplay(apiBase)
      .then((response) => {
        if (current && displayRequest.current === request) setDisplay(response);
      })
      .catch(() => {
        // Archive and names are display choices; an unreadable set shows every
        // conversation under its derived name.
      });
    return () => {
      current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- setDisplay is recreated every render and only closes over apiBase
  }, [apiBase]);

  useEffect(() => {
    if (menuChatId === null) return;
    const close = (event: Event) => {
      if (event instanceof KeyboardEvent && event.key !== "Escape") return;
      if (event.target instanceof Element && event.target.closest(".agent-row-menu")) return;
      setMenuChatId(null);
    };
    window.addEventListener("pointerdown", close);
    window.addEventListener("keydown", close);
    return () => {
      window.removeEventListener("pointerdown", close);
      window.removeEventListener("keydown", close);
    };
  }, [menuChatId]);

  const updateDisplay = async (change: () => Promise<ChatDisplay>) => {
    const request = ++displayRequest.current;
    setMenuChatId(null);
    setArchiveError(null);
    try {
      const response = await change();
      if (displayRequest.current === request) setDisplay(response);
    } catch (failure) {
      if (displayRequest.current === request)
        setArchiveError(failure instanceof Error ? failure.message : String(failure));
    }
  };
  const archive = (chatId: string, archived: boolean) => {
    // An archived chat leaves the open list, so it must not stay selected there
    // where a new turn would run out of sight.
    const next = conversations.find(
      (item) => item.chatId !== chatId && !archivedChatIds.has(item.chatId),
    );
    if (archived && !showingArchived && selected?.chatId === chatId && next) onSelect(next.chatId);
    return updateDisplay(() => setChatArchived(apiBase, chatId, archived)).then(() =>
      onArchiveChange?.(),
    );
  };
  const pin = (chatId: string, pinned: boolean) =>
    updateDisplay(() => setChatPinned(apiBase, chatId, pinned));
  const rename = (chatId: string, title: string) => {
    setRenamingChatId(null);
    return updateDisplay(() => setChatTitle(apiBase, chatId, title));
  };
  const finishRename = (conversation: ChatConversation, title: string | null) => {
    if (title === null || title === conversation.title) setRenamingChatId(null);
    else void rename(conversation.chatId, title);
  };
  const removeDraft = (chatId: string) => {
    setMenuChatId(null);
    onRemoveDraft(chatId);
    // Move the selection off the removed draft so the shown chat is the one that loads.
    const next = conversations.find((item) => item.chatId !== chatId);
    if (selected?.chatId === chatId && next) onSelect(next.chatId);
  };
  const agentMenu = (
    conversation: ChatConversation,
    status: ConversationAgentStatus,
    archived: boolean,
  ) => (
    <AgentMenu
      title={conversation.title}
      open={menuChatId === conversation.chatId}
      draft={conversation.tasks.length === 0 && !conversation.updatedAt}
      archived={archived}
      pinned={pinnedChatIds.has(conversation.chatId)}
      working={status.state === "working"}
      onToggle={() =>
        setMenuChatId(menuChatId === conversation.chatId ? null : conversation.chatId)
      }
      onRemove={() => removeDraft(conversation.chatId)}
      onRename={() => {
        setMenuChatId(null);
        setRenamingChatId(conversation.chatId);
      }}
      onPin={() => void pin(conversation.chatId, !pinnedChatIds.has(conversation.chatId))}
      onArchive={() => void archive(conversation.chatId, !archived)}
    />
  );

  const [boardOrder, setBoardOrder] = useState(() => readAgentBoardOrder(project.id));
  useEffect(() => setBoardOrder(readAgentBoardOrder(project.id)), [project.id]);
  // Built only while the board shows; the list view never reads it.
  const boardColumns = !board
    ? null
    : (() => {
        const columns: Record<AgentBoardColumn, BoardCard[]> = {
          needs_you: [],
          working: [],
          done: [],
          archived: [],
        };
        const chatCard = (row: ConversationAgentRow, column: AgentBoardColumn): BoardCard => ({
          id: row.conversation.chatId,
          title: row.conversation.title,
          column,
          working: row.status.state === "working",
          conversation: row.conversation,
          status: row.status,
        });
        for (const group of CONVERSATION_AGENT_GROUPS) {
          const column = agentBoardColumn(group);
          for (const row of activeGroups[group]) columns[column].push(chatCard(row, column));
        }
        const archivedGroups = groupConversationAgents(
          filteredConversations.filter((conversation) => archivedChatIds.has(conversation.chatId)),
          unreadChatIds,
          query,
        );
        for (const group of CONVERSATION_AGENT_GROUPS)
          for (const row of archivedGroups[group]) columns.archived.push(chatCard(row, "archived"));
        for (const column of AGENT_BOARD_COLUMNS)
          columns[column] = orderAgentBoardCards(
            columns[column],
            (card) => card.id,
            boardOrder,
            column === "archived" ? [] : display.pinned,
          );
        return columns;
      })();
  const focusComposer = useRef(false);
  const openCard = (card: BoardCard) => {
    onSelect(card.id);
    // Cross-target selection enters through the route after its workspace loads.
    if (!sameGraphTarget(card.conversation.graphTarget, graphTarget)) return;
    // The board already did the list's job, so the chat opens with the list folded.
    setListCollapsed(true);
    focusComposer.current = true;
    onBoardChange(false);
  };
  useEffect(() => {
    if (board || !focusComposer.current) return;
    focusComposer.current = false;
    const frame = window.requestAnimationFrame(() =>
      workspace.current
        ?.querySelector<HTMLTextAreaElement>('.conversation-surface textarea[aria-label="Message"]')
        ?.focus(),
    );
    return () => window.cancelAnimationFrame(frame);
  }, [board, selected?.chatId]);
  const dropCard = (card: BoardCard, drop: AgentBoardDrop, columnIds: string[]) => {
    const ordered = saveAgentBoardColumn(boardOrder, columnIds);
    setBoardOrder(ordered);
    writeAgentBoardOrder(project.id, ordered);
    if (drop === "archive") void archive(card.id, true);
    if (drop === "restore") void archive(card.id, false);
  };

  useEffect(() => {
    setListWidth(readChatListWidth(project.id));
    setListCollapsed(readChatListCollapsed(project.id));
    setQuery("");
    setFilter("all");
    setGraphFilter("all");
  }, [project.id]);

  useEffect(() => {
    if (!project.id) return;
    try {
      window.localStorage.setItem(chatListWidthStorageKey(project.id), String(listWidth));
    } catch {
      // Layout state is a convenience; storage failures must not affect chat.
    }
  }, [listWidth, project.id]);

  useEffect(() => {
    if (!project.id) return;
    try {
      window.localStorage.setItem(chatListCollapsedStorageKey(project.id), String(listCollapsed));
    } catch {
      // Layout state is a convenience; storage failures must not affect chat.
    }
  }, [listCollapsed, project.id]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!isChatListToggleShortcut(event)) return;
      event.preventDefault();
      if (narrow) setMobileListOpen((current) => !current);
      else setListCollapsed((current) => !current);
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [narrow]);

  useEffect(() => {
    const element = workspace.current;
    if (!element || narrow) return;

    const updateBounds = (width: number) => {
      const nextBounds = chatListWidthBounds(width);
      setWidthBounds(nextBounds);
      setListWidth((current) => clampChatListWidth(current, nextBounds));
    };
    updateBounds(element.getBoundingClientRect().width);
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) updateBounds(entry.contentRect.width);
    });
    observer.observe(element);
    return () => observer.disconnect();
    // The board and the chat view are different elements behind one ref.
  }, [project.id, narrow, board]);

  const selectedStatus = selected ? conversationAgentStatus(selected, unreadChatIds) : null;
  const selectedLatest = selectedStatus?.latest ?? null;
  // An Auto-research child lives on its orchestrator's branch; its links stay there.
  const orchestratorHref = projectViewHash(
    project.id,
    selected?.graphTarget ?? MAIN_GRAPH,
    "execution",
    selected?.orchestratorEpisodeId
      ? { autoResearchEpisodeId: selected.orchestratorEpisodeId }
      : {},
  );
  const resizeFromPointer = (clientX: number) => {
    const bounds = workspace.current?.getBoundingClientRect();
    if (!bounds) return;
    setListWidth(clampChatListWidth(clientX - bounds.left, widthBounds));
  };

  const conversationHeading =
    selected && selectedStatus ? (
      <>
        <strong>{selected.title}</strong>
        <span className="conversation-header-meta">
          {[
            selectedLatest?.provider_label ??
              project.providers?.[project.agent_profiles?.[selected.kind]?.provider]?.label,
            selectedLatest?.request.model,
            selectedLatest?.request.reasoning,
            selectedLatest?.request.run_truth_scope?.join(", "),
            selected.kind === "project_chat" ? "Project chat" : "Node chat",
          ]
            .filter(Boolean)
            .join(" · ")}
        </span>
        {selected.conversationKind === "auto_research_child" && (
          <span className="conversation-header-meta">
            Managed by the orchestrator in <a href={orchestratorHref}>Runs</a>.
          </span>
        )}
        {!selected.humanTurnRefusal && needsHuman(selectedStatus) && selectedLatest && (
          <div className="conversation-header-banner" role="status">
            <span>{selectedLatest.status_label}</span>
            {selectedLatest.can_resume && (
              <button
                className="button compact"
                type="button"
                onClick={() => onResumeTask(selectedLatest)}
              >
                Resume
              </button>
            )}
            {!selectedLatest.can_resume && selectedLatest.can_retry && (
              <button
                className="button compact"
                type="button"
                onClick={() => onRetryTask(selectedLatest)}
              >
                Retry
              </button>
            )}
            {!selectedLatest.can_resume &&
              !selectedLatest.can_retry &&
              selected.conversationKind === "auto_research_child" && (
                // Only the orchestrator recovers its own turn: message it or add turns.
                <a className="button compact" href={orchestratorHref}>
                  Message orchestrator
                </a>
              )}
          </div>
        )}
      </>
    ) : null;

  const renderBoardCard = (card: BoardCard) => {
    const { conversation, status } = card;
    const latest = status.latest;
    const provider =
      latest?.request.provider ?? project.agent_profiles?.[conversation.kind]?.provider ?? "";
    const providerLabel =
      latest?.provider_label ?? project.providers?.[provider]?.label ?? provider;
    const renaming = renamingChatId === conversation.chatId;
    const action =
      !conversation.humanTurnRefusal && needsHuman(status) && latest
        ? latest.can_resume
          ? { label: "Resume", run: () => onResumeTask(latest) }
          : latest.can_retry
            ? { label: "Retry", run: () => onRetryTask(latest) }
            : null
        : null;
    return (
      <>
        <div className="agent-card-top">
          <span
            className="agent-card-avatar"
            data-working={card.working || undefined}
            aria-hidden="true"
          >
            {hasProviderLogo(provider) ? (
              <ProviderMark provider={provider} label={providerLabel} />
            ) : (
              providerLabel.slice(0, 1).toUpperCase()
            )}
          </span>
          {renaming ? (
            <AgentRename
              title={conversation.title}
              onDone={(title) => finishRename(conversation, title)}
            />
          ) : (
            <strong className="agent-card-title">
              {pinnedChatIds.has(conversation.chatId) && card.column !== "archived" && (
                <Pin className="agent-group-icon" size={12} aria-label="Pinned" />
              )}
              {status.unread && <span className="agent-new-pill">New</span>}
              {conversation.title}
            </strong>
          )}
          {!renaming && agentMenu(conversation, status, card.column === "archived")}
        </div>
        <ConversationTags conversation={conversation} />
        <span className="agent-card-meta">
          {agentCardMeta(status, conversation.kind, providerLabel)}
        </span>
        <div className="agent-card-foot">
          <span className="agent-card-state" data-state={status.state}>
            {agentCardState(
              status,
              sinceLabel(latest?.last_activity_at ?? conversation.updatedAt ?? null, now),
            )}
          </span>
          <EpisodeAuthor author={latest?.authorized_by ?? null} />
          {action && (
            <button className="button compact" type="button" onClick={action.run}>
              {action.label}
            </button>
          )}
        </div>
      </>
    );
  };

  if (boardColumns)
    return (
      <section className="agents-board-view" ref={workspace} aria-label="Agents board">
        <header className="agents-board-head">
          <h2>Agents</h2>
          <label className="agent-list-search">
            <Search size={14} aria-hidden="true" />
            <input
              type="search"
              aria-label="Search agents"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          {graphFilterControl}
        </header>
        {archiveError && (
          <p className="agent-list-error" role="alert">
            {archiveError}
          </p>
        )}
        <AgentBoard
          columns={boardColumns}
          labels={BOARD_LABELS}
          renderCard={renderBoardCard}
          onOpen={openCard}
          onDrop={dropCard}
        />
      </section>
    );

  return (
    <section
      className="chats-workspace"
      ref={workspace}
      style={{
        gridTemplateColumns: narrow
          ? undefined
          : listCollapsed
            ? `${CHAT_LIST_COLLAPSED_WIDTH}px 0px minmax(0, 1fr)`
            : `${listWidth}px ${CHAT_LIST_DIVIDER_WIDTH}px minmax(0, 1fr)`,
      }}
    >
      <button
        className="conversation-list-toggle view-disclosure-summary"
        type="button"
        aria-controls="conversation-list-panel"
        aria-expanded={mobileListOpen}
        onClick={() => setMobileListOpen((current) => !current)}
      >
        <MessageCircle size={14} />
        <span>Agents</span>
        <ChevronDown size={14} />
      </button>
      <aside
        className="conversation-list"
        aria-label="Project conversations"
        hidden={narrow ? !mobileListOpen : listCollapsed}
        id="conversation-list-panel"
      >
        <div className="agent-list-tools">
          <div className="agent-list-top">
            <button
              aria-controls="conversation-list-panel"
              aria-expanded
              aria-keyshortcuts="Meta+B"
              aria-label="Collapse conversation list"
              className="conversation-list-fold"
              onClick={() => setListCollapsed(true)}
              title="Collapse conversation list"
              type="button"
            >
              <PanelLeft size={16} />
            </button>
            <button
              aria-label="Show the agents board"
              className="conversation-list-fold"
              onClick={() => onBoardChange(true)}
              title="Show the agents board"
              type="button"
            >
              <Kanban size={16} />
            </button>
            <label className="agent-list-search">
              <Search size={14} aria-hidden="true" />
              <input
                type="search"
                aria-label="Search agents"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            {graphFilterControl}
          </div>
          <div className="agent-list-filters" role="group" aria-label="Filter agents">
            {(archivedCount > 0 || showingArchived
              ? (["all", "working", "archived"] as const)
              : (["all", "working"] as const)
            ).map((value) => {
              const count =
                value === "archived"
                  ? archivedCount
                  : value === "all"
                    ? Object.values(activeGroups).reduce((total, rows) => total + rows.length, 0)
                    : activeGroups[value].length;
              return (
                <button
                  type="button"
                  key={value}
                  data-filter={value}
                  aria-pressed={filter === value}
                  onClick={() => setFilter(value)}
                >
                  {value === "all"
                    ? "All"
                    : value === "archived"
                      ? "Archived"
                      : GROUP_LABELS[value]}{" "}
                  <span>{count}</span>
                </button>
              );
            })}
          </div>
        </div>
        {archiveError && (
          <p className="agent-list-error" role="alert">
            {archiveError}
          </p>
        )}
        <div role="listbox" aria-label="Conversations">
          {visibleGroups.map((group) =>
            groups[group].length === 0 ? null : (
              <div
                className="agent-group"
                role="group"
                aria-label={GROUP_LABELS[group]}
                data-group={group}
                key={group}
              >
                <div className="agent-group-heading" aria-hidden="true">
                  <span className="agent-group-label">
                    <AgentGroupIcon group={group} />
                    {GROUP_LABELS[group]}
                  </span>
                  <span>{groups[group].length}</span>
                </div>
                {groups[group].map(({ conversation, status }) => {
                  const selectedConversation = conversation.chatId === selected?.chatId;
                  const unread = status.unread;
                  const latest = status.latest;
                  const archived = archivedChatIds.has(conversation.chatId);
                  const menuOpen = menuChatId === conversation.chatId;
                  return (
                    <div
                      className={`agent-row${menuOpen ? " menu-open" : ""}`}
                      key={conversation.chatId}
                    >
                      {renamingChatId === conversation.chatId ? (
                        <AgentRename
                          title={conversation.title}
                          onDone={(title) => finishRename(conversation, title)}
                        />
                      ) : (
                        <button
                          type="button"
                          role="option"
                          aria-selected={selectedConversation}
                          aria-current={selectedConversation ? "page" : undefined}
                          aria-label={`${conversation.title}, ${conversation.kind === "project_chat" ? "project" : "node"} conversation${unread ? ", unread result" : ""}`}
                          className={`${selectedConversation ? "active" : ""}${unread ? " unread" : ""}`}
                          data-state={status.state}
                          title={conversation.title}
                          onClick={() => {
                            onSelect(conversation.chatId);
                            if (narrow) setMobileListOpen(false);
                          }}
                        >
                          <span className="agent-row-body">
                            <span className="agent-row-title">
                              {/* A pinned row sits outside its status group, so it carries the mark. */}
                              {group === "pinned" && <AgentGroupIcon group={status.group} />}
                              {unread && <span className="agent-new-pill">New</span>}
                              {conversation.title}
                            </span>
                            <ConversationTags conversation={conversation} />
                            <span className="agent-row-meta">
                              {latest ? (
                                <>
                                  <ProviderMark
                                    provider={latest.request.provider ?? ""}
                                    label={latest.provider_label}
                                  />
                                  {agentMeta(status, conversation.kind) &&
                                    `${latest.provider_label || latest.request.provider ? " · " : ""}${agentMeta(status, conversation.kind)}`}
                                </>
                              ) : (
                                "\u00a0"
                              )}
                            </span>
                          </span>
                          <time>
                            {status.state === "working"
                              ? "live"
                              : sinceLabel(
                                  latest?.last_activity_at ?? conversation.updatedAt ?? null,
                                  now,
                                )}
                          </time>
                        </button>
                      )}
                      {renamingChatId !== conversation.chatId &&
                        agentMenu(conversation, status, archived)}
                    </div>
                  );
                })}
              </div>
            ),
          )}
        </div>
      </aside>
      <div className="conversation-divider" hidden={listCollapsed}>
        <div
          aria-controls="conversation-list-panel conversation-surface-panel"
          aria-label="Resize conversation list"
          aria-orientation="vertical"
          aria-valuemax={widthBounds.maximum}
          aria-valuemin={widthBounds.minimum}
          aria-valuenow={Math.round(listWidth)}
          className="conversation-resize-handle"
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
              event.preventDefault();
              setListWidth((current) =>
                clampChatListWidth(current + (event.key === "ArrowLeft" ? -16 : 16), widthBounds),
              );
            }
            if (event.key === "Home" || event.key === "End") {
              event.preventDefault();
              setListWidth(event.key === "Home" ? widthBounds.minimum : widthBounds.maximum);
            }
          }}
          onPointerCancel={(event) => {
            if (event.currentTarget.hasPointerCapture(event.pointerId)) {
              event.currentTarget.releasePointerCapture(event.pointerId);
            }
          }}
          onPointerDown={(event) => {
            event.currentTarget.setPointerCapture(event.pointerId);
            resizeFromPointer(event.clientX);
          }}
          onPointerMove={(event) => {
            if (event.currentTarget.hasPointerCapture(event.pointerId)) {
              resizeFromPointer(event.clientX);
            }
          }}
          onPointerUp={(event) => {
            if (event.currentTarget.hasPointerCapture(event.pointerId)) {
              event.currentTarget.releasePointerCapture(event.pointerId);
            }
          }}
          role="separator"
          tabIndex={0}
        />
      </div>
      <div className="conversation-surface" id="conversation-surface-panel">
        {!narrow && listCollapsed && (
          <button
            aria-controls="conversation-list-panel"
            aria-expanded={false}
            aria-keyshortcuts="Meta+B"
            aria-label="Expand conversation list"
            className="conversation-list-fold is-floating"
            onClick={() => setListCollapsed(false)}
            title="Expand conversation list"
            type="button"
          >
            <PanelLeft size={16} />
          </button>
        )}
        {selected && sameGraphTarget(selected.graphTarget ?? MAIN_GRAPH, graphTarget) ? (
          <NodeChat
            key={selected.chatId}
            project={project}
            actorId={actorId}
            graphTarget={graphTarget}
            node={selected.nodeId ? (nodes[selected.nodeId] ?? null) : null}
            nodes={nodes}
            glossaryIndex={glossaryIndex}
            conversationTitle={selected.kind === "node_chat" ? selected.title : undefined}
            header={conversationHeading}
            headerState={selectedStatus?.state}
            runScope={runScope}
            tasks={tasks}
            watchers={watchers}
            historyMessages={chatTranscripts.get(selected.chatId)?.messages}
            chatId={selected.chatId}
            presentation="workspace"
            readOnly={Boolean(selected.humanTurnRefusal)}
            allowArtifactComments={selected.conversationKind === "auto_research_child"}
            readOnlyNotice={
              selected.humanTurnRefusal ? (
                <div className="chat-composer" role="status">
                  <p>{selected.humanTurnRefusal.detail}</p>
                  {orchestratorCanUnlock(selected.humanTurnRefusal) && (
                    <a className="button compact" href={orchestratorHref}>
                      Message orchestrator
                    </a>
                  )}
                </div>
              ) : undefined
            }
            graphChangesDisabled={graphChangesDisabled}
            onStartTask={onStartTask}
            onResumeTask={onResumeTask}
            onRetryTask={onRetryTask}
            onRefreshTask={onRefreshTask}
            onInspectTask={onInspectTask}
            onOpenInbox={onOpenInbox}
            onRepairGraphUpdate={onRepairGraphUpdate}
            onOpenNode={onOpenNode}
            onStopWatcher={onStopWatcher}
            onNewSession={() => onNewSession(selected)}
            onClose={() => undefined}
          />
        ) : null}
      </div>
    </section>
  );
}
