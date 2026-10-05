import type { GraphBranchChanges, GraphChangeSource, MergeDiffPath } from "../types";
import { MERGE_MARK_SEVERITY, type MergeDiffMark, mergePathMark } from "./mergePanel";
import { humanFieldLabels, humanize } from "./nodePresentation";

type NodeChange = GraphBranchChanges["nodes"][number];

const MERGE_MARK_LABELS: Record<MergeDiffMark, string | null> = {
  delivered: "Already merged",
  changed: null,
  proposal: "Becomes a Proposal",
  needs_agent: "Needs agent",
  conflict: "Conflict",
};

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
      <ChangedFields before={change.before} after={change.after} mergePaths={mergePaths} />
      <ChangeHistory history={change.history} onInspectTask={onInspectTask} />
    </section>
  );
}

/** Each changed field as main before, branch, and main now beside a conflict. */
export function ChangedFields({
  before,
  after,
  mergePaths = [],
}: {
  before: object | null;
  after: object | null;
  mergePaths?: MergeDiffPath[];
}) {
  const beforeFields: Record<string, unknown> = Object.fromEntries(Object.entries(before ?? {}));
  const afterFields: Record<string, unknown> = Object.fromEntries(Object.entries(after ?? {}));
  const fields = [...new Set([...Object.keys(beforeFields), ...Object.keys(afterFields)])]
    .filter((key) => !["id", "created_rev", "updated_rev", "draft_touched"].includes(key))
    .filter((key) => JSON.stringify(beforeFields[key]) !== JSON.stringify(afterFields[key]));
  return (
    <div className="branch-change-fields">
      {fields.map((key) => {
        // A whole-entity path ("") covers every field; a nested path covers its parent field.
        const paths = mergePaths.filter(
          (path) =>
            path.field_path === "" ||
            path.field_path === key ||
            path.field_path.startsWith(`${key}.`),
        );
        const mark = paths
          .map(mergePathMark)
          .reduce<MergeDiffMark | null>(
            (worst, next) =>
              worst && MERGE_MARK_SEVERITY.indexOf(worst) >= MERGE_MARK_SEVERITY.indexOf(next)
                ? worst
                : next,
            null,
          );
        const conflict = paths.find((path) => path.conflict);
        const label = mark && MERGE_MARK_LABELS[mark];
        return (
          <div className="branch-change-field" key={key}>
            <div className="branch-change-field-head">
              <span className="eyebrow">{humanFieldLabels[key] ?? humanize(key)}</span>
              {label && <span className={`branch-diff-word diff-${mark}`}>{label}</span>}
            </div>
            {before && (
              <p className="branch-change-value main-before">
                <span>Main before</span> <s>{readableValue(beforeFields[key])}</s>
              </p>
            )}
            <p className="branch-change-value branch">
              <span>Branch</span> {after ? readableValue(afterFields[key]) : "Removed"}
            </p>
            {conflict && (
              <p className="branch-change-value main-now">
                <span>Main now</span> {readableValue(conflict.main)}
              </p>
            )}
          </div>
        );
      })}
    </div>
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
            {humanize(source.producer)} · {source.summary}
          </span>
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
