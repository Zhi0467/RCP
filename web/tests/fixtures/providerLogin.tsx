import { createRoot } from "react-dom/client";
import { ProviderLoginNotice } from "../../src/projects/ProviderLoginNotice";
import type { ProviderLoginState } from "../../src/core/types";
import "../../src/styles.css";

declare global {
  interface Window {
    openedSignIns: { provider: string; host: string }[];
  }
}

window.openedSignIns = [];

const row = (patch: Partial<ProviderLoginState>): ProviderLoginState => ({
  provider: "codex",
  label: "Codex",
  host: "",
  login_id: "",
  kind: "shared",
  owner_user_id: null,
  owner_display_name: null,
  added_by_display_name: null,
  added_at: null,
  limited_at: null,
  draining_since: null,
  bound_running: 0,
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
    onOpenSignIns={({ provider, host }) => window.openedSignIns.push({ provider, host })}
    states={[
      row({}),
      row({
        provider: "claude",
        label: "Claude",
        host: "gpu-1.example",
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
