import { useState } from "react";
import { createRoot } from "react-dom/client";
import { DetailDrawer } from "../../src/graph/DetailDrawer";
import { buildGlossaryIndex } from "../../src/graph/glossary";
import type { GraphNode, NewNode, OntologyState } from "../../src/core/types";
import "../../src/styles.css";
const ontology: OntologyState = {
  types: [],
  relations: [],
  fields: [
    {
      owner_type: "evidence",
      name: "score",
      kind: "number",
      required: true,
      deprecated: false,
      definition: "",
      agent_writable: true,
    },
  ],
};
const noop = () => {};
function Fixture() {
  const [saved, setSaved] = useState<NewNode | null>(null);
  const [removed, setRemoved] = useState(false);
  const node = {
    id: "ev/new",
    type: "evidence",
    title: "New observation",
    observation: "New observation",
    origin: "unknown",
    extension_type: null,
    extension_fields: { score: 1 },
    standing: "asserted",
    source_refs: [],
    created_rev: 0,
    updated_rev: 0,
    ...saved,
  } as GraphNode;
  return (
    <>
      {!removed && (
        <DetailDrawer
          node={node}
          allNodes={{ [node.id]: node }}
          edges={[]}
          glossaryIndex={buildGlossaryIndex({})}
          beliefTransitions={[]}
          validationMessages={[]}
          ontology={ontology}
          detailSlot="original"
          stagedNewNode
          onUpdateStagedNode={setSaved}
          onUnstage={() => setRemoved(true)}
          onClose={noop}
          onDock={noop}
          onBeginEdit={noop}
          onStanding={noop}
          onStage={noop}
          onOpenChat={noop}
          onOpenRelatedNode={noop}
          onSelectNode={noop}
        />
      )}
      <output data-testid="saved">{JSON.stringify(saved)}</output>
    </>
  );
}
createRoot(document.getElementById("root")!).render(<Fixture />);
