import React from "react";
import { createRoot } from "react-dom/client";
import { Artifacts } from "../../src/views/Artifacts";
import { ArtifactViewer } from "../../src/components/ArtifactViewer";
import "../../src/styles.css";
createRoot(document.getElementById("root")!).render(
  <>
    <Artifacts projectId="project" />
    <ArtifactViewer />
  </>,
);
