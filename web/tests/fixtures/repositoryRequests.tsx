import { createRoot } from "react-dom/client";
import { RepositoryRequests } from "../../src/projects/RepositoryRequests";
import { TeamProjectSetup } from "../../src/projects/TeamProjectSetup";
import "../../src/styles.css";

const project = {
  id: "project",
  name: "Plasticity",
  repositories: [{ alias: "state" }],
};
const root = createRoot(document.getElementById("root")!);
function render() {
  root.render(
    location.hash.includes("request=") || location.search === "?setup" ? (
      <TeamProjectSetup intentChooser={null} onCreated={() => {}} onCancel={() => {}} />
    ) : (
      <RepositoryRequests
        project={project}
        spaceKind="team"
        baseMachineId="base"
        disabled={false}
        connectAlias={location.search === "?connect" ? "state" : null}
        onCloseConnect={() => {}}
        onAdded={() => {}}
      />
    ),
  );
}
window.addEventListener("hashchange", render);
render();
