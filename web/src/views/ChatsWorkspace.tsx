import { Circle, LoaderCircle, MessageCircle } from "lucide-react";
import { isActiveTask } from "../agentTasks";
import { conversationHasUnread, type ChatConversation } from "../chatWorkspace";
import type { AgentTask, ChatTranscript, GraphNode, ProjectSnapshot, StartAgentTask } from "../types";
import { NodeChat } from "../components/NodeChat";

interface Props {
  project: ProjectSnapshot;
  conversations: ChatConversation[];
  selectedChatId: string | null;
  nodes: Record<string, GraphNode>;
  runScope: string[];
  tasks: AgentTask[];
  activeTask: AgentTask | null;
  graphChangesDisabled: boolean;
  unreadTaskIds: ReadonlySet<string>;
  chatTranscripts: ReadonlyMap<string, ChatTranscript>;
  hasMore: boolean;
  loadingMore: boolean;
  onSelect: (chatId: string) => void;
  onLoadMore: () => void;
  onStartTask: StartAgentTask;
  onInspectTask: (taskId: string) => void;
  onOpenInbox: () => void;
  onRepairGraphUpdate: (taskId: string) => Promise<void>;
}

export function ChatsWorkspace({
  project,
  conversations,
  selectedChatId,
  nodes,
  runScope,
  tasks,
  activeTask,
  graphChangesDisabled,
  unreadTaskIds,
  chatTranscripts,
  hasMore,
  loadingMore,
  onSelect,
  onLoadMore,
  onStartTask,
  onInspectTask,
  onOpenInbox,
  onRepairGraphUpdate,
}: Props) {
  const selected = conversations.find((conversation) => conversation.chatId === selectedChatId)
    ?? conversations[0]
    ?? null;

  return (
    <section className="chats-workspace">
      <aside className="conversation-list" aria-label="Project conversations">
        <header><MessageCircle size={16} /><strong>Chats</strong></header>
        <div role="listbox" aria-label="Conversations">
          {conversations.map((conversation) => {
            const latest = conversation.tasks.at(-1);
            const active = conversation.tasks.some(isActiveTask);
            const unread = conversationHasUnread(conversation, unreadTaskIds);
            const selectedConversation = conversation.chatId === selected?.chatId;
            return (
              <button
                type="button"
                role="option"
                aria-selected={selectedConversation}
                aria-current={selectedConversation ? "page" : undefined}
                aria-label={`${conversation.title}, ${conversation.kind === "project_chat" ? "project" : "node"} conversation${unread ? ", unread result" : ""}`}
                className={`${selectedConversation ? "active" : ""}${unread ? " unread" : ""}`}
                onClick={() => onSelect(conversation.chatId)}
                key={conversation.chatId}
              >
                <span>{conversation.title}</span>
                <small>{conversation.kind === "project_chat" ? "Project" : "Node"}</small>
                {active && <Circle className="conversation-active" size={8} fill="currentColor" />}
                {!active && unread && <Circle className="conversation-unread" size={8} fill="currentColor" aria-hidden="true" />}
                {!active && !unread && latest && <time>{new Date(latest.updated_at).toLocaleDateString()}</time>}
              </button>
            );
          })}
        </div>
        {hasMore && (
          <footer className="conversation-list-more">
            <button className="button primary compact" type="button" disabled={loadingMore} onClick={onLoadMore}>
              {loadingMore && <LoaderCircle className="spin" size={12} />}
              Load more
            </button>
          </footer>
        )}
      </aside>
      <div className="conversation-surface">
        {selected ? (
          <NodeChat
            key={selected.chatId}
            project={project}
            node={selected.nodeId ? nodes[selected.nodeId] ?? null : null}
            runScope={runScope}
            tasks={tasks}
            activeTask={activeTask}
            historyMessages={chatTranscripts.get(selected.chatId)?.messages}
            chatId={selected.chatId}
            presentation="workspace"
            graphChangesDisabled={graphChangesDisabled}
            onStartTask={onStartTask}
            onInspectTask={onInspectTask}
            onOpenInbox={onOpenInbox}
            onRepairGraphUpdate={onRepairGraphUpdate}
            onClose={() => undefined}
          />
        ) : null}
      </div>
    </section>
  );
}
