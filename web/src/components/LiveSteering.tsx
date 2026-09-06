import { LoaderCircle, Send } from "lucide-react";
import { useId, useRef, useState } from "react";

import { steerChatTurn } from "../api";
import type { AgentTask, ChatMessage } from "../types";

interface Props {
  task: AgentTask;
  onReceipt: (message: ChatMessage) => void;
  onError: (message: string | null) => void;
}

export function LiveSteering({ task, onReceipt, onError }: Props) {
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const inputId = useId();
  const reasonId = useId();
  const reason = task.steer_unavailable_reason;

  async function send() {
    if (!message.trim() || !task.can_steer || !task.steer_turn_id || submittingRef.current) return;
    const request = {
      message_id: crypto.randomUUID(),
      attempt: task.attempt,
      expected_turn_id: task.steer_turn_id,
      message,
    };
    submittingRef.current = true;
    setSubmitting(true);
    onError(null);
    try {
      onReceipt(await steerChatTurn(task.project_id, task.operation_id, request));
      setMessage("");
    } catch (error) {
      onError(
        `Steering receipt could not be read. Nothing was resent. ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }

  return (
    <form
      className="chat-live-steering"
      aria-label="Steer running turn"
      onSubmit={(event) => {
        event.preventDefault();
        void send();
      }}
    >
      <label htmlFor={inputId}>Steer running turn</label>
      <textarea
        id={inputId}
        aria-describedby={reason ? reasonId : undefined}
        disabled={!task.can_steer || submitting}
        rows={2}
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            void send();
          }
        }}
      />
      <button
        className="icon-button primary"
        type="submit"
        disabled={!task.can_steer || !message.trim() || submitting}
        aria-describedby={reason ? reasonId : undefined}
      >
        {submitting ? <LoaderCircle size={14} className="spin" /> : <Send size={14} />}
        {submitting ? "Awaiting receipt" : "Send steer"}
      </button>
      {reason && <strong id={reasonId}>{reason}</strong>}
    </form>
  );
}
