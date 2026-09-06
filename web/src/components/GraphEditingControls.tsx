import { Check, Link2, RotateCcw, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import type { Edge, EvidenceAssessment, GraphEditOptions, GraphNode, GraphState } from "../types";
import { NewCustomNode } from "./NewCustomNode";

export interface GraphEditingProps {
  projectId: string;
  mutationsDisabled?: boolean;
  onStageCustomNode: (node: GraphNode) => void;
  onStageEdge: (edge: Edge) => void;
  onRemoveEdge: (edgeId: string) => void;
  onUndoRemoveEdge: (edgeId: string) => void;
  draftAddedEdges?: Edge[];
  draftRemovedEdgeIds?: string[];
  canonicalEdges?: Record<string, Edge>;
}

export function GraphEditingControls({
  graph,
  projectId,
  mutationsDisabled = false,
  onStageCustomNode,
  onStageEdge,
  onRemoveEdge,
  onUndoRemoveEdge,
  draftAddedEdges = [],
  draftRemovedEdgeIds = [],
  canonicalEdges = {},
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
  const [relations, setRelations] = useState<GraphEditOptions["relations"]>([]);
  const [relevance, setRelevance] = useState<EvidenceAssessment["relevance"] | "">("");
  const [weight, setWeight] = useState<EvidenceAssessment["weight"] | "">("");
  const [qualifications, setQualifications] = useState("");
  const [error, setError] = useState<string | null>(null);
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
    if (!open) return;
    let cancelled = false;
    setError(null);
    setRelations([]);
    void api<GraphEditOptions>(`/api/projects/${projectId}/graph-edit-options`).then(
      (options) => {
        if (!cancelled) setRelations(options.relations);
      },
      (failure) => {
        if (!cancelled) setError(String(failure));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [open, projectId, graph.revision]);
  const nodes = Object.values(graph.nodes);
  const hasEndpoints = Boolean(graph.nodes[source] && graph.nodes[target]);
  const selectedRelation = relations.find((item) => item.name === relation);
  const needsAssessment =
    selectedRelation?.assessment_required_for.some(
      (pair) =>
        graph.nodes[source]?.type === pair.source_type &&
        graph.nodes[target]?.type === pair.target_type,
    ) ?? false;
  const edges = Object.values({
    ...canonicalEdges,
    ...graph.edges,
    ...Object.fromEntries(draftAddedEdges.map((edge) => [edge.id, edge])),
  });
  return (
    <div className="graph-editing-controls">
      <NewCustomNode
        ontology={graph.ontology}
        disabled={mutationsDisabled}
        existingNodeIds={new Set(Object.keys(graph.nodes))}
        onStage={onStageCustomNode}
      />
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
              if (
                mutationsDisabled ||
                !hasEndpoints ||
                !selectedRelation ||
                (needsAssessment && (!relevance || !weight))
              )
                return;
              // The backend resolves the actual relation layer during preview and Sync.
              onStageEdge({
                id: `edge/${crypto.randomUUID()}`,
                source,
                target,
                relation,
                layer: "meta",
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
                  {nodes.map((node) => (
                    <option key={node.id} value={node.id}>
                      {node.title} · {node.id}
                    </option>
                  ))}
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
                  {relations.map(({ name }) => (
                    <option key={name} value={name}>
                      {name.replaceAll("_", " ")}
                    </option>
                  ))}
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
                  {nodes.map((node) => (
                    <option key={node.id} value={node.id}>
                      {node.title} · {node.id}
                    </option>
                  ))}
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
              <button
                className="button primary compact"
                type="submit"
                disabled={
                  mutationsDisabled ||
                  Boolean(error) ||
                  !hasEndpoints ||
                  !selectedRelation ||
                  (needsAssessment && (!relevance || !weight))
                }
              >
                <Check size={14} /> Stage connection
              </button>
            </div>
          </form>
          {error && <p role="alert">Could not load relations: {error}</p>}
          <ul className="graph-connection-list">
            {edges.map((edge) => {
              const removed = draftRemovedEdgeIds.includes(edge.id);
              return (
                <li key={edge.id} className={removed ? "is-removed" : ""}>
                  <span>
                    {graph.nodes[edge.source]?.title ?? edge.source} →{" "}
                    <strong>{edge.relation.replaceAll("_", " ")}</strong> →{" "}
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
}
