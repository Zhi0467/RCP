import { useEffect, useRef, useState, type DragEvent } from "react";
import { RotateCw, X } from "lucide-react";
import { api, ApiError } from "../core/api";
import type { ProjectCard, ProjectGroup } from "../core/types";
import { canDropProject, PROJECT_DRAG_TYPE } from "./projectGroups";

export function useProjectGroupActions(onChanged: () => Promise<void>) {
  const [failure, setFailure] = useState<{ id: string; retry: () => void; message: string } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const run = async (id: string, mutation: () => Promise<unknown>) => {
    if (locked.current) return false;
    locked.current = true;
    setBusy(true);
    setFailure(null);
    let applied = false;
    try {
      await mutation();
      applied = true;
      await onChanged();
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        try {
          await onChanged();
        } catch {
          /* The retry below also refreshes. */
        }
      }
      setFailure({
        id,
        message: String(error),
        retry: () => void run(id, applied ? async () => {} : mutation),
      });
      return false;
    } finally {
      locked.current = false;
      setBusy(false);
    }
  };
  const move = (projectId: string, groupId: string | null) =>
    run(projectId, () =>
      api(`/api/projects/${encodeURIComponent(projectId)}/group`, {
        method: "PUT",
        body: JSON.stringify({ group_id: groupId }),
      }),
    );
  const retry = (id: string) =>
    failure?.id === id ? (
      <button
        className="icon-button group-retry"
        aria-label="Retry group change"
        title={failure.message}
        disabled={busy}
        onClick={failure.retry}
      >
        <RotateCw size={16} />
      </button>
    ) : null;
  return { run, move, busy, failure, retry };
}

export function GroupNameDialog({
  name,
  onClose,
  onSave,
}: {
  name: string;
  onClose: () => void;
  onSave: (name: string) => Promise<void>;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [value, setValue] = useState(name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  const save = async () => {
    if (!value.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onSave(value.trim());
      onClose();
    } catch (error) {
      setError(String(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <dialog
      ref={ref}
      className={`group-name-dialog${error ? " group-failed" : ""}`}
      aria-labelledby="group-name-title"
      onCancel={(event) => {
        if (busy) event.preventDefault();
        else onClose();
      }}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <header>
          <h2 id="group-name-title">{name ? "Rename group" : "New group"}</h2>
          <button
            type="button"
            className="icon-button"
            aria-label="Close"
            disabled={busy}
            onClick={onClose}
          >
            <X size={18} />
          </button>
        </header>
        <label htmlFor="group-name">Name</label>
        <input
          id="group-name"
          autoFocus
          value={value}
          disabled={busy}
          onChange={(event) => {
            setValue(event.target.value);
            setError(null);
          }}
        />
        <footer>
          {error ? (
            <button
              type="submit"
              className="icon-button"
              aria-label="Retry saving group"
              title={error}
              disabled={busy}
            >
              <RotateCw size={16} />
            </button>
          ) : (
            <button className="button primary" disabled={busy || !value.trim()}>
              Save
            </button>
          )}
        </footer>
      </form>
    </dialog>
  );
}

export function useProjectGroupDrag(
  projects: ProjectCard[],
  groups: ProjectGroup[],
  move: (id: string, groupId: string | null) => unknown,
) {
  const [dragged, setDragged] = useState<string | null>(null);
  const [over, setOver] = useState<string | null | undefined>(undefined);
  const end = () => {
    setDragged(null);
    setOver(undefined);
  };
  const target = (groupId: string | null) => ({
    onDragOver: (event: DragEvent) => {
      if (
        !canDropProject(
          projects.find((project) => project.id === dragged),
          groupId,
          groups,
        )
      )
        return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      setOver(groupId);
    },
    onDragLeave: (event: DragEvent) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOver(undefined);
    },
    onDrop: (event: DragEvent) => {
      event.preventDefault();
      const id = event.dataTransfer.getData(PROJECT_DRAG_TYPE);
      if (
        id === dragged &&
        canDropProject(
          projects.find((project) => project.id === id),
          groupId,
          groups,
        )
      )
        move(id, groupId);
      end();
    },
  });
  const source = (id: string) => ({
    draggable: true,
    onDragStart: (event: DragEvent) => {
      event.dataTransfer.setData(PROJECT_DRAG_TYPE, id);
      event.dataTransfer.effectAllowed = "move";
      setDragged(id);
    },
    onDragEnd: end,
  });
  return { source, target, over };
}
