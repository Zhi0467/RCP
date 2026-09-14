import { createRoot } from "react-dom/client";
import { ProviderLoginNotice } from "../../src/components/ProviderLoginNotice";
import "../../src/styles.css";

createRoot(document.getElementById("root")!).render(
  <ProviderLoginNotice
    states={[
      {
        provider: "codex",
        host: "",
        state: "signed_out",
        generation: 0,
        changed_at: "2026-09-14T00:00:00Z",
        detail: "Please sign in again",
        source: "turn",
        changed_by: null,
      },
    ]}
  />,
);
