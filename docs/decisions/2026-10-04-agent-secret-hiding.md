# Selected agent secrets are usable but not readable

Date: 2026-10-04

## Decision

Protect selected secrets against a generic network attacker whose untrusted web
content prompts an agent to read and exfiltrate credentials. Keep network access;
hide selected secret paths on supported launches and allow authentication through
confirmed SSH-agent signing. Research data remains readable. This is not targeted
RCP-content defense, same-account isolation, or a claim that private data is gone.

Do not add an egress filter, separate OS account, or RCP-managed full sandbox.
Agents retain SSH, Slurm, GPUs, systemd/launchctl, browser, and Git. Deliberate
escapes through process managers, localhost SSH, or scheduler jobs are outside
this threat model. Browser and Git capability never shrinks.

One resolved provider-neutral scope owns the paths, environment allow list,
effective status, key evidence, and fingerprint. Code-owned defaults cannot be
removed in Settings. Any member may add machine hidden folders; the backend
rejects overlap with required checkouts, stages, tools, sockets, and readable
keys, and checks again at launch. Tool-call environments use an allow list;
provider processes keep their own authentication outside the wrapper.

Private SSH identities and deploy keys are hidden only when their decoded
public-key SHA256 fingerprint is listed by the correct host's agent and bounded
signing succeeds. Missing or stale public keys and failed checks mean readable
fallback with a visible reason. Public keys, SSH configuration, and known hosts
remain readable; parents containing exempt keys are never masked. The backend
owns one deploy-key agent per account at a private stable socket under `~/.rcp`,
separate from the user's agent. Remote deploy keys remain readable with warnings.

Linux runs browser daemons inside the same hiding policy, including inside the
systemd job rather than around its launcher. Policy changes preserve profiles
and leases. macOS keeps the browser unwrapped because Chromium's sandbox cannot
nest inside Seatbelt; browser file operations remain unhidden and visibly so.

The macOS Keychain stays open. RCP items trusting Apple's `security` tool remain
readable, but those credentials serve the loopback backend, tailnet-only team
server, or RCP window and have limited value to a remote attacker. Blocking the
Keychain breaks `gh` and Keychain-backed Git. Developer ID signing with
app-scoped items is a future fix. Files that tools read directly, including
`~/.config/gh`, `~/.netrc`, and `~/.aws`, are not default denies; users may add
credential folders themselves.

## Consequences

When support is missing or user namespaces are blocked, launch unhidden and
show the reason in the turn, Settings, and doctor. Never block a launch for
hiding availability. An uncovered provider-native read tool also reports
unhidden. The promise is selected secrets unreadable on supported launches,
with compatibility exceptions visible rather than silently breaking work.
