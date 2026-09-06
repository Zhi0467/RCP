import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ConversationWorktreeState, WorktreeIntegrationOption } from "../types";
import "./WorktreeControls.css";

export function useConversationWorktree(
  projectId: string,
  chatId: string,
  chatScope: "node" | "project",
  nodeId: string | null,
  runOn: string,
  scope: string[],
  taskRevision: string,
  enabled: boolean,
) {
  const path = `/api/projects/${encodeURIComponent(projectId)}/chats/${encodeURIComponent(chatId)}/worktree`;
  const query = new URLSearchParams({ run_on: runOn, chat_scope: chatScope });
  if (nodeId) query.set("node_id", nodeId);
  if (scope.length === 0) query.append("run_truth_scope", "");
  scope.forEach((alias) => query.append("run_truth_scope", alias));
  const url = `${path}?${query}`;
  const [result, setResult] = useState<{
    url: string;
    state: ConversationWorktreeState | null;
    error: string | null;
  } | null>(null);
  const [revision, setRevision] = useState(0);
  const [selection, setSelection] = useState<string | null>(null);
  const request = useRef(0);
  const refresh = () => setRevision((value) => value + 1);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    const id = ++request.current;
    api<ConversationWorktreeState>(url, { signal: controller.signal }).then(
      (state) => {
        if (id === request.current && !controller.signal.aborted) {
          setResult({ url, state, error: null });
          if (state.binding) setSelection(null);
        }
      },
      (error) => {
        if (id === request.current && !controller.signal.aborted) {
          setResult({ url, state: null, error: String(error) });
        }
      },
    );
    return () => {
      controller.abort();
      request.current += 1;
    };
  }, [url, taskRevision, revision, enabled]);

  const current = result?.url === url ? result : null;
  return {
    state: current?.state ?? null,
    error: current?.error ?? null,
    chosen: selection === url,
    choose: (selected: boolean) => setSelection(selected ? url : null),
    refresh,
    previewRemoval: async () => {
      const id = ++request.current;
      const state = await api<ConversationWorktreeState>(`${url}&inspect_removal=true`);
      if (id !== request.current)
        throw new Error("Worktree changed while checking removal. Try again.");
      setResult({ url, state, error: null });
    },
    remove: async () => {
      await api<ConversationWorktreeState>(path, { method: "DELETE" });
      refresh();
    },
  };
}

interface Props {
  state: ConversationWorktreeState | null;
  error: string | null;
  chosen: boolean;
  disabled: boolean;
  onChoose: (selected: boolean) => void;
  onIntegrate: (option: WorktreeIntegrationOption) => Promise<void>;
  onRemove: () => Promise<void>;
  onPreviewRemove: () => Promise<void>;
  onRefresh: () => void;
}

export function WorktreeControls({
  state,
  error,
  chosen,
  disabled,
  onChoose,
  onIntegrate,
  onRemove,
  onPreviewRemove,
  onRefresh,
}: Props) {
  const [integrating, setIntegrating] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [previewingRemoval, setPreviewingRemoval] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const binding = state?.binding;

  const previewRemoval = async () => {
    setPreviewingRemoval(true);
    setRemoveError(null);
    setIntegrating(false);
    try {
      await onPreviewRemove();
      setConfirmRemove(true);
    } catch (error) {
      setRemoveError(error instanceof Error ? error.message : String(error));
    } finally {
      setPreviewingRemoval(false);
    }
  };

  const remove = async () => {
    setRemoving(true);
    setRemoveError(null);
    try {
      await onRemove();
      setConfirmRemove(false);
    } catch (error) {
      setRemoveError(error instanceof Error ? error.message : String(error));
    } finally {
      setRemoving(false);
    }
  };

  return (
    <section className="chat-worktree-controls" aria-label="Conversation worktree">
      {error ? (
        <>
          <span role="alert">{error}</span>
          <button className="button compact" type="button" onClick={onRefresh}>
            Retry worktree check
          </button>
        </>
      ) : !state ? (
        <span role="status">Checking worktree…</span>
      ) : binding ? (
        <>
          <strong title={binding.worktree_path}>
            Worktree: {binding.branch} ({String(binding.status)})
          </strong>
          {state.unavailable_reason && <span role="alert">{state.unavailable_reason}</span>}
          <button
            className="button compact"
            type="button"
            aria-expanded={integrating}
            disabled={disabled || removing || previewingRemoval}
            onClick={() => {
              onRefresh();
              setIntegrating((open) => !open);
              setConfirmRemove(false);
            }}
          >
            Integrate
          </button>
          <button
            className="button compact"
            type="button"
            disabled={disabled || removing || previewingRemoval || !state.can_remove}
            title={state.remove_reason ?? undefined}
            onClick={() => void previewRemoval()}
          >
            {previewingRemoval ? "Checking removal…" : "Remove worktree"}
          </button>
          {state.remove_reason && <span>{state.remove_reason}</span>}
          {removeError && <span role="alert">{removeError}</span>}
          {integrating && (
            <div className="chat-worktree-options">
              {state.integration_options.map((option) => (
                <div key={option.id}>
                  <button
                    className="button compact"
                    type="button"
                    disabled={disabled || !option.enabled}
                    title={option.reason ?? undefined}
                    onClick={() => void onIntegrate(option)}
                  >
                    {option.label}
                  </button>
                  {option.reason && <span>{option.reason}</span>}
                </div>
              ))}
            </div>
          )}
          {state.dirty_worktree.length > 0 && (
            <pre aria-label="Uncommitted worktree changes">{state.dirty_worktree.join("\n")}</pre>
          )}
          {confirmRemove && (
            <div
              className="chat-worktree-removal"
              role="group"
              aria-label="Confirm worktree removal"
            >
              <p>
                Remove {binding.worktree_path}? Branch {binding.branch} will be kept.
              </p>
              {state.remote_branch_evidence && <p>{state.remote_branch_evidence}</p>}
              <p>
                Ahead of {binding.starting_branch}: {state.ahead_count ?? "unknown"} commits. Remote
                branch:{" "}
                {state.remote_branch_exists === null
                  ? "unknown"
                  : state.remote_branch_exists
                    ? "exists"
                    : "absent"}
                .
              </p>
              <button
                className="button compact"
                type="button"
                disabled={disabled || removing || !state.can_remove}
                onClick={() => void remove()}
              >
                {removing ? "Removing…" : "Confirm removal"}
              </button>
              <button
                className="button compact"
                type="button"
                disabled={removing}
                onClick={() => setConfirmRemove(false)}
              >
                Cancel
              </button>
            </div>
          )}
        </>
      ) : state.show_chooser ? (
        <>
          <label>
            <input
              type="checkbox"
              checked={chosen}
              disabled={disabled || !state.can_choose}
              onChange={(event) => onChoose(event.target.checked)}
            />
            Work in a worktree
          </label>
          {state.unavailable_reason && <span>{state.unavailable_reason}</span>}
        </>
      ) : null}
    </section>
  );
}
