import { AlertTriangle, Check, Link2, RotateCcw, Trash2, X } from "lucide-react";
import { memo, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { errorMessage } from "../errors";
import type {
  Edge,
  EvidenceAssessment,
  GraphEditOptions,
  NewNode,
  NewEdge,
  GraphState,
} from "../types";
import { humanize } from "../nodePresentation";
import { NewCustomNode } from "./NewCustomNode";

export interface GraphEditingProps {
  projectId: string;
  mutationsDisabled?: boolean;
  onStageCustomNode: (node: NewNode) => void;
  onStageEdge: (edge: NewEdge) => void;
  onRemoveEdge: (edgeId: string) => void;
  onUndoRemoveEdge: (edgeId: string) => void;
  draftAddedEdges?: NewEdge[];
  draftRemovedEdgeIds?: string[];
  draftRemovedNodeIds?: string[];
  draftAddedNodeIds?: string[];
  canonicalEdges?: Record<string, Edge>;
}

const NO_EDGES: NewEdge[] = [];
const NO_IDS: string[] = [];
const NO_CANONICAL_EDGES: Record<string, Edge> = {};

export const GraphEditingControls = memo(function GraphEditingControls({
  graph,
  projectId,
  mutationsDisabled = false,
  onStageCustomNode,
  onStageEdge,
  onRemoveEdge,
  onUndoRemoveEdge,
  draftAddedEdges = NO_EDGES,
  draftRemovedEdgeIds = NO_IDS,
  draftRemovedNodeIds = NO_IDS,
  draftAddedNodeIds = NO_IDS,
  canonicalEdges = NO_CANONICAL_EDGES,
  connection,
}: GraphEditingProps & {
  graph: GraphState;
  connection?: { source: string; target: string } | null;
}) {
  const [open, setOpen] = useState(false);
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [relation, setRelation] = useState("");
  const [explanation, setExplanation] = useState("");
  const [options, setOptions] = useState<GraphEditOptions | null>(null);
  const [relevance, setRelevance] = useState<EvidenceAssessment["relevance"] | "">("");
  const [weight, setWeight] = useState<EvidenceAssessment["weight"] | "">("");
  const [qualifications, setQualifications] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  useEffect(() => {
    setRelevance("");
    setWeight("");
    setQualifications("");
  }, [projectId, source, target, relation]);
  useEffect(() => {
    if (!connection) return;
    setSource(connection.source);
    setTarget(connection.target);
    setOpen(true);
  }, [connection]);
  useEffect(() => {
    let cancelled = false;
    setError(null);
    setOptions(null);
    void api<GraphEditOptions>(`/api/projects/${projectId}/graph-edit-options`).then(
      (options) => {
        if (!cancelled) setOptions(options);
      },
      (failure) => {
        if (!cancelled) setError(errorMessage(failure));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [projectId, reloadToken]);
  const removedNodeIds = useMemo(() => new Set(draftRemovedNodeIds), [draftRemovedNodeIds]);
  const removedEdgeIds = useMemo(() => new Set(draftRemovedEdgeIds), [draftRemovedEdgeIds]);
  const existingNodeIds = useMemo(
    () => new Set([...Object.keys(graph.nodes), ...draftAddedNodeIds]),
    [graph.nodes, draftAddedNodeIds],
  );
  const addedEdgeIds = useMemo(
    () => new Set(draftAddedEdges.map((edge) => edge.id)),
    [draftAddedEdges],
  );
  const nodes = useMemo(
    () => (open ? Object.values(graph.nodes).filter((node) => !removedNodeIds.has(node.id)) : []),
    [open, graph.nodes, removedNodeIds],
  );
  const nodeOptions = useMemo(
    () =>
      nodes.map((node) => (
        <option key={node.id} value={node.id}>
          {node.title} · {node.id}
        </option>
      )),
    [nodes],
  );
  const relations = useMemo(
    () => [
      ...(options?.relations ?? []),
      ...graph.ontology.relations
        .filter((item) => !item.deprecated)
        .map((item) => ({
          name: item.name,
          assessment_required_for: [],
        })),
    ],
    [options, graph.ontology.relations],
  );
  const relationOptions = useMemo(
    () =>
      relations.map(({ name }) => (
        <option key={name} value={name}>
          {humanize(name)}
        </option>
      )),
    [relations],
  );
  const hasEndpoints =
    Boolean(graph.nodes[source] && graph.nodes[target]) &&
    !removedNodeIds.has(source) &&
    !removedNodeIds.has(target);
  const selectedRelation = relations.find((item) => item.name === relation);
  const needsAssessment =
    selectedRelation?.assessment_required_for.some(
      (pair) =>
        graph.nodes[source]?.type === pair.source_type &&
        graph.nodes[target]?.type === pair.target_type,
    ) ?? false;
  const canStage =
    !mutationsDisabled &&
    !error &&
    options !== null &&
    hasEndpoints &&
    Boolean(selectedRelation) &&
    (!needsAssessment || Boolean(relevance && weight));
  const edges = useMemo(() => {
    if (!open || (!source && !target)) return [];
    // Only canonical edges and this human's additions can be removed. Keep
    // removed canonical entries here so undo remains available after preview.
    return Object.values({
      ...canonicalEdges,
      ...Object.fromEntries(draftAddedEdges.map((edge) => [edge.id, edge])),
    }).filter(
      (edge) =>
        edge.source === source ||
        edge.target === source ||
        edge.source === target ||
        edge.target === target,
    );
  }, [open, source, target, canonicalEdges, draftAddedEdges]);
  return (
    <div className="graph-editing-controls">
      <NewCustomNode
        ontology={graph.ontology}
        nodePrefixes={options?.node_prefixes ?? null}
        // A node id is derived from the backend prefixes, so without them the
        // form could be filled in but never staged. Keep it closed instead.
        disabled={mutationsDisabled || !options}
        existingNodeIds={existingNodeIds}
        onStage={onStageCustomNode}
      />
      {error && (
        <p className="inline-failure" role="alert">
          <AlertTriangle size={13} aria-hidden="true" />
          <span>
            Connections and node prefixes are unavailable, so connecting nodes and creating new ones
            stay unavailable until this loads. {error}
          </span>
          <button
            className="button secondary compact"
            type="button"
            onClick={() => setReloadToken((token) => token + 1)}
          >
            Retry
          </button>
        </p>
      )}
      {!open ? (
        <button
          className="button secondary compact"
          type="button"
          disabled={mutationsDisabled}
          onClick={() => setOpen(true)}
        >
          <Link2 size={14} /> Connections
        </button>
      ) : (
        <section className="new-custom-node graph-connections" aria-label="Graph connections">
          <header>
            <strong>Connections</strong>
            <button
              className="icon-button"
              type="button"
              aria-label="Close connections"
              onClick={() => setOpen(false)}
            >
              <X size={14} />
            </button>
          </header>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (!canStage) return;
              onStageEdge({
                id: `edge/${crypto.randomUUID()}`,
                source,
                target,
                relation,
                explanation: explanation.trim(),
                ...(needsAssessment && relevance && weight
                  ? {
                      assessment: {
                        relevance,
                        weight,
                        qualifications: [
                          ...new Set(
                            qualifications
                              .split("\n")
                              .map((line) => line.trim())
                              .filter(Boolean),
                          ),
                        ],
                      },
                    }
                  : {}),
              });
              setExplanation("");
            }}
          >
            <div className="new-custom-node-grid">
              <label>
                From
                <select
                  aria-label="From"
                  value={source}
                  disabled={mutationsDisabled}
                  onChange={(event) => setSource(event.target.value)}
                  required
                >
                  <option value="">Choose node</option>
                  {nodeOptions}
                </select>
              </label>
              <label>
                Relation
                <select
                  aria-label="Relation"
                  value={relation}
                  disabled={mutationsDisabled || Boolean(error)}
                  onChange={(event) => setRelation(event.target.value)}
                  required
                >
                  <option value="">Choose relation</option>
                  {relationOptions}
                </select>
              </label>
              <label>
                To
                <select
                  aria-label="To"
                  value={target}
                  disabled={mutationsDisabled}
                  onChange={(event) => setTarget(event.target.value)}
                  required
                >
                  <option value="">Choose node</option>
                  {nodeOptions}
                </select>
              </label>
              <label className="wide">
                Explanation
                <textarea
                  value={explanation}
                  rows={2}
                  disabled={mutationsDisabled}
                  onChange={(event) => setExplanation(event.target.value)}
                />
              </label>
              {needsAssessment && (
                <>
                  <label>
                    Relevance
                    <select
                      aria-label="Relevance"
                      required
                      value={relevance}
                      disabled={mutationsDisabled}
                      onChange={(event) =>
                        setRelevance(event.target.value as EvidenceAssessment["relevance"])
                      }
                    >
                      <option value="">Choose relevance</option>
                      <option value="direct">Direct</option>
                      <option value="indirect">Indirect</option>
                      <option value="contextual">Contextual</option>
                    </select>
                  </label>
                  <label>
                    Weight
                    <select
                      aria-label="Weight"
                      required
                      value={weight}
                      disabled={mutationsDisabled}
                      onChange={(event) =>
                        setWeight(event.target.value as EvidenceAssessment["weight"])
                      }
                    >
                      <option value="">Choose weight</option>
                      <option value="limited">Limited</option>
                      <option value="moderate">Moderate</option>
                      <option value="strong">Strong</option>
                    </select>
                  </label>
                  <label className="wide">
                    Qualifications
                    <textarea
                      aria-label="Qualifications"
                      rows={2}
                      value={qualifications}
                      disabled={mutationsDisabled}
                      onChange={(event) => setQualifications(event.target.value)}
                    />
                  </label>
                </>
              )}
              <button className="button primary compact" type="submit" disabled={!canStage}>
                <Check size={14} /> Stage connection
              </button>
            </div>
          </form>
          <ul className="graph-connection-list">
            {edges.map((edge) => {
              const removed = removedEdgeIds.has(edge.id) && !addedEdgeIds.has(edge.id);
              return (
                <li key={edge.id} className={removed ? "is-removed" : ""}>
                  <span>
                    {graph.nodes[edge.source]?.title ?? edge.source} →{" "}
                    <strong>{humanize(edge.relation)}</strong> →{" "}
                    {graph.nodes[edge.target]?.title ?? edge.target}
                  </span>
                  <button
                    className="button secondary compact"
                    type="button"
                    disabled={mutationsDisabled}
                    aria-label={`${removed ? "Undo removal of" : "Remove"} ${edge.relation} connection from ${edge.source} to ${edge.target}`}
                    onClick={() => (removed ? onUndoRemoveEdge(edge.id) : onRemoveEdge(edge.id))}
                  >
                    {removed ? <RotateCcw size={14} /> : <Trash2 size={14} />}
                    {removed ? "Undo" : "Remove"}
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
});
