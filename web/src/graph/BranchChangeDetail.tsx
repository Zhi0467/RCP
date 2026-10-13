import type { GraphBranchChanges, GraphChangeSource, MergeDiffPath } from "../core/types";
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

const DECISION_MARKS = new Set<MergeDiffMark>(["conflict", "needs_agent", "proposal"]);

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
      {(change.before === null || change.change === "created") &&
      // A plain addition is just a tag; an addition the merge must decide shows its fields.
      !mergePaths?.some((path) => DECISION_MARKS.has(mergePathMark(path))) ? (
        <span className="eyebrow">New on this branch</span>
      ) : (
        <ChangedFields before={change.before} after={change.after} mergePaths={mergePaths} />
      )}
      <ChangeHistory history={change.history} onInspectTask={onInspectTask} />
    </section>
  );
}

const INTERNAL_FIELDS = new Set([
  "id",
  "created_rev",
  "updated_rev",
  "draft_touched",
  "current_summary_stale",
  "next_action_stale",
  "extension_type",
  "source_refs",
  "type",
]);

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
    // Internal fields stay out unless the merge names one, so a mark always shows its values.
    .filter(
      (key) =>
        !INTERNAL_FIELDS.has(key) ||
        mergePaths.some((path) => path.field_path !== "" && pathCovers(path, key)),
    )
    .filter((key) => !(emptyValue(beforeFields[key]) && emptyValue(afterFields[key])))
    .filter((key) => JSON.stringify(beforeFields[key]) !== JSON.stringify(afterFields[key]));
  return (
    <div className="branch-change-fields">
      {fields.map((key) => {
        // A whole-entity path ("") covers every field; a nested path covers its parent field.
        const paths = mergePaths.filter((path) => pathCovers(path, key));
        const mark = paths
          .map(mergePathMark)
          .reduce<MergeDiffMark | null>(
            (worst, next) =>
              worst && MERGE_MARK_SEVERITY.indexOf(worst) >= MERGE_MARK_SEVERITY.indexOf(next)
                ? worst
                : next,
            null,
          );
        const conflicts = paths.filter((path) => path.conflict);
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
            {conflicts.map((conflict) => (
              <p className="branch-change-value main-now" key={conflict.field_path}>
                <span>
                  Main now
                  {conflict.field_path.startsWith(`${key}/`) &&
                    ` · ${conflict.field_path.slice(key.length + 1)}`}
                </span>{" "}
                {readableValue(
                  conflict.field_path === "" ? fieldOf(conflict.main, key) : conflict.main,
                )}
              </p>
            ))}
          </div>
        );
      })}
    </div>
  );
}

/** Merge paths are slash-delimited; the whole-entity path ("") covers every field. */
function pathCovers(path: MergeDiffPath, key: string): boolean {
  return path.field_path === "" || path.field_path === key || path.field_path.startsWith(`${key}/`);
}

function fieldOf(entity: unknown, key: string): unknown {
  return entity && typeof entity === "object"
    ? (entity as Record<string, unknown>)[key]
    : undefined;
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

function emptyValue(value: unknown): boolean {
  return (
    value == null || value === "" || (typeof value === "object" && Object.keys(value).length === 0)
  );
}
