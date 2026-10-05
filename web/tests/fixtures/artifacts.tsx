import React from "react";
import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";
import { Artifacts } from "../../src/artifacts/Artifacts";
import { RunArtifacts } from "../../src/experiments/RunArtifacts";
import type { RunArtifactEntry } from "../../src/core/types";
import { ArtifactViewer } from "../../src/artifacts/ArtifactViewer";
import "../../src/styles.css";
const root = createRoot(document.getElementById("root")!);
const nodeTitles: Record<string, string> = { "exp/transfer": "Transfer experiment" };
const testWindow = window as unknown as { openedNodes: string[] };
testWindow.openedNodes = [];

function renderArtifacts() {
  root.render(
    <>
      <Artifacts
        projectId="project"
        nodeTitle={(nodeId) => nodeTitles[nodeId] ?? null}
        onOpenNode={(nodeId) => testWindow.openedNodes.push(nodeId)}
      />
      <ArtifactViewer />
    </>,
  );
}
renderArtifacts();

/** Leaves and reopens the view, as switching project tabs does. */
export function remountArtifacts() {
  flushSync(() => root.render(<></>));
  renderArtifacts();
}

export function renderRunArtifacts(artifacts: RunArtifactEntry[]) {
  root.render(<RunArtifacts projectId="project" artifacts={artifacts} />);
}
