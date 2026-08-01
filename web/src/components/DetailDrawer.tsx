import { AlertTriangle, Check, MessageCircle, Network, PencilLine, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { DraggableWindow } from "./DraggableWindow";
import { changedNodeFields, editableNodeFields, nodeEditDraft } from "../nodeEditing";
import type { DraftNodeValue } from "../humanDraft";
import { beliefCausePresentation, edgeValidationFlags, nodeBeliefTransitions } from "../nodeDetail";
import {
  glossaryTermsForNode,
  humanFieldLabels,
  humanize,
  nodeTypeLabel,
  presentNode,
} from "../nodePresentation";
import type {
  BeliefTransition,
  Edge,
  GlossaryTerm,
  GraphNode,
  OntologyState,
  ValidationMessage,
} from "../types";

interface Props {
  node: GraphNode;
  edges: Edge[];
  allNodes: Record<string, GraphNode>;
  glossary: Record<string, GlossaryTerm>;
  beliefTransitions: BeliefTransition[];
  validationMessages: ValidationMessage[];
  ontology: OntologyState;
  mutationsDisabled?: boolean;
  stagedNewNode?: boolean;
  onUnstage?: () => void;
  onClose: () => void;
  onBeginEdit: () => void;
  onStanding: (standing: GraphNode["standing"]) => void;
  onStage: (changes: Record<string, DraftNodeValue>) => void;
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
  glossary,
  beliefTransitions,
  validationMessages,
  ontology,
  mutationsDisabled = false,
  stagedNewNode = false,
  onUnstage,
  onClose,
  onBeginEdit,
  onStanding,
  onStage,
  onOpenChat,
  onExploreRelations,
  onSelectNode,
}: Props) {
  const [editing, setEditing] = useState(false);
  const [editBase, setEditBase] = useState(node);
  const [draft, setDraft] = useState<Record<string, string>>(() => nodeEditDraft(node, ontology));
  const standingBeforeEdit = useRef<GraphNode["standing"] | null>(null);
  const editFields = useMemo(() => editableNodeFields(editBase, ontology), [editBase, ontology]);
  const changes = useMemo(
    () => changedNodeFields(editBase, draft, ontology),
    [draft, editBase, ontology],
  );
  const changeCount = Object.keys(changes).length;

  useEffect(() => {
    if (!editing) {
      setEditBase(node);
      setDraft(nodeEditDraft(node, ontology));
    }
  }, [editing, node, ontology]);

  useEffect(() => {
    if (mutationsDisabled) setEditing(false);
  }, [mutationsDisabled]);

  const beginEditing = () => {
    if (mutationsDisabled) return;
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
    if (changeCount === 0 || mutationsDisabled) return;
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
  const transitions = nodeBeliefTransitions(node.id, beliefTransitions);
  const presentation = presentNode(node);
  const definitions = glossaryTermsForNode(node, glossary);
  const presentedKeys = new Set([
    presentation.key,
    ...presentation.context.map((item) => item.key),
  ]);
  const details = Object.entries(node).filter(
    ([key, value]) => !ignored.has(key) && !presentedKeys.has(key) && hasValue(value),
  );
  const fullscreenTarget = typeof document === "undefined" ? null : document.fullscreenElement;
  const drawer = (
    <DraggableWindow className="node-detail-window" kind="detail">
      <aside
        className={`detail-drawer node-detail-drawer${node.draft_touched ? " draft-touched" : ""}`}
        role="dialog"
        aria-modal="false"
        aria-labelledby="drawer-title"
      >
        <header data-drag-handle="true">
          <div>
            <span className="eyebrow">{nodeTypeLabel(node)}</span>
            <h2 id="drawer-title">{node.title}</h2>
            <div className="node-meta">
              <span className="mono">{node.id}</span>
              <span className={`standing ${node.standing}`}>{node.standing}</span>
              {node.type === "evidence" && node.origin && (
                <span className="node-origin">{originLabels[node.origin]}</span>
              )}
            </div>
          </div>
          <button className="icon-button" aria-label="Close detail" onClick={close}>
            <X size={18} />
          </button>
        </header>

        <div className={`drawer-content${editing ? " editing" : ""}`}>
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
                    <input
                      type={field.kind === "number" ? "number" : "text"}
                      autoFocus={field.key === "title"}
                      value={draft[field.key] ?? ""}
                      onChange={(event) =>
                        setDraft((current) => ({ ...current, [field.key]: event.target.value }))
                      }
                    />
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
                <p>{formatValue(presentation.value)}</p>
              </section>

              {presentation.context.length > 0 && (
                <section className="node-context">
                  <h3>Context</h3>
                  <dl className="context-list">
                    {presentation.context.map((item) => (
                      <div key={item.key}>
                        <dt>{item.label}</dt>
                        <dd>{formatValue(item.value)}</dd>
                      </div>
                    ))}
                  </dl>
                </section>
              )}

              {definitions.length > 0 && (
                <section className="node-glossary">
                  <h3>Terms used here</h3>
                  <dl>
                    {definitions.map((term) => (
                      <div key={term.term}>
                        <dt>{term.term}</dt>
                        <dd>{term.plain_definition}</dd>
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
                        <dd>{formatValue(value)}</dd>
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
                        <dd>{formatValue(value)}</dd>
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
                  disabled={mutationsDisabled || changeCount === 0}
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
              <div>
                <button
                  className="button secondary"
                  disabled={mutationsDisabled}
                  onClick={beginEditing}
                >
                  <PencilLine size={14} /> Edit node
                </button>
                <button
                  className={`button judgment ${node.standing === "contested" ? "selected disagree" : ""}`}
                  aria-pressed={node.standing === "contested"}
                  disabled={mutationsDisabled}
                  onClick={() =>
                    onStanding(node.standing === "contested" ? "asserted" : "contested")
                  }
                >
                  <X size={14} /> Disagree
                </button>
                <button
                  className={`button judgment ${node.standing === "accepted" ? "selected agree" : ""}`}
                  aria-pressed={node.standing === "accepted"}
                  disabled={mutationsDisabled}
                  onClick={() => onStanding(node.standing === "accepted" ? "asserted" : "accepted")}
                >
                  <Check size={14} /> Agree
                </button>
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

function formatValue(value: unknown): React.ReactNode {
  if (Array.isArray(value))
    return (
      <ul>
        {value.map((item, index) => (
          <li key={index}>{formatValue(item)}</li>
        ))}
      </ul>
    );
  if (typeof value === "object" && value !== null)
    return <pre>{JSON.stringify(value, null, 2)}</pre>;
  return String(value);
}
