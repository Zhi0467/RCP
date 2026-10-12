import { LoaderCircle, Send } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "../core/api";
import type { Episode } from "../core/types";

export type SendOrchestratorMessage = (
  episodeId: string,
  body: string,
  invocationCeiling?: number,
  requestId?: string,
) => Promise<void>;

/**
 * The orchestrator card's composer: the Runs card's Message orchestrator form,
 * gated by the same episode projection. It sends mail, or Add turns once ended;
 * it never starts an ordinary chat turn.
 */
export function OrchestratorComposer({
  episode,
  busyAction,
  onSendMessage,
}: {
  /** The newest member of the chain, whose projection decides what a message may do. */
  episode: Episode;
  busyAction: string | null;
  onSendMessage: SendOrchestratorMessage;
}) {
  const [message, setMessage] = useState("");
  const [messageTurns, setMessageTurns] = useState("3");
  const [refused, setRefused] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<{
    body: string;
    turns: string;
    invocationCeiling?: number;
    id: string;
  } | null>(null);
  const continues = episode.message_requires_continuation;
  const parsedTurns = Number(messageTurns);
  const turnsValid = Number.isSafeInteger(parsedTurns) && parsedTurns >= 1;
  const busy = busyAction !== null;
  const sending = busyAction === `message:${episode.episode_id}`;
  const disabled = refused || !episode.can_message || (continues && !turnsValid);

  useEffect(() => {
    setRefused(false);
  }, [episode]);

  const submit = async () => {
    const body = message.trim();
    if (!body || busy || disabled) return;
    setError(null);
    try {
      // A retry reuses its key only while what it asks is unchanged, including
      // whether the run must continue: an ended run needs the turns allowance.
      const invocationCeiling = continues ? parsedTurns : undefined;
      if (
        request.current?.body !== body ||
        request.current?.turns !== messageTurns ||
        request.current?.invocationCeiling !== invocationCeiling
      ) {
        request.current = { body, turns: messageTurns, invocationCeiling, id: crypto.randomUUID() };
      }
      await onSendMessage(
        episode.episode_id,
        body,
        request.current.invocationCeiling,
        request.current.id,
      );
      request.current = null;
      setMessage("");
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409) {
        request.current = null;
        setRefused(true);
      }
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  };

  return (
    <div className="chat-composer orchestrator-composer">
      {(episode.can_message || continues || episode.message_refusal) && (
        <form
          className="campaign-message-composer"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          {continues && (
            <label className="campaign-reauthorize" style={{ gridColumn: "1 / -1" }}>
              Add turns
              <input
                type="number"
                min={1}
                step={1}
                aria-label="Message continuation turns"
                value={messageTurns}
                disabled={busy || refused || !episode.can_message}
                onChange={(event) => setMessageTurns(event.target.value)}
              />
            </label>
          )}
          {episode.message_refusal && (
            <p
              style={{ gridColumn: "1 / -1" }}
              role="status"
              data-refusal-code={episode.message_refusal.code}
            >
              {episode.message_refusal.detail}
            </p>
          )}
          <textarea
            rows={2}
            aria-label="Message orchestrator"
            value={message}
            disabled={busy || refused || !episode.can_message}
            onChange={(event) => setMessage(event.target.value)}
          />
          <button
            className="button primary compact"
            type="submit"
            disabled={!message.trim() || busy || disabled}
          >
            {sending ? <LoaderCircle className="spin" size={12} /> : <Send size={12} />}
            Send
          </button>
        </form>
      )}
      {error && (
        <p className="campaign-run-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
