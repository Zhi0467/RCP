// Login rows as `GET /api/providers/logins` serves them, for one host.
export const HOST = "gpu-1.example";

export function loginRow(overrides = {}) {
  return {
    provider: "claude",
    label: "Claude",
    host: HOST,
    login_id: "",
    kind: "shared",
    owner_user_id: null,
    owner_display_name: null,
    added_by_display_name: null,
    added_at: null,
    limited_at: null,
    draining_since: null,
    bound_running: 0,
    machines: ["gpu-1"],
    state: "signed_in",
    generation: 1,
    detail: null,
    source: "verify",
    changed_at: "2026-10-28T09:00:00Z",
    changed_by: "u-me",
    sign_in_methods: ["token_entry"],
    managed: true,
    login_command: "claude setup-token",
    token_instructions: null,
    token: null,
    sign_in: null,
    provider_path: null,
    agent_used: true,
    ...overrides,
  };
}

export function signIn(overrides = {}) {
  return {
    attempt_id: "attempt-1",
    login_id: "",
    provider: "codex",
    host: HOST,
    state: "pending",
    user_code: "ABCD-1234",
    verification_url: "https://example.invalid/device",
    detail: null,
    started_at: "2026-10-28T09:00:00Z",
    started_by: "u-me",
    started_by_display_name: "Me",
    finished_at: null,
    resumed: null,
    ...overrides,
  };
}

const codex = { provider: "codex", label: "Codex", sign_in_methods: ["device_code"] };

/** Every card state on one machine, for the visual check. */
export const everyState = [
  loginRow({ kind: "own", login_id: "own-me", owner_user_id: "u-me", owner_display_name: "Me" }),
  loginRow({
    token: {
      pasted_at: "2026-10-01T10:00:00Z",
      pasted_by: "u-me",
      verified_at: "2026-10-28T09:00:00Z",
      estimated_expiry_at: null,
    },
  }),
  loginRow({
    login_id: "ana",
    added_by_display_name: "Ana",
    added_at: "2026-10-20T10:00:00Z",
    limited_at: "2026-10-28T08:00:00Z",
  }),
  loginRow({
    login_id: "bo",
    added_by_display_name: "Bo",
    added_at: "2026-10-21T10:00:00Z",
    state: "signed_out",
  }),
  loginRow({
    login_id: "cy",
    added_by_display_name: "Cy",
    added_at: "2026-10-22T10:00:00Z",
    draining_since: "2026-10-28T09:30:00Z",
    bound_running: 2,
  }),
  loginRow({
    kind: "own",
    login_id: "own-dee",
    owner_user_id: "u-dee",
    owner_display_name: "Dee",
    detail: null,
    source: null,
  }),
  loginRow({ ...codex, state: "signed_out", sign_in: signIn() }),
  loginRow({
    ...codex,
    login_id: "eve",
    added_by_display_name: "Eve",
    added_at: "2026-10-23T10:00:00Z",
    state: "signed_out",
    sign_in: signIn({
      attempt_id: "attempt-2",
      login_id: "eve",
      started_by: "u-eve",
      started_by_display_name: "Eve",
      user_code: null,
      verification_url: null,
    }),
  }),
  loginRow({
    provider: "opencode",
    label: "OpenCode",
    managed: false,
    sign_in_methods: [],
    login_command: "opencode auth login",
  }),
];

export const settingsFor = (claude = "own_then_shared", codexSetting = "shared") => ({
  settings: [
    { provider: "claude", host: HOST, setting: claude, changed_at: null, stored: true },
    { provider: "codex", host: HOST, setting: codexSetting, changed_at: null, stored: true },
  ],
  other_member_count: 4,
});
