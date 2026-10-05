import { createRoot } from "react-dom/client";
import { parseProjectSetupRoute } from "../../src/projects/projectSetup";
import { TransferProjectSetup } from "../../src/projects/TransferProjectSetup";
import "../../src/styles.css";

const route = parseProjectSetupRoute(window.location.hash);
if (route.kind !== "move") throw new Error("Expected the move route");
createRoot(document.getElementById("root")!).render(
  <TransferProjectSetup route={route} intentChooser={null} onCancel={() => {}} />,
);
