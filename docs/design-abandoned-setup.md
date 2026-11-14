# Abandoned setup: cancel and recovery paths

Design for one PR. Delete this file before merge; its rules move to the owning specs
(`server-and-machine-operations.md` "Durable project provisioning" and "Central checkouts",
`providers-and-containment.md` sign-in sections). Team enrollment re-claim and
move-to-team cancel are tracked as a separate follow-up.

Each item extends an existing owner. It adds no new layer.

## 1. Team-project setup: cancel in every open state

Today the member cancel route (`api/project_provisioning.py:1418`) refuses after
`waiting_for_server_setup`. The spec says the machine owner records the cleanup
disposition afterward, but no code path does. `GitCredentialManager.remove_key` has no
caller, and the member route only ever writes `nothing_to_remove`.

### Who and when

The authorizer may cancel a `create_team_project`, `add_repository` or
`connect_repository` request in any open status: `waiting_for_server_setup`,
`setup_in_progress`, `operator_action_needed` or `ready_for_review`. `incoming_transfer`
keeps today's refusal; transfer cancel is in the follow-up. `can_cancel` widens to match.

### Owner and fence

`ProjectProvisionCoordinator.cancel(request_id)` owns the cleanup. It runs in the same
CLI-owned team service process as `advance`.

- `app.py` stores the coordinator on `app.state.project_provision_coordinator`. It is `None`
  when `control_server is None`.
- The route takes the coordinator from there. When it is `None`, server setup cannot
  have run, so only today's `waiting_for_server_setup` path applies.
- The coordinator keeps one `threading.Lock` per request id. `advance` holds it for a whole
  step and waits for it as long as needed.
- `cancel` waits at most `PROJECT_PROVISION_CANCEL_WAIT_SECONDS` (5 s in `limits.py`).
  On timeout it answers 409: "A setup step is running. Try again when it finishes."
- Once the request is cancelled, `_resolve` refuses it. The revision bump also invalidates
  a plan or a `complete` call already in flight (`expected_revision`).

### Cleanup

Cleanup is idempotent and runs under the lock. For each repository:

- **Deploy key (GitHub sources).** A new `GitCredentialManager.existing_key(machine,
  repository, space_id, project_id, alias)` returns the key's `DeployKeyMaterial`, or
  `None` when the deterministic path is empty.
  - It reuses the absence probe now inside `prepare_recovery_preflight`, extracted as
    `_key_absent`, then the helper's `inspect`.
  - `remove_key(machine, material)` deletes the key.
  - Reading the path itself, rather than the recorded label, also covers a key written
    before its label was saved (`project_provision.py:612` vs `:648`).
- **Checkout.** If `checkout_disposition == "request_created"` and `resolved_path` is set,
  a new checkout-helper verb `remove-checkout` deletes it.
  - It takes the same arguments as `prepare` (account, home, location, configured root,
    project id, alias) and derives `<central root>/<project id>/repositories/<alias>` the
    same way. It never accepts a path from the caller.
  - It opens each component without following symlinks and requires the account to own it.
  - It removes only the alias directory, with fd-relative `shutil.rmtree`.
  - `reused_existing` checkouts are never touched.
  - Request-made checkouts hold only GitHub history or RCP's empty first commit, because
    no agent runs before registration. Removing them is what lets an add, connect or
    retry with the same project id and alias succeed later.

If any helper call fails, for example when SSH is unreachable, cancel answers 409 and
names the machine and the exact path. The request stays open and unchanged. Pressing
Cancel again retries; already-removed items read as absent. A crash mid-cleanup leaves the
same retryable state. The existing "cancellation handling" branch
(`operator_action_needed` plus a disposition) stays unused.

### Disposition

The disposition is written once, in the transition to `cancelled`:

- `nothing_to_remove`: no key and no request-made checkout existed.
- `request_owned_state_removed`: local state was removed and no GitHub key ever existed.
  This covers server-only repositories only.
- `prepared_state_preserved`: a GitHub deploy key existed. Its private file is gone, but
  its grant on GitHub may still be live, and RCP holds no GitHub token to revoke it
  (spec: confirmed revocation or preservation).

### Web

- The cancelled request keeps its repository ledger. Each repository with a
  `deploy_key_label` links its `settings_url` as "Remove deploy key" and shows the label
  in code.
- The backend's `next_action` for a cancelled request with `prepared_state_preserved` says
  to remove the listed deploy keys on GitHub.
- "Existing setup requests" lists open requests plus cancelled requests whose disposition
  is `prepared_state_preserved`, since those still have a manual step.

## 2. Shared provider sign-in

- **No silent replace.** `start_sign_in` refuses a login whose state is `signed_in`
  with 409: "This login is already signed in. Sign out first to replace it." The UI offers
  Sign in only on signed-out rows, so only a stale tab or a race reaches this. There is no
  replace flag, because no caller needs one.
- **Any member can cancel.** The cancel route authorizes with
  `require_login(LoginKey(status…), member_id)` instead of `started_by`. Non-starters
  already receive the attempt id, with the code scrubbed, in `row.sign_in`
  (`provider_login_rows.py:35-43`).
  - The status GET stays starter-only.
  - The web shows the existing cancel button next to "X is signing in".
- **Gate wait deadline.** `ProviderCredentialGate.hold_blocking(key, wait_seconds=None)`
  raises `ProviderCredentialBusy` once `wait_seconds` elapse.
  - `verify`, `save_token` and `sign_out` pass `PROVIDER_LOGIN_GATE_WAIT_SECONDS` (10 s)
    and map the error to `ProviderLoginRefused` 409: "This login is busy. Try again
    shortly."
  - Without the deadline, these requests busy-wait for as long as a device sign-in holds
    the gate (up to 15 min), so an abandoned sign-in hangs every other action on that
    login.
  - The sign-in thread, probes and turns keep waiting without a deadline.
- **Restart during token save.** Startup settles the token-save fence, "A replacement
  credential is awaiting verification.", with its own detail: "Saving the token was
  interrupted. Paste it again." The device-code marker keeps its current message.

## Verification

Backend tests (pytest, focused files):

- Cancel in `setup_in_progress` and in `ready_for_review` removes the recorded key and the
  request-made checkout, keeps a `reused_existing` checkout, and writes
  `prepared_state_preserved`. Uses doubles of the credential and checkout managers.
- Cancel with a key on disk but no recorded label removes it.
- A helper failure returns 409, leaves the request unchanged, and a retry succeeds.
- Cancel while `advance` holds the lock returns 409.
- A plan taken before cancel is refused by `advance` afterward.
- Server-only cancel writes `request_owned_state_removed`.
- The real checkout helper `remove-checkout` refuses a symlinked component and another
  owner's directory, reports an absent checkout as not removed, and removes a real tree. Runs locally in tmp.
- Sign-in: starting on a signed-in login returns 409; another member's cancel succeeds; a
  member without access gets 403 or 404; verify while the gate is held returns 409 within
  the deadline; startup settles the token-save marker.

Web: a node test for the list filter. Run the build.

Served app: drive the team setup on throwaway data at 390 px and desktop to confirm Cancel
in an open state and the cancelled ledger.
