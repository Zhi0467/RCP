# Server-only and empty team repositories

Status: design confirmed 2026-10-09; implementation not started. Ships in
PR #284 with the setup-page and wizard trims already on that branch.

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

## Slices

Each slice updates the specs it changes and adds tests in proportion to its
diff. Shared contracts (`src/rcp/storage/models.py`, `web/src/core/types.ts`)
change only in slice 1 and slice 3.

1. **Source model and setup** — `storage/models.py`, `storage/provisioning.py`,
   `api/project_provisioning.py`, `server_ops/project_provision.py`,
   `server_ops/git_credentials.py`, `server_ops/project_checkout.py`,
   `server_ops/remote_project_checkout.py`, `web/src/core/types.ts`.
   The repository intent carries `repository: GitHubRepositoryRef | None`.
   Server-only repositories skip the key and probe targets and get one
   checkout target that initializes the repository. An empty GitHub probe
   result becomes "create and push the first commit" instead of a stop.
   Specs: projects-spaces-and-operations, server-and-machine-operations,
   api-web-and-desktop-projections.
2. **Backup and restore** — `server_ops/backup_models.py`,
   `server_ops/backup_checkout.py`, `server_ops/backup.py`,
   `server_ops/restore.py`. The recovery descriptor allows a server-only
   repository; capture verifies it has no origin; restore initializes it and
   publishes `.research`; the backup report names server-only repositories as
   not backed up.
3. **Connect and add later** — new provisioning kinds `add_repository` and
   `connect_repository` in `storage/models.py`, their API routes and
   coordinator targets, the fast-forward check, the diverged-history stop, and
   the manifest append for an existing project.
4. **Transfer** — `transfer/`, `project_transfer.py`: blank target URL makes a
   server-only repository from the bundle; refuse when the bundle is missing.
5. **Web** — `web/src/projects/TeamProjectSetup.tsx` (optional URL, server-only
   label and backup note), `ProjectSettings.tsx` (Add repository, Connect to
   GitHub), `TransferProjectSetup.tsx` (blank target URL).

Order: slice 1 first; then 2, 3, 4, and 5 in parallel.

## Checks

- Focused pytest per slice; web tests and build for slice 5.
- Served-app drive on disposable data: create a team project with a
  server-only repository and one empty GitHub repository on a throwaway team
  server; connect the server-only one to an empty GitHub repository.
- Backup and restore of a project with a server-only repository on a copy.
