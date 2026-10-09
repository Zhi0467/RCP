import { createRoot } from "react-dom/client";
import { RepositoryRequests } from "../../src/projects/RepositoryRequests";
import { TeamProjectSetup } from "../../src/projects/TeamProjectSetup";
import "../../src/styles.css";

const project = {
  id: "project",
  machines: [{ alias: "server" }],
  repositories: [
    { alias: "state", source: "server_only", can_connect: true },
    { alias: "code", source: "github", github_identity: "lab/code", can_connect: false },
    { alias: "locked", source: "server_only", can_connect: false },
  ],
};
const root = createRoot(document.getElementById("root")!);
function render() {
  root.render(
    location.hash.includes("request=") || location.search ? (
      <TeamProjectSetup intentChooser={null} onCreated={() => {}} onCancel={() => {}} />
    ) : (
      <RepositoryRequests project={project} disabled={false} />
    ),
  );
}
window.addEventListener("hashchange", render);
render();
