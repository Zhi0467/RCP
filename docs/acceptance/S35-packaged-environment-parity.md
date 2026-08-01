---
id: S35-packaged-environment-parity
status: blocked-external
tier: packaged + live
driver: desktop
covered_by: none
requires: a signed application bundle, a clean macOS user account, a reachable SSH host, and Codex or Claude installed for that account
invariants: [4, 6]
---

# RCP knows where your tools are, and you can see and correct it

An agent CLI the human can run in Terminal is an agent CLI RCP can launch. A
state repository they can read is one RCP can read. An SSH host they can reach is
one RCP can reach. None of that is automatic for an application launched from the
Dock.

The obvious mechanism — repair the environment before the backend starts — is
unbounded. Provider discovery uses `shutil.which`, and a Dock-launched
application does not inherit the shell environment Terminal does. If `codex` came
from nvm, mise, asdf, or direnv it lives behind a shell function or a
version-scoped directory that exists only after shell init runs, so "repair the
environment" really means "execute the user's login shell and harvest its
result," which differs per terminal and per directory.

So the promise is not that the app reconstructs your shell. It is that **RCP
resolves each provider once, records the absolute path, and shows it to you.**
Margin made the same choice: it records an absolute Node path in its bundle
rather than reconstructing an environment. Every `ProviderProfile` method in
[providers.py](../../src/rcp/providers.py) already takes a `binary` argument, so
only discovery is hardcoded to `PATH` — the plumbing is there.

This also unifies local and remote, which currently disagree. Remote discovery
runs through `bash -lic` and gets the login environment for free; local discovery
gets nothing. One recorded path per provider per machine covers both.

Beyond finding binaries, a signed application is subject to macOS privacy
controls a terminal session normally is not. Project directories under Documents,
Desktop, or iCloud may require consent or be denied outright, and subprocesses
RCP launches inherit the application's access rather than the human's. SSH keys
in the keychain and the agent socket are the same class of question. This
scenario does not predict which of those fail — the only way to know is to run
the signed application under an account that has granted it nothing, and RCP has
a rule against repeating an unverified blocker as if it were a finding.

## UI path

Confirmed with the human on 2026-07-31.

- **A recorded path per provider per machine**, visible and editable wherever
  machines are configured. `MachineConfig` in
  [config.py](../../src/rcp/config.py) is a shared contract, so this field lands
  serially before anything consuming it.
- **Environment repair is demoted to a discovery aid.** It runs before the
  backend starts, so every subprocess sees the same corrected environment, but
  nothing depends on it having worked — it only makes the first resolution more
  likely to succeed.
- **A third readiness state.** The code already separates "not installed" from
  "unreachable". Add "recorded at this path, not found", with a re-resolve
  action. This is what a version manager switching versions looks like, and it
  should be visible rather than silently retried.
- **A denied directory says it was denied**, and is never reported as a missing
  binary or an unreachable host.
- **First-launch consent is explained** before macOS asks for it.
- Any project location is supported; a denial is explained rather than
  pre-empted by requesting blanket access.

Local providers are deliberately **not** launched through a login shell, even
though the remote path already does. That would add shell-init latency to every
probe and extend a surface this repo has already been burned by — the `bash -lic`
terminal-process-group message that masked a dropped connection and is recorded
under repeated failures.

Deliberately not possible: reporting a permission denial as a missing binary,
requesting broad access to stand in for the folders a project actually uses, and
a provider path RCP uses but will not show you.

## Setup

A clean macOS user account that has never run RCP or granted it anything, with
Codex or Claude installed and authenticated for that account, an SSH host it can
reach, and a project whose state repository lives under a protected folder.

## Drive

1. Open the signed application under the clean account.
2. Read provider readiness for local Codex and Claude, and inspect the recorded
   paths.
3. Move or remove a recorded binary, reread readiness, and re-resolve.
4. Open a project whose state repository is under a protected folder.
5. Run an agent task that writes its patch, and one against the remote host.
6. Compare each result with the same operation run from Terminal as that user.

## Assert

- `providers_visible_in_terminal_are_visible_in_the_application`
- `each_provider_path_is_recorded_and_shown`
- `a_recorded_path_can_be_edited_by_hand`
- `a_stale_recorded_path_is_its_own_readiness_state`
- `re_resolve_recovers_a_moved_binary`
- `local_and_remote_resolve_through_the_same_field`
- `the_environment_is_repaired_before_the_backend_starts`
- `nothing_depends_on_the_repair_having_worked`
- `ssh_authenticates_from_the_application`
- `a_protected_folder_is_reachable_or_says_it_was_denied`
- `a_permission_denial_is_never_reported_as_a_missing_binary`
- `an_agent_subprocess_writes_its_patch`
- `a_remote_run_completes_from_the_application`
- `first_launch_consent_is_explained_before_it_is_requested`

## Failure means

RCP tells a human that Codex is not installed on a machine where they just ran
it, or an agent task fails on a permission the human was never asked for. Both
send them to fix the wrong thing, and neither leaves them anywhere to look.
