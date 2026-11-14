import { ArrowDown } from "lucide-react";
import { useImperativeHandle, type ReactNode, type Ref, type RefObject } from "react";
import { useAgentWindowScroll, type AgentWindowScroll } from "./useAgentWindowScroll";

/**
 * The scrolling body of every agent window. It follows the newest line and
 * reopens each conversation where the reader left it.
 */
export function AgentWindowLines({
  scrollKey,
  linesRef,
  transcript,
  questions,
  scroll,
  children,
}: {
  scrollKey: string;
  linesRef: RefObject<HTMLDivElement | null>;
  transcript: unknown;
  questions: unknown;
  scroll?: Ref<AgentWindowScroll>;
  children: ReactNode;
}) {
  const handle = useAgentWindowScroll(scrollKey, linesRef, transcript, questions);
  useImperativeHandle(scroll, () => handle);
  return (
    <div
      className="agent-chat-view-lines"
      aria-live="polite"
      onScroll={handle.handleChatScroll}
      ref={linesRef}
    >
      {children}
      <div className="agent-chat-view-jump" data-shown={handle.farFromBottom || undefined}>
        <button
          type="button"
          className="icon-button"
          aria-label="Jump to latest"
          title="Jump to latest"
          onClick={handle.jumpToLatest}
        >
          <ArrowDown size={16} />
        </button>
      </div>
    </div>
  );
}
