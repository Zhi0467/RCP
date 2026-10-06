const reasons: Record<string, { label: string; reason: string; fix?: string }> = {
  ready: { label: "Ready", reason: "The browser is ready." },
  not_installed: {
    label: "Not installed",
    reason: "The browser is not installed.",
    fix: "Install it from the machine card.",
  },
  installing: {
    label: "Installing…",
    reason: "The browser is being installed on this machine.",
  },
  node_missing: {
    label: "Node missing",
    reason: "Node.js is missing.",
    fix: "Install Node 20+ and npm on this machine first, then check the machine card.",
  },
  node_too_old: {
    label: "Node too old",
    reason: "Node.js is too old.",
    fix: "Install Node 20+ and npm on this machine first, then check the machine card.",
  },
  npm_missing: {
    label: "npm missing",
    reason: "npm is missing.",
    fix: "Install Node 20+ and npm on this machine first, then check the machine card.",
  },
  system_libraries_missing: {
    label: "Libraries missing",
    reason: "Browser system libraries are missing.",
    fix: "Run the installation command shown on the machine card.",
  },
  unsupported_platform: {
    label: "Unsupported platform",
    reason: "This machine's platform is not supported.",
  },
  host_unreachable: {
    label: "Host unreachable",
    reason: "RCP cannot reach the execution machine.",
    fix: "Check the connection on the machine card.",
  },
  account_mismatch: {
    label: "Account mismatch",
    reason: "The machine's account does not match the browser owner.",
  },
  storage_unavailable: {
    label: "Storage unavailable",
    reason: "The browser's storage is unavailable.",
  },
  linger_disabled: {
    label: "Stops at logout",
    reason: "Background processes on this machine stop when its account logs out.",
    fix: "Allow background processes on the machine card in Settings.",
  },
  owner_unavailable: {
    label: "Process owner unavailable",
    reason: "The machine cannot keep a managed browser process running.",
  },
  cleanup_pending: {
    label: "Cleanup pending",
    reason: "A previous browser session is still waiting for cleanup.",
  },
  runtime_failed: { label: "Browser failed", reason: "The browser runtime failed." },
  capacity: {
    label: "All browsers busy",
    reason: "All managed browser sessions on this machine are busy.",
  },
  start_failed: { label: "Start failed", reason: "The browser could not start." },
  session_unreachable: {
    label: "Session unreachable",
    reason: "The browser session cannot be reached.",
  },
  owner_mismatch: {
    label: "Owner mismatch",
    reason: "The browser session belongs to a different owner.",
  },
  capability_denied: { label: "Not allowed", reason: "This turn is not allowed to use a browser." },
  admission_failed: {
    label: "Admission failed",
    reason: "RCP could not grant browser access to this turn.",
  },
  lease_unknown: {
    label: "Session no longer tracked",
    reason: "RCP can no longer track this turn's browser session.",
  },
  finish_failed: {
    label: "Final check failed",
    reason: "RCP could not finish checking this turn's browser session.",
  },
  lost: { label: "Connection lost", reason: "The browser connection was lost during this turn." },
  runtime_error: { label: "Browser error", reason: "The browser encountered a runtime error." },
  command_failed: { label: "Command failed", reason: "A browser command failed." },
};

export function browserReason(code: string | null | undefined) {
  return code && Object.hasOwn(reasons, code)
    ? { code, ...reasons[code] }
    : { code: "unknown", label: "Browser unavailable", reason: "The browser is unavailable." };
}
