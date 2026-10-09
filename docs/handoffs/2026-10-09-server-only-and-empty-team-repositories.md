# Server-only and empty team repositories

Status: slice A contracts and W1 (B1 setup/first push and B3 add/connect)
implemented and locally checked. Other slice B owners and live served-app/remote
journeys remain. Design confirmed and reviewed 2026-10-09.
Ships in PR #284 with the setup-page and wizard trims already on that branch.

Decision: [team repositories may live only on the server](../decisions/2026-10-09-team-repositories-may-live-only-on-the-server.md).

## Settled

- A team repository's source is GitHub or server only. The wizard's GitHub URL
  is optional; blank means server only. There is no mode switch.
- Server only: `git init -b main` in the central checkout, one empty commit
  `Start RCP project` authored by RCP, no remote, no deploy key, no push proof.
  Agents and terminals use it exactly like a cloned checkout, with the same
  per-member Git identity.
- Empty GitHub repository: after the deploy-key stop, setup makes the same
  empty commit and pushes it to `main` with the deploy key. That push replaces
  the temporary-ref write proof for an empty repository. The wizard announces
  the commit before making it.
- Backup does not include server-only code. Setup, project settings, and the
  backup report state it. Restore recreates an empty server-only repository
  (`git init -b main` plus the RCP commit) and publishes the archived
  `.research` state into it.
- Connect to GitHub later and add a repository later are provisioning requests
  started from project settings, one repository each, run by the same server
  command and wizard. Connecting pushes `main` when GitHub is empty or the push
  fast-forwards; otherwise a human stop asks the operator to merge GitHub's
  `main` into the server repository, then resume. Never force-push or merge.
- Transfer: a blank GitHub URL on the target makes a server-only repository
  built from the transfer Git bundle; the bundle is required for that
  repository.
- Branch name `main` everywhere RCP creates history.
- An added repository carries a "count as project truth" choice, on by
  default. The requesting member's final Confirm is the human approval: it
  publishes manifest membership and truth scope through the existing approval
  transition and `StateWorkspace`, bound to the reviewed manifest and head.
- Empty or connect push: list GitHub's real refs (not only advertised `HEAD`),
  fetch remote `main`, push the recorded local `main` with a non-forced refspec
  only when remote `main` is absent or an ancestor of it. The RCP first commit
  is created and recorded before any push so a retry reuses it. A lost receipt
  is reconciled by reading the remote back; a human push that wins the race
  returns the request to the diverged-history stop.

## Design review (astra, 2026-10-09)

Folded in above and in the slices: existing-project authority and storage
migration; one effective repository inventory for backup, restore, doctor,
terminals, agent launches, and Settings (backup otherwise refuses the whole
project); a replacement checkout proof after restore instead of the old
provisioning SHA; native transfer contracts and bundle install on `main`;
race-safe first push.

## Slices

Each slice updates the specs it changes and adds tests in proportion to its
diff.

**A. Contracts (implemented).** Storage migration: provisioning kinds
`add_repository` and `connect_repository`, a nullable GitHub source per
repository, existing-project targeting (`target_project_id`), and the truth
choice. One owner, `effective_repositories(project)`, that resolves each
repository's source (GitHub or server only), deploy-key evidence, and latest
checkout proof from completed requests. API response fields and
`web/src/core/types.ts`. Native transfer structs gain the optional source.

**B. Parallel after A, one owner each.**

1. Setup and first push (implemented in W1) — `server_ops/project_provision.py`,
   `git_credentials.py`, `project_checkout.py`, `remote_project_checkout.py`:
   server-only init, empty-repo first commit and race-safe push.
2. Backup, restore, doctor — `projects.py` descriptor builder,
   `server_ops/backup*.py`, `transport/remote_backup_checkout.py`,
   `server_ops/restore.py`, `server_ops/doctor.py`: consume the effective
   inventory, record a replacement proof after restore, surface the
   not-backed-up notice durably; prove backup → restore → backup.
3. Add and connect (implemented in W1) — coordinator targets for the two new kinds, the
   approval-transition completion, the diverged-history stop.
4. Transfer — `transfer/configuration.py`, `project_transfer.py`,
   `transport/remote_transfer_git.py`, `web/src-tauri/src/project_transfer.rs`:
   blank target source, per-repository bundle requirement, bundle installed on
   `main`.
5. Runtime — `api/terminals.py`, `terminals/manager.py`,
   `transport/remote_terminal.py`, `agents/git_access.py`, `service.py`
   Settings projection: no deploy key for server-only repositories, member Git
   identity kept.
6. Web — setup wizard optional URL and backup note, Settings Add repository
   and Connect to GitHub with the truth checkbox, transfer blank target URL.

## Checks

- Focused pytest per slice; web tests and build for slice 5.
- Served-app drive on disposable data: create a team project with a
  server-only repository and one empty GitHub repository on a throwaway team
  server; connect the server-only one to an empty GitHub repository.
- Backup and restore of a project with a server-only repository on a copy.
