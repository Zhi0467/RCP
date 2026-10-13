import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { PinnedView, PinnedViewTab } from "../../src/graph/PinnedView";
import { researchViewsProfile } from "../../src/graph/researchViewResources";
import { useResource } from "../../src/core/useResource";
import { ResourceRuntime, activateProject } from "./resourceRuntime";
import { ArtifactViewer } from "../../src/artifacts/ArtifactViewer";
import "../../src/styles.css";
activateProject("project");
function Fixture() {
  const [active, select] = useState<string | null>(null);
  const pins = useResource("pins", researchViewsProfile("project", null)).value?.pins ?? [];
  const pin = pins.find((p) => p.artifact_id === active);
  return (
    <>
      <div className="research-subpanel" role="tablist">
        <button role="tab" aria-selected={!pin} onClick={() => select(null)}>
          Research
        </button>
        <button role="tab">DAG</button>
        {pins.map((p) => (
          <PinnedViewTab
            key={p.artifact_id}
            projectId="project"
            branchId={null}
            pin={p}
            active={active === p.artifact_id}
            onSelect={() => select(p.artifact_id)}
            onUnpin={() => select(null)}
          />
        ))}
      </div>
      {pin && (
        <PinnedView
          projectId="project"
          branchId={null}
          pin={pin}
          ready
          nodes={{ n: {} }}
          onOpenNode={(id) => (document.body.dataset.node = id)}
        />
      )}
      <ArtifactViewer />
    </>
  );
}
createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    <Fixture />
  </ResourceRuntime>,
);
