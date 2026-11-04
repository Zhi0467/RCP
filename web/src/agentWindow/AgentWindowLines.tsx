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
      className="node-chat-lines"
      aria-live="polite"
      onScroll={handle.handleChatScroll}
      ref={linesRef}
    >
      {children}
    </div>
  );
}
