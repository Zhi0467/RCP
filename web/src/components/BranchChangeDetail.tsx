import type { GraphBranchChanges, GraphChangeSource } from "../types";
import { humanFieldLabels, humanize } from "../nodePresentation";

type NodeChange = GraphBranchChanges["nodes"][number];

export function BranchChangeDetail({
  change,
  onInspectTask,
}: {
  change: NodeChange;
  onInspectTask?: (taskId: string) => void;
}) {
  return (
    <section className="branch-change-detail" aria-label="Branch changes">
      <h3>
        <span className={`branch-change-badge ${change.change}`}>{humanize(change.change)}</span> in
        this branch
      </h3>
      <details open={change.change === "updated"}>
        <summary>Before and after</summary>
        <ChangedFields before={change.before} after={change.after} />
      </details>
      <ChangeHistory history={change.history} onInspectTask={onInspectTask} />
    </section>
  );
}

export function ChangedFields({ before, after }: { before: object | null; after: object | null }) {
  const beforeFields = Object.fromEntries(Object.entries(before ?? {}));
  const afterFields = Object.fromEntries(Object.entries(after ?? {}));
  const fields = [...new Set([...Object.keys(beforeFields), ...Object.keys(afterFields)])]
    .filter((key) => !["id", "created_rev", "updated_rev", "draft_touched"].includes(key))
    .filter((key) => JSON.stringify(beforeFields[key]) !== JSON.stringify(afterFields[key]));
  return (
    <table className="branch-change-fields">
      <thead>
        <tr>
          <th>Field</th>
          <th>At branch start</th>
          <th>Now</th>
        </tr>
      </thead>
      <tbody>
        {fields.map((key) => (
          <tr key={key}>
            <th>{humanFieldLabels[key] ?? humanize(key)}</th>
            <td>{readableValue(beforeFields[key])}</td>
            <td>{readableValue(afterFields[key])}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function ChangeHistory({
  history,
  onInspectTask,
}: {
  history: GraphChangeSource[];
  onInspectTask?: (taskId: string) => void;
}) {
  if (history.length === 0) return null;
  return (
    <ol className="branch-change-history" aria-label="Change history">
      {history.map((source, index) => (
        <li key={`${source.revision}:${index}`}>
          <span>
            <strong>{humanize(source.producer)}</strong> · Revision {source.revision}
          </span>
          <p>{source.summary}</p>
          {source.task_id && onInspectTask && (
            <button
              type="button"
              className="button compact secondary"
              onClick={() => onInspectTask(source.task_id!)}
            >
              View task
            </button>
          )}
        </li>
      ))}
    </ol>
  );
}

function readableValue(value: unknown): string {
  if (value === undefined || value === null || value === "") return "—";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(readableValue).join("\n");
  if (typeof value === "object")
    return Object.entries(value)
      .map(([key, item]) => `${humanize(key)}: ${readableValue(item)}`)
      .join("\n");
  return String(value);
}
