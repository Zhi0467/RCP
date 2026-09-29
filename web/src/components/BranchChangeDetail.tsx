import type { GraphBranchChanges, GraphChangeSource, MergeDiffPath } from "../types";
import { mergePathMark } from "../mergePanel";
import { humanFieldLabels, humanize } from "../nodePresentation";

type NodeChange = GraphBranchChanges["nodes"][number];

const MERGE_MARK_LABELS = {
  delivered: "Already merged",
  changed: "Merges",
  proposal: "Becomes a Proposal",
  needs_agent: "Needs agent",
  conflict: "Conflict",
} as const;

export function BranchChangeDetail({
  change,
  mergePaths,
  onInspectTask,
}: {
  change: NodeChange;
  mergePaths?: MergeDiffPath[];
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
      {mergePaths && mergePaths.length > 0 && <MergeFields paths={mergePaths} />}
      <ChangeHistory history={change.history} onInspectTask={onInspectTask} />
    </section>
  );
}

/** Field-level base → branch, with main beside a conflict, marked as Merge will treat it. */
function MergeFields({ paths }: { paths: MergeDiffPath[] }) {
  return (
    <details open={paths.some((path) => path.conflict || path.needs_agent)}>
      <summary>On merge</summary>
      <table className="branch-change-fields merge-fields">
        <thead>
          <tr>
            <th>Field</th>
            <th>At branch start</th>
            <th>Branch</th>
            <th>Main now</th>
            <th>On merge</th>
          </tr>
        </thead>
        <tbody>
          {paths.map((path) => {
            const mark = mergePathMark(path);
            return (
              <tr key={path.field_path} className={`merge-mark-${mark}`}>
                <th>{humanFieldLabels[path.field_path] ?? humanize(path.field_path || "node")}</th>
                <td>{readableValue(path.base)}</td>
                <td>{readableValue(path.branch)}</td>
                <td>{path.conflict ? readableValue(path.main) : ""}</td>
                <td>{MERGE_MARK_LABELS[mark]}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </details>
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
