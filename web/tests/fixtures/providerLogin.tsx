import { createRoot } from "react-dom/client";
import { ProviderLoginNotice } from "../../src/projects/ProviderLoginNotice";
import { loginRow } from "./signInRows.mjs";
import "../../src/styles.css";

declare global {
  interface Window {
    openedSignIns: { provider: string; machine_id: string }[];
  }
}

window.openedSignIns = [];

// A signed-out shared Codex login on the backend machine.
const row = (patch = {}) =>
  loginRow({
    provider: "codex",
    label: "Codex",
    machine_id: "m-backend",
    state: "signed_out",
    generation: 0,
    changed_at: "2026-09-14T00:00:00Z",
    detail: "Please sign in again",
    source: "turn",
    changed_by: null,
    ...patch,
  });

createRoot(document.getElementById("root")!).render(
  <ProviderLoginNotice
    viewerUserId="u-me"
    onOpenSignIns={({ provider, machine_id }) =>
      window.openedSignIns.push({ provider, machine_id })
    }
    states={[
      row({}),
      row({
        provider: "claude",
        label: "Claude",
        machine_id: "m-gpu-1",
        login_id: "own-dee",
        kind: "own",
        owner_user_id: "u-dee",
        owner_display_name: "Dee",
        detail: null,
        source: null,
      }),
    ]}
  />,
);
