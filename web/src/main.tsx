import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { installClientEventReporting } from "./core/clientEvents";
import { installLinkGuard } from "./core/linkGuard";
import { installPreloadRecovery, RootErrorBoundary } from "./rootRecovery";
import "katex/dist/katex.min.css";
import "./styles.css";

installClientEventReporting();
installPreloadRecovery();
installLinkGuard();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RootErrorBoundary>
      <App />
    </RootErrorBoundary>
  </StrictMode>,
);
