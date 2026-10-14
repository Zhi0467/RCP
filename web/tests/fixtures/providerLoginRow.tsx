import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { ProviderLoginRow } from "../../src/projects/ProviderLogins";
import type { ProviderLoginAccount } from "../../src/core/types";
import "../../src/styles.css";

declare global {
  interface Window {
    loginChanges: number;
    setLoginAccount: (patch: Partial<ProviderLoginAccount>) => void;
    rerenderEvery: (ms: number) => void;
  }
}

window.loginChanges = 0;

const initial: ProviderLoginAccount = {
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
  managed: true,
  login_command: "codex login",
  token_instructions: null,
};

function Harness() {
  const [account, setAccount] = useState(initial);
  const [rerenderMs, setRerenderMs] = useState(0);
  const [, setTick] = useState(0);
  window.setLoginAccount = (patch) => setAccount((current) => ({ ...current, ...patch }));
  // Stands in for the app's once-a-second scheduler re-render.
  window.rerenderEvery = setRerenderMs;
  useEffect(() => {
    if (!rerenderMs) return;
    const timer = setInterval(() => setTick((tick) => tick + 1), rerenderMs);
    return () => clearInterval(timer);
  }, [rerenderMs]);
  return (
    <ProviderLoginRow
      account={account}
      spaceKind="personal"
      writesDisabled={false}
      memberName={(id) => id}
      onChanged={async () => {
        window.loginChanges += 1;
      }}
    />
  );
}

createRoot(document.getElementById("root")!).render(<Harness />);
