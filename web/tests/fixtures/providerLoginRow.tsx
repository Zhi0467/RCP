import { createRoot } from "react-dom/client";
import { ProviderLoginRow } from "../../src/components/ProviderLogins";
import "../../src/styles.css";

declare global {
  interface Window {
    loginChanges: number;
  }
}

window.loginChanges = 0;

createRoot(document.getElementById("root")!).render(
  <ProviderLoginRow
    account={{
      provider: "codex",
      label: "Codex on this machine",
      host: "",
      machines: [],
      state: "signed_out",
      generation: 0,
      changed_at: "2026-09-14T00:00:00Z",
      changed_by: null,
      detail: null,
      source: "turn",
      token: null,
      sign_in: {
        login_id: "login-1",
        provider: "codex",
        host: "",
        state: "pending",
        user_code: "ABCD-1234",
        verification_url: "https://example.invalid/device",
        detail: null,
        started_at: "2026-09-14T00:00:00Z",
      },
      sign_in_methods: ["device_code"],
      token_instructions: null,
    }}
    spaceKind="personal"
    writesDisabled={false}
    memberName={(id) => id}
    onChanged={async () => {
      window.loginChanges += 1;
    }}
  />,
);
