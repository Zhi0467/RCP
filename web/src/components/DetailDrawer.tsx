import {
  AlertTriangle,
  Check,
  FlaskConical,
  MessageCircle,
  Minus,
  Network,
  PencilLine,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { GlossaryIndex } from "../glossary";
import { DraggableWindow } from "./DraggableWindow";
import { GlossaryText } from "./GlossaryText";
import {
  changedNodeFields,
  editableNodeFields,
  nodeEditDraft,
  type NodeEditField,
} from "../nodeEditing";
import type { DraftNodeValue } from "../humanDraft";
import { beliefCausePresentation, edgeValidationFlags, nodeBeliefTransitions } from "../nodeDetail";
import { humanFieldLabels, humanize, nodeTypeLabel, presentNode } from "../nodePresentation";
import type {
  BeliefTransition,
  Edge,
  ExperimentControlState,
  GraphNode,
  OntologyState,
  ValidationMessage,
} from "../types";

interface Props {
  node: GraphNode;
  edges: Edge[];
  allNodes: Record<string, GraphNode>;
  glossaryIndex: GlossaryIndex;
  beliefTransitions: BeliefTransition[];
  validationMessages: ValidationMessage[];
  ontology: OntologyState;
  sizeStorageKey?: string;
  mutationsDisabled?: boolean;
  stagedNewNode?: boolean;
  stagedForRemoval?: boolean;
  hasStagedNodeChange?: boolean;
  canonicalStanding?: GraphNode["standing"];
  experimentControl?: ExperimentControlState | null;
  experimentWatcherCount?: number;
  onStopExperimentWatchers?: () => void;
  experimentRunDisabled?: boolean;
  experimentRunBusy?: boolean;
  onUnstage?: () => void;
  onRemove?: () => void;
  onUndoRemoval?: () => void;
  onClose: () => void;
  onDock: () => void;
  onBeginEdit: () => void;
  onStanding: (standing: GraphNode["standing"]) => void;
  onStage: (changes: Record<string, DraftNodeValue>) => void;
  onRunExperiment?: () => void;
  onOpenChat: () => void;
  onExploreRelations: () => void;
  onSelectNode: (nodeId: string) => void;
}

const ignored = new Set([
  "id",
  "type",
  "title",
  "standing",
  "created_rev",
  "updated_rev",
  "source_refs",
  "draft_touched",
  "origin",
  "extension_type",
  "extension_fields",
]);

const originLabels: Record<NonNullable<GraphNode["origin"]>, string> = {
  internal_run: "Internal run",
  external_publication: "External publication",
  external_instance: "External instance",
  analytic: "Analytic",
  unknown: "Unknown",
};

export function DetailDrawer({
  node,
  edges,
  allNodes,
  glossaryIndex,
  beliefTransitions,
  validationMessages,
  ontology,
  sizeStorageKey,
  mutationsDisabled = false,
  stagedNewNode = false,
  stagedForRemoval = false,
  hasStagedNodeChange = false,
  canonicalStanding = node.standing,
  experimentControl = null,
  experimentWatcherCount = 0,
  onStopExperimentWatchers,
  experimentRunDisabled = false,
  experimentRunBusy = false,
  onUnstage,
  onRemove,
  onUndoRemoval,
  onClose,
  onDock,
  onBeginEdit,
  onStanding,
  onStage,
  onRunExperiment,
  onOpenChat,
  onExploreRelations,
  onSelectNode,
}: Props) {
  const [editing, setEditing] = useState(false);
  const [removalConfirmationOpen, setRemovalConfirmationOpen] = useState(false);
  const [editBase, setEditBase] = useState(node);
  const [draft, setDraft] = useState<Record<string, string>>(() => nodeEditDraft(node, ontology));
  const standingBeforeEdit = useRef<GraphNode["standing"] | null>(null);
  const editFields = useMemo(() => editableNodeFields(editBase, ontology), [editBase, ontology]);
  const changes = useMemo(
    () => changedNodeFields(editBase, draft, ontology),
    [draft, editBase, ontology],
  );
  const changeCount = Object.keys(changes).length;
  const editErrors = useMemo(
    () =>
      Object.fromEntries(
        editFields.flatMap((field) => {
          const error = nodeEditFieldError(field, draft[field.key] ?? "");
          return error ? [[field.key, error]] : [];
        }),
      ),
    [draft, editFields],
  );
  const editInvalid = Object.keys(editErrors).length > 0;
  const nodeMutationDisabled = mutationsDisabled || stagedForRemoval;
  const detachedExperimentWorkActive = experimentWatcherCount > 0;
  const experimentControlActive = Boolean(
    experimentControl?.active || detachedExperimentWorkActive,
  );
  const experimentPausedAtLimit = Boolean(experimentControl?.paused && !experimentControlActive);

  useEffect(() => {
    if (!editing) {
      setEditBase(node);
      setDraft(nodeEditDraft(node, ontology));
    }
  }, [editing, node, ontology]);

  useEffect(() => {
    if (nodeMutationDisabled) setEditing(false);
  }, [nodeMutationDisabled]);

  const beginEditing = () => {
    if (nodeMutationDisabled) return;
    standingBeforeEdit.current = node.standing;
    if (node.standing !== "asserted") onBeginEdit();
    setEditBase(node);
    setDraft(nodeEditDraft(node, ontology));
    setEditing(true);
  };
  const cancelEditing = () => {
    if (standingBeforeEdit.current && standingBeforeEdit.current !== "asserted") {
      onStanding(standingBeforeEdit.current);
    }
    standingBeforeEdit.current = null;
    setEditBase(node);
    setDraft(nodeEditDraft(node, ontology));
    setEditing(false);
  };
  const stage = () => {
    if (changeCount === 0 || nodeMutationDisabled || editInvalid) return;
    onStage(changes);
    standingBeforeEdit.current = null;
    setEditing(false);
  };
  const close = () => {
    if (editing && standingBeforeEdit.current && standingBeforeEdit.current !== "asserted") {
      onStanding(standingBeforeEdit.current);
    }
    standingBeforeEdit.current = null;
    onClose();
  };

  const relations = edges.filter((edge) => edge.source === node.id || edge.target === node.id);
  const removalBlockedReason = stagedForRemoval
    ? null
    : canonicalStanding === "accepted"
      ? "Clear or contest this accepted node and Sync before removing it."
      : experimentControl?.active
        ? "This node cannot be removed while its bounded experiment loop is active."
        : hasStagedNodeChange
          ? "Sync or reset this node's staged changes before removing it."
          : null;
  useEffect(() => {
    setRemovalConfirmationOpen(false);
  }, [
    canonicalStanding,
    experimentControl?.active,
    hasStagedNodeChange,
    mutationsDisabled,
    node.id,
    stagedForRemoval,
  ]);
  const confirmRemoval = () => {
    if (mutationsDisabled || removalBlockedReason || !onRemove) return;
    setRemovalConfirmationOpen(false);
    onRemove();
  };
  const transitions = nodeBeliefTransitions(node.id, beliefTransitions);
  const presentation = presentNode(node);
  const presentedKeys = new Set([
    presentation.key,
    ...presentation.context.map((item) => item.key),
  ]);
  const details = Object.entries(node).filter(
    ([key, value]) => !ignored.has(key) && !presentedKeys.has(key) && hasValue(value),
  );
  const fullscreenTarget = typeof document === "undefined" ? null : document.fullscreenElement;
  const drawer = (
    <DraggableWindow
      className="node-detail-window"
      kind="detail"
      resizable
      sizeStorageKey={sizeStorageKey}
    >
      <aside
        className={`detail-drawer node-detail-drawer${node.draft_touched ? " draft-touched" : ""}`}
        role="dialog"
        aria-modal="false"
        aria-labelledby="drawer-title"
      >
        <header data-drag-handle="true">
          <div data-text-selectable="true">
            <span className="eyebrow">{nodeTypeLabel(node)}</span>
            <h2 id="drawer-title">
              <GlossaryText text={node.title} glossaryIndex={glossaryIndex} />
            </h2>
            <div className="node-meta">
              <span className="mono">{node.id}</span>
              <span className={`standing ${node.standing}`}>
                {node.standing}
                {canonicalStanding !== node.standing && " · staged"}
              </span>
              {node.type === "evidence" && node.origin && (
                <span className="node-origin">{originLabels[node.origin]}</span>
              )}
            </div>
          </div>
          <div className="window-actions">
            <button
              className="icon-button"
              aria-label="Dock node window"
              title="Dock node window"
              onClick={onDock}
            >
              <Minus size={18} />
            </button>
            <button className="icon-button" aria-label="Close detail" onClick={close}>
              <X size={18} />
            </button>
          </div>
        </header>

        <div className={`drawer-content${editing ? " editing" : ""}`}>
          {stagedForRemoval && (
            <section className="node-removal-staged" role="status">
              <Trash2 size={16} />
              <span>
                <strong>Removal staged.</strong> Sync will remove this node and {relations.length}{" "}
                connected relation{relations.length === 1 ? "" : "s"}.
              </span>
              <button
                className="button compact"
                type="button"
                disabled={mutationsDisabled || !onUndoRemoval}
                onClick={onUndoRemoval}
              >
                Undo
              </button>
            </section>
          )}
          {editing ? (
            <form
              className="node-edit-form"
              onSubmit={(event) => {
                event.preventDefault();
                stage();
              }}
            >
              {editFields.map((field) => (
                <label className="node-edit-field" key={field.key}>
                  <span>
                    {field.label}
                    {field.kind === "list" ? " · one item per line" : ""}
                    {field.nullable ? <span className="node-field-optional">Optional</span> : null}
                  </span>
                  {field.kind === "text" || field.kind === "number" ? (
                    <>
                      <input
                        type={field.kind === "number" ? "number" : "text"}
                        min={field.min}
                        step={field.kind === "number" ? (field.integer ? 1 : "any") : undefined}
                        aria-invalid={Boolean(editErrors[field.key])}
                        autoFocus={field.key === "title"}
                        value={draft[field.key] ?? ""}
                        onChange={(event) =>
                          setDraft((current) => ({ ...current, [field.key]: event.target.value }))
                        }
                      />
                      {editErrors[field.key] && (
                        <small className="node-edit-error" role="alert">
                          {editErrors[field.key]}
                        </small>
                      )}
                    </>
                  ) : field.kind === "boolean" ? (
                    <select
                      value={draft[field.key] ?? ""}
                      onChange={(event) =>
                        setDraft((current) => ({ ...current, [field.key]: event.target.value }))
                      }
                    >
                      {field.nullable && <option value="">—</option>}
                      <option value="true">Yes</option>
                      <option value="false">No</option>
                    </select>
                  ) : (
                    <textarea
                      rows={field.kind === "list" ? 4 : 5}
                      value={draft[field.key] ?? ""}
                      onChange={(event) =>
                        setDraft((current) => ({ ...current, [field.key]: event.target.value }))
                      }
                    />
                  )}
                </label>
              ))}
            </form>
          ) : (
            <>
              <section className="node-lead">
                <span className="eyebrow">{presentation.label}</span>
                <p>{formatValue(presentation.value, glossaryIndex)}</p>
              </section>

              {node.type === "experiment" && experimentControl && onRunExperiment && (
                <section
                  className={`experiment-control${experimentControlActive ? " active" : ""}${experimentPausedAtLimit ? " paused" : ""}`}
                >
                  <div className="experiment-control-heading">
                    <div>
                      <span className="eyebrow">Episode invocations</span>
                      <strong>
                        {experimentControl.invocations_used} /{" "}
                        {experimentControl.invocation_ceiling}
                      </strong>
                      <span className="experiment-invocations-remaining">
                        {experimentControl.invocations_remaining} remaining
                      </span>
                    </div>
                    {experimentControlActive && (
                      <span className="experiment-loop-marker">Active loop</span>
                    )}
                    {experimentPausedAtLimit && (
                      <span className="experiment-loop-marker paused">Paused at limit</span>
                    )}
                    <button
                      className="button primary compact experiment-run-button"
                      type="button"
                      disabled={
                        nodeMutationDisabled ||
                        experimentRunDisabled ||
                        experimentRunBusy ||
                        experimentControlActive ||
                        !experimentControl.ready
                      }
                      onClick={onRunExperiment}
                    >
                      <FlaskConical size={13} />{" "}
                      {experimentRunBusy
                        ? "Starting"
                        : experimentPausedAtLimit
                          ? "Run pending wake"
                          : "Run"}
                    </button>
                  </div>
                  {experimentPausedAtLimit && (
                    <p className="experiment-loop-resume">
                      New episode · pending watcher continues as invocation 1
                    </p>
                  )}
                  {experimentControl.reasons.length > 0 && (
                    <ul className="experiment-gate-reasons" aria-label="Run requirements">
                      {experimentControl.reasons.map((reason) => (
                        <li key={reason}>{reason}</li>
                      ))}
                    </ul>
                  )}
                  {experimentWatcherCount > 0 && onStopExperimentWatchers && (
                    <button
                      className="button compact experiment-stop-watchers"
                      type="button"
                      onClick={onStopExperimentWatchers}
                    >
                      Stop {experimentWatcherCount === 1 ? "watcher" : "watchers"}
                    </button>
                  )}
                  {(node.attempts ?? []).length > 0 && (
                    <ol className="experiment-attempts" aria-label="Attempts">
                      {(node.attempts ?? []).map((attempt) => {
                        const open = ["planned", "submitted", "running"].includes(attempt.status);
                        return (
                          <li key={attempt.id} className={open ? "open" : undefined}>
                            <span className="experiment-attempt-status">{attempt.status}</span>
                            <span>{attempt.purpose}</span>
                            {attempt.decision_bundle.length > 0 && (
                              <span className="experiment-attempt-pins">
                                {attempt.decision_bundle
                                  .map((pin) => pin.selected_option)
                                  .join(", ")}
                              </span>
                            )}
                          </li>
                        );
                      })}
                    </ol>
                  )}
                  {experimentControl.decision_drift.length > 0 && (
                    <ul className="experiment-decision-drift" aria-label="Decision drift">
                      {experimentControl.decision_drift.map((drift) => (
                        <li key={drift.decision_id}>
                          {drift.proposed
                            ? `${drift.decision_id} has a proposed change. This episode was pinned to ${drift.pinned_option}.`
                            : `${drift.decision_id} moved to ${drift.current_option ?? drift.current_status ?? "an unavailable state"} after this episode was pinned to ${drift.pinned_option}.`}
                        </li>
                      ))}
                    </ul>
                  )}
                </section>
              )}

              {presentation.context.length > 0 && (
                <section className="node-context">
                  <h3>Context</h3>
                  <dl className="context-list">
                    {presentation.context.map((item) => (
                      <div key={item.key}>
                        <dt>{item.label}</dt>
                        <dd>{formatValue(item.value, glossaryIndex)}</dd>
                      </div>
                    ))}
                  </dl>
                </section>
              )}

              {transitions.length > 0 && (
                <section className="belief-history">
                  <h3>Status history</h3>
                  <ol>
                    {transitions.map((transition) => {
                      const cause = beliefCausePresentation(transition, edges, allNodes);
                      return (
                        <li
                          key={`${transition.revision}-${transition.from_status}-${transition.to_status}`}
                        >
                          <span className="belief-transition">
                            <strong>{transition.from_status}</strong>
                            <span>→</span>
                            <strong>{transition.to_status}</strong>
                          </span>
                          <span className="mono">rev {transition.revision}</span>
                          {cause.nodeId ? (
                            <button type="button" onClick={() => onSelectNode(cause.nodeId!)}>
                              {cause.label}
                            </button>
                          ) : (
                            <span>{cause.label}</span>
                          )}
                        </li>
                      );
                    })}
                  </ol>
                </section>
              )}

              {Object.keys(node.extension_fields).length > 0 && (
                <section>
                  <h3>Extension fields</h3>
                  <dl className="detail-list">
                    {Object.entries(node.extension_fields).map(([key, value]) => (
                      <div key={key}>
                        <dt>{humanize(key)}</dt>
                        <dd>{formatValue(value, glossaryIndex)}</dd>
                      </div>
                    ))}
                  </dl>
                </section>
              )}

              <section>
                <div className="relations-control">
                  <button
                    className="relations-control-heading"
                    onClick={onExploreRelations}
                    aria-label={`Open DAG focused on relations for ${node.title}`}
                  >
                    <Network size={15} />
                    <strong>Relations</strong>
                    <small>{relations.length}</small>
                  </button>
                  {relations.map((edge) => {
                    const peerId = edge.source === node.id ? edge.target : edge.source;
                    const flags = edgeValidationFlags(edge.id, validationMessages);
                    return (
                      <div
                        className={`relation-row${flags.length > 0 ? " has-flag" : ""}`}
                        key={edge.id}
                      >
                        <button type="button" onClick={onExploreRelations}>
                          <span>
                            {edge.source === node.id ? edge.relation : `← ${edge.relation}`}
                          </span>
                          <strong>{allNodes[peerId]?.title ?? peerId}</strong>
                        </button>
                        {flags.map((flag) => (
                          <span
                            className="relation-flag"
                            role="status"
                            key={`${edge.id}-${flag.message}`}
                          >
                            <AlertTriangle size={12} />
                            {flag.message}
                          </span>
                        ))}
                      </div>
                    );
                  })}
                </div>
              </section>

              {details.length > 0 && (
                <section>
                  <h3>Record details</h3>
                  <dl className="detail-list">
                    {details.map(([key, value]) => (
                      <div key={key}>
                        <dt>{humanFieldLabels[key] ?? humanize(key)}</dt>
                        <dd>{formatValue(value, glossaryIndex)}</dd>
                      </div>
                    ))}
                  </dl>
                </section>
              )}

              <section>
                <h3>Conversation evidence</h3>
                {node.source_refs.length === 0 ? (
                  <p className="muted">No source excerpts attached.</p>
                ) : (
                  node.source_refs.map((source) => (
                    <blockquote key={`${source.session_id}-${source.record_uuid}`}>
                      <p>{source.excerpt}</p>
                      <footer className="mono">
                        {source.source} · {source.truth_repository} ·{" "}
                        {new Date(source.timestamp).toLocaleString()}
                      </footer>
                    </blockquote>
                  ))
                )}
              </section>
            </>
          )}
        </div>

        <footer className="drawer-actions">
          {editing ? (
            <>
              <span className="node-edit-status">
                {changeCount > 0
                  ? `${changeCount} field${changeCount === 1 ? "" : "s"}`
                  : "No changes"}
              </span>
              <div>
                <button className="button ghost" type="button" onClick={cancelEditing}>
                  Cancel
                </button>
                <button
                  className="button primary compact"
                  type="button"
                  disabled={nodeMutationDisabled || changeCount === 0 || editInvalid}
                  onClick={stage}
                >
                  <Check size={14} /> Done
                </button>
              </div>
            </>
          ) : stagedNewNode ? (
            <>
              <span className="node-edit-status">Staged node</span>
              <button
                className="button secondary compact"
                type="button"
                disabled={mutationsDisabled}
                onClick={onUnstage}
              >
                <Trash2 size={14} /> Remove
              </button>
            </>
          ) : (
            <>
              <button className="button ghost" onClick={onOpenChat}>
                <MessageCircle size={15} /> Ask about this node
              </button>
              <div className="node-detail-actions">
                <div className="node-judgment-actions">
                  <button
                    className="button secondary"
                    disabled={nodeMutationDisabled}
                    onClick={beginEditing}
                  >
                    <PencilLine size={14} /> Edit node
                  </button>
                  <button
                    className={`button judgment node-standing-toggle contest${node.standing === "contested" ? " selected disagree" : ""}`}
                    aria-pressed={node.standing === "contested"}
                    disabled={nodeMutationDisabled}
                    onClick={() =>
                      onStanding(node.standing === "contested" ? "asserted" : "contested")
                    }
                  >
                    {node.standing === "contested" ? <Check size={14} /> : <X size={14} />}
                    Contest
                  </button>
                  <button
                    className={`button judgment node-standing-toggle agree${node.standing === "accepted" ? " selected agree" : ""}`}
                    aria-pressed={node.standing === "accepted"}
                    disabled={nodeMutationDisabled}
                    onClick={() =>
                      onStanding(node.standing === "accepted" ? "asserted" : "accepted")
                    }
                  >
                    <Check size={14} />
                    Agree
                  </button>
                </div>
                <div className="node-removal-action">
                  <button
                    className="button danger"
                    type="button"
                    hidden={removalConfirmationOpen}
                    disabled={
                      mutationsDisabled ||
                      stagedForRemoval ||
                      Boolean(removalBlockedReason) ||
                      !onRemove
                    }
                    title={removalBlockedReason ?? undefined}
                    onClick={() => setRemovalConfirmationOpen(true)}
                  >
                    <Trash2 size={14} /> {stagedForRemoval ? "Removal staged" : "Remove node…"}
                  </button>
                  <div
                    className="node-removal-confirmation"
                    role="alert"
                    hidden={!removalConfirmationOpen}
                  >
                    <span>
                      Remove <strong>“{node.title}”</strong>? Sync will remove it and{" "}
                      {relations.length} connected relation{relations.length === 1 ? "" : "s"}.
                    </span>
                    <div>
                      <button
                        className="button compact"
                        type="button"
                        onClick={() => setRemovalConfirmationOpen(false)}
                      >
                        Cancel
                      </button>
                      <button
                        className="button danger compact"
                        type="button"
                        onClick={confirmRemoval}
                      >
                        Confirm remove
                      </button>
                    </div>
                  </div>
                  {removalBlockedReason && <small>{removalBlockedReason}</small>}
                </div>
              </div>
            </>
          )}
        </footer>
      </aside>
    </DraggableWindow>
  );
  return fullscreenTarget ? createPortal(drawer, fullscreenTarget) : drawer;
}

function hasValue(value: unknown): boolean {
  return (
    value !== null &&
    value !== undefined &&
    value !== "" &&
    !(Array.isArray(value) && value.length === 0)
  );
}

function nodeEditFieldError(field: NodeEditField, value: string): string | null {
  if (field.kind !== "number") return null;
  const number = Number(value);
  if (!Number.isFinite(number)) return "Enter a number.";
  if (field.integer && !Number.isInteger(number)) return "Enter a whole number.";
  if (field.min !== undefined && number < field.min) {
    return field.integer
      ? `Enter a whole number of at least ${field.min}.`
      : `Enter a number of at least ${field.min}.`;
  }
  return null;
}

function formatValue(value: unknown, glossaryIndex: GlossaryIndex): React.ReactNode {
  if (Array.isArray(value))
    return (
      <ul>
        {value.map((item, index) => (
          <li key={index}>{formatValue(item, glossaryIndex)}</li>
        ))}
      </ul>
    );
  if (typeof value === "object" && value !== null)
    return <pre>{JSON.stringify(value, null, 2)}</pre>;
  return <GlossaryText text={String(value)} glossaryIndex={glossaryIndex} />;
}
