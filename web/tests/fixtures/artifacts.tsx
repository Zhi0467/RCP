import React from "react";
import { createRoot } from "react-dom/client";
import { Artifacts } from "../../src/views/Artifacts";
import { RunArtifacts } from "../../src/components/RunArtifacts";
import type { RunArtifactEntry } from "../../src/types";
import { ArtifactViewer } from "../../src/components/ArtifactViewer";
import "../../src/styles.css";
const root = createRoot(document.getElementById("root")!);
root.render(
  <>
    <Artifacts projectId="project" />
    <ArtifactViewer />
  </>,
);

export function renderRunArtifacts(artifacts: RunArtifactEntry[]) {
  root.render(<RunArtifacts projectId="project" artifacts={artifacts} />);
}
