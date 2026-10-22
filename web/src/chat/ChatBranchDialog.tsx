import { CornerUpLeft, LoaderCircle, RotateCcw, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { branchChat, createGraphFork, loadGraphRefs } from "../core/api";
import { MAIN_GRAPH, projectViewHash, sameGraphTarget } from "../core/graphTarget";
import type { ChatOrigin, GraphRef, GraphTargetRef } from "../core/types";
import "./chatBranch.css";

function targetOf(ref: GraphRef): GraphTargetRef {
  return ref.kind === "main" ? MAIN_GRAPH : { kind: "branch", branch_id: ref.branch_id };
}

/** The first transcript line of a branched chat: a link back to the chat it came from. */
export function ChatOriginLink({ projectId, origin }: { projectId: string; origin: ChatOrigin }) {
  return (
    <a
      className="chat-origin-link"
      href={projectViewHash(projectId, origin.source_graph_target, "chats", {
        chatId: origin.source_chat_id,
      })}
    >
      <CornerUpLeft size={12} aria-hidden="true" />
      {origin.source_title}
    </a>
  );
}

/** Branches a chat onto a new or existing graph branch and opens the new chat. */
export function ChatBranchDialog({
  projectId,
  chatId,
  graphTarget,
  onClose,
}: {
  projectId: string;
  chatId: string;
  graphTarget: GraphTargetRef;
  onClose: () => void;
}) {
  const [refs, setRefs] = useState<GraphRef[] | null>(null);
  const [selection, setSelection] = useState("new");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const createdTarget = useRef<GraphTargetRef | null>(null);
  // A retry repeats the first fork request whole: the same id at the same head.
  const forkRequest = useRef<{ id: string; revision: number } | null>(null);

  const load = async () => {
    setError(null);
    setBusy(true);
    try {
      setRefs(await loadGraphRefs(projectId));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    dialog.current?.showModal();
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- loads once per opening
  }, []);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      let target = createdTarget.current;
      if (!target && selection === "new") {
        if (!forkRequest.current) {
          // The source's head now, including changes since the dialog opened.
          const current = (await loadGraphRefs(projectId)).find((ref) =>
            sameGraphTarget(targetOf(ref), graphTarget),
          );
          if (!current) throw new Error("The source graph is unavailable.");
          forkRequest.current = { id: crypto.randomUUID(), revision: current.head.revision };
        }
        const fork = await createGraphFork(
          projectId,
          graphTarget,
          forkRequest.current.revision,
          forkRequest.current.id,
          name.trim() || undefined,
        );
        target = targetOf(fork);
        createdTarget.current = target;
      }
      if (!target) {
        const ref = refs?.find(
          (ref) => (ref.kind === "main" ? "main" : ref.branch_id) === selection,
        );
        if (!ref) throw new Error("Choose a branch.");
        target = targetOf(ref);
      }
      const transcript = await branchChat(projectId, chatId, target);
      onClose();
      window.location.hash = projectViewHash(projectId, transcript.graph_target, "chats", {
        chatId: transcript.chat_id,
      });
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };

  const locked = busy || Boolean(createdTarget.current);
  return createPortal(
    <dialog
      ref={dialog}
      className="chat-branch-dialog"
      aria-labelledby="chat-branch-title"
      data-error={Boolean(error)}
      onCancel={onClose}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <header>
          <strong id="chat-branch-title">Branch this chat</strong>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>
        <label>
          Target
          <select
            aria-label="Branch target"
            value={selection}
            disabled={locked}
            onChange={(event) => {
              setSelection(event.target.value);
              forkRequest.current = null;
              setError(null);
            }}
          >
            <option value="new">New branch</option>
            {refs
              ?.filter((ref) => !ref.archived && !sameGraphTarget(targetOf(ref), graphTarget))
              .map((ref) => (
                <option key={ref.branch_id ?? "main"} value={ref.branch_id ?? "main"}>
                  {ref.name}
                </option>
              ))}
          </select>
        </label>
        {selection === "new" && (
          <label>
            Name (optional)
            <input
              aria-label="Branch name"
              value={name}
              disabled={locked}
              onChange={(event) => {
                setName(event.target.value);
                forkRequest.current = null;
              }}
            />
          </label>
        )}
        <footer>
          {error ? (
            <button
              className="icon-button"
              type="button"
              aria-label="Retry"
              title={error}
              disabled={busy}
              onClick={() => void (refs ? submit() : load())}
            >
              <RotateCcw size={16} />
            </button>
          ) : (
            <button className="button primary" type="submit" disabled={busy || !refs}>
              {busy ? (
                <LoaderCircle size={16} aria-label="Loading" className="spinning" />
              ) : (
                "Branch chat"
              )}
            </button>
          )}
        </footer>
      </form>
    </dialog>,
    document.body,
  );
}
