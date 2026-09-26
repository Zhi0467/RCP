import {
  AlertCircle,
  CheckCircle2,
  CircleDashed,
  CircleX,
  Ellipsis,
  ChevronDown,
  LoaderCircle,
  MessageCircle,
  PanelLeftClose,
  PanelLeftOpen,
  PauseCircle,
  Search,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import {
  CONVERSATION_AGENT_GROUPS,
  conversationAgentStatus,
  groupConversationAgents,
  type ChatConversation,
  type ConversationAgentGroup,
  type ConversationAgentState,
  type ConversationAgentStatus,
} from "../chatWorkspace";
import type { GlossaryIndex } from "../glossary";
import {
  CHAT_LIST_DEFAULT_WIDTH,
  CHAT_LIST_COLLAPSED_WIDTH,
  CHAT_LIST_DIVIDER_WIDTH,
  CHAT_LIST_MIN_WIDTH_COMPACT,
  chatListWidthBounds,
  clampChatListWidth,
  isChatListToggleShortcut,
  type ChatListWidthBounds,
} from "../chatLayout";
import type {
  AgentTask,
  ChatTranscript,
  GraphNode,
  ProjectSnapshot,
  StartAgentTask,
  WatcherRecord,
} from "../types";
import { loadArchivedChats, setChatArchived } from "../api";
import { NodeChat } from "../components/NodeChat";
import { useNarrowViewport } from "../hooks/useNarrowViewport";

interface Props {
  project: ProjectSnapshot;
  conversations: ChatConversation[];
  selectedChatId: string | null;
  nodes: Record<string, GraphNode>;
  glossaryIndex: GlossaryIndex;
  runScope: string[];
  tasks: AgentTask[];
  watchers: WatcherRecord[];
  graphChangesDisabled: boolean;
  unreadTaskIds: ReadonlySet<string>;
  chatTranscripts: ReadonlyMap<string, ChatTranscript>;
  hasMore: boolean;
  loadingMore: boolean;
  onSelect: (chatId: string) => void;
  onLoadMore: () => void;
  onStartTask: StartAgentTask;
  onResumeTask: (task: AgentTask) => void;
  onRetryTask: (task: AgentTask) => void;
  onRefreshTask: (taskId: string) => Promise<AgentTask>;
  onInspectTask: (taskId: string) => void;
  onOpenInbox: () => void;
  onOpenNode?: (nodeId: string) => void;
  onRepairGraphUpdate: (taskId: string) => Promise<void>;
  onStopWatcher?: (watcherId: string) => void;
  onNewSession: (conversation: ChatConversation) => void;
  onRemoveDraft: (chatId: string) => void;
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

type AgentFilter = "all" | "needs_you" | "working" | "archived";

const GROUP_LABELS: Record<ConversationAgentGroup, string> = {
  needs_you: "Needs you",
  working: "Working",
  recent: "Recent",
};

/** A needs-you or paused row shows the backend's status label as its reason. */
function needsHuman(status: ConversationAgentStatus): boolean {
  return status.group === "needs_you";
}

function AgentStateIcon({ state }: { state: ConversationAgentState }) {
  if (state === "needs_you") return <AlertCircle size={15} aria-hidden="true" />;
  if (state === "paused") return <PauseCircle size={15} aria-hidden="true" />;
  if (state === "working") return <LoaderCircle className="spin" size={15} aria-hidden="true" />;
  if (state === "unread" || state === "done") return <CheckCircle2 size={15} aria-hidden="true" />;
  if (state === "failed") return <CircleX size={15} aria-hidden="true" />;
  return <CircleDashed size={15} aria-hidden="true" />;
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

function modeLabel(status: ConversationAgentStatus): string | null {
  const mode = status.latest?.request.mode;
  return mode === "work" ? "Work" : mode === "discuss" ? "Discuss" : null;
}

function agentMeta(status: ConversationAgentStatus): string {
  const latest = status.latest;
  if (!latest) return "";
  const parts: (string | null | undefined)[] = [latest.provider_label, modeLabel(status)];
  if (status.state === "working") {
    parts.push(latest.phase, `${Math.max(1, Math.round(latest.elapsed_seconds / 60))}m`);
  } else {
    parts.push(latest.request.run_truth_scope?.join(", "));
  }
  return parts.filter(Boolean).join(" · ");
}

export function ChatsWorkspace({
  project,
  conversations,
  selectedChatId,
  nodes,
  glossaryIndex,
  runScope,
  tasks,
  watchers,
  graphChangesDisabled,
  unreadTaskIds,
  chatTranscripts,
  hasMore,
  loadingMore,
  onSelect,
  onLoadMore,
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
  const [listWidth, setListWidth] = useState(() => readChatListWidth(project.id));
  const [listCollapsed, setListCollapsed] = useState(() => readChatListCollapsed(project.id));
  const [widthBounds, setWidthBounds] = useState<ChatListWidthBounds>(() =>
    chatListWidthBounds(typeof window === "undefined" ? 1200 : window.innerWidth),
  );
  const workspace = useRef<HTMLElement>(null);
  const apiBase = `/api/projects/${encodeURIComponent(project.id)}`;
  const [archivedChatIds, setArchivedChatIds] = useState<ReadonlySet<string>>(new Set());
  const [menuChatId, setMenuChatId] = useState<string | null>(null);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  const showingArchived = filter === "archived";
  const listed = conversations.filter(
    (conversation) => archivedChatIds.has(conversation.chatId) === showingArchived,
  );
  const archivedCount = conversations.filter((conversation) =>
    archivedChatIds.has(conversation.chatId),
  ).length;
  const groups = groupConversationAgents(listed, unreadTaskIds, query);
  const activeGroups = showingArchived
    ? groupConversationAgents(
        conversations.filter((conversation) => !archivedChatIds.has(conversation.chatId)),
        unreadTaskIds,
        query,
      )
    : groups;
  const visibleGroups = CONVERSATION_AGENT_GROUPS.filter(
    (group) => filter === "all" || showingArchived || group === filter,
  );
  const now = Date.now();
  const selected =
    conversations.find((conversation) => conversation.chatId === selectedChatId) ??
    conversations[0] ??
    null;

  useEffect(() => {
    let current = true;
    setArchivedChatIds(new Set());
    loadArchivedChats(apiBase)
      .then((response) => {
        if (current) setArchivedChatIds(new Set(response.chat_ids));
      })
      .catch(() => {
        // Archive only hides rows; an unreadable list shows every conversation.
      });
    return () => {
      current = false;
    };
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

  const archive = async (chatId: string, archived: boolean) => {
    setMenuChatId(null);
    setArchiveError(null);
    try {
      const response = await setChatArchived(apiBase, chatId, archived);
      setArchivedChatIds(new Set(response.chat_ids));
    } catch (failure) {
      setArchiveError(failure instanceof Error ? failure.message : String(failure));
    }
  };

  useEffect(() => {
    setListWidth(readChatListWidth(project.id));
    setListCollapsed(readChatListCollapsed(project.id));
    setQuery("");
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
  }, [project.id, narrow]);

  const selectedStatus = selected ? conversationAgentStatus(selected, unreadTaskIds) : null;
  const selectedLatest = selectedStatus?.latest ?? null;
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
            modeLabel(selectedStatus),
            selectedLatest?.request.run_truth_scope?.join(", "),
            selected.kind === "project_chat" ? "Project chat" : "Node chat",
          ]
            .filter(Boolean)
            .join(" · ")}
        </span>
        {needsHuman(selectedStatus) && selectedLatest && (
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
          </div>
        )}
      </>
    ) : null;

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
              <PanelLeftClose size={15} />
            </button>
            <label className="agent-list-search">
              <Search size={13} aria-hidden="true" />
              <input
                type="search"
                aria-label="Search agents"
                placeholder="Search agents"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
          </div>
          <div className="agent-list-filters" role="group" aria-label="Filter agents">
            {(archivedCount > 0 || showingArchived
              ? (["all", "needs_you", "working", "archived"] as const)
              : (["all", "needs_you", "working"] as const)
            ).map((value) => {
              const count =
                value === "archived"
                  ? archivedCount
                  : value === "all"
                    ? activeGroups.needs_you.length +
                      activeGroups.working.length +
                      activeGroups.recent.length
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
                  <span>{GROUP_LABELS[group]}</span>
                  <span>{groups[group].length}</span>
                </div>
                {groups[group].map(({ conversation, status }) => {
                  const selectedConversation = conversation.chatId === selected?.chatId;
                  const unread = status.state === "unread";
                  const latest = status.latest;
                  const draft = conversation.tasks.length === 0 && !conversation.updatedAt;
                  const archived = archivedChatIds.has(conversation.chatId);
                  const menuOpen = menuChatId === conversation.chatId;
                  return (
                    <div
                      className={`agent-row${menuOpen ? " menu-open" : ""}`}
                      key={conversation.chatId}
                    >
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
                        <span className="agent-row-icon">
                          <AgentStateIcon state={status.state} />
                        </span>
                        <span className="agent-row-body">
                          <span className="agent-row-title">{conversation.title}</span>
                          {needsHuman(status) && latest && (
                            <span className="agent-row-reason">{latest.status_label}</span>
                          )}
                          {latest && <span className="agent-row-meta">{agentMeta(status)}</span>}
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
                      <div className="agent-row-menu">
                        <button
                          type="button"
                          className="agent-row-menu-button"
                          aria-label={`More actions for ${conversation.title}`}
                          aria-haspopup="menu"
                          aria-expanded={menuOpen}
                          onClick={() => setMenuChatId(menuOpen ? null : conversation.chatId)}
                        >
                          <Ellipsis size={14} />
                        </button>
                        {menuOpen && (
                          <div className="agent-row-menu-list" role="menu">
                            {draft ? (
                              <button
                                type="button"
                                role="menuitem"
                                onClick={() => {
                                  setMenuChatId(null);
                                  onRemoveDraft(conversation.chatId);
                                }}
                              >
                                Remove
                              </button>
                            ) : (
                              <button
                                type="button"
                                role="menuitem"
                                disabled={!archived && status.state === "working"}
                                title={
                                  !archived && status.state === "working"
                                    ? "Archive after this agent finishes"
                                    : undefined
                                }
                                onClick={() => void archive(conversation.chatId, !archived)}
                              >
                                {archived ? "Restore" : "Archive"}
                              </button>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            ),
          )}
        </div>
        {hasMore && (
          <footer className="conversation-list-more">
            <button
              className="button primary compact"
              type="button"
              disabled={loadingMore}
              onClick={onLoadMore}
            >
              {loadingMore && <LoaderCircle className="spin" size={12} />}
              Load more
            </button>
          </footer>
        )}
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
            <PanelLeftOpen size={15} />
          </button>
        )}
        {selected ? (
          <NodeChat
            key={selected.chatId}
            project={project}
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
