import { ResourceRuntime, activateProject } from "./resourceRuntime";
import { createRoot } from "react-dom/client";
import { BulletinPanel } from "../../src/chat/BulletinPanel";
import { ArtifactViewer } from "../../src/artifacts/ArtifactViewer";
import "../../src/styles.css";

activateProject("project");
createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    <main style={{ height: "100vh" }}>
      <BulletinPanel projectId="project" target={{ kind: "main" }} actorId="member" />
    </main>
    <ArtifactViewer />
  </ResourceRuntime>,
);
