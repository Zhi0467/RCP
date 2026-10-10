# Team repositories may live only on the server

Confirmed by the human 2026-10-09.

## Decision

A team project repository has one of two sources:

- **GitHub** (`owner/repo` on github.com), as before. An empty GitHub
  repository no longer stops setup: after the deploy key is added, setup
  creates one empty commit, `Start RCP project`, and pushes it to `main` with
  that key. The push is the write proof. The wizard says it will do this before
  it does.
- **Server only.** Setup runs `git init -b main` in the central checkout and
  makes the same `Start RCP project` commit. There is no remote, deploy key, or
  push proof. The wizard leaves the GitHub URL blank to choose it.

Server-only code is not backed up. Setup, project settings, and the backup
report say so. Restore recreates an empty server-only repository and publishes
the archived `.research` state into it.

A server-only repository can later be connected to GitHub, and a team project
can later gain repositories. Both run as provisioning requests through the same
server command and wizard as setup, one repository each. Connecting pushes the
server's `main` when GitHub is empty or the push fast-forwards; otherwise the
wizard stops and asks the human to merge GitHub's `main` into the server
repository first. RCP never force-pushes or merges for the human.

A personal project whose repository has no GitHub remote transfers to a
server-only team repository built from the transfer Git bundle; including
committed history is then required for that repository.

## Why

A GitHub repository with a commit was the only entry to a team project, so a
new idea or a local-only codebase could not start there. The research state
needs a server folder, and agents need Git history, branches, and landing
commits, so the minimum route is a Git repository on the server rather than a
plain folder or no repository.

The old rule against a manufactured first commit kept RCP from writing to a
human's repository unannounced. The commit is now announced in the wizard,
empty, and authored by RCP, which keeps that intent while removing the stop.

Backing server-only code up would put source Git into an archive designed to
exclude it; the human chose a visible warning over a larger archive.

## Rejected

- A plain folder without Git, and a project without any repository.
- Backing up server-only repositories as Git bundles.
- Connecting to GitHub or adding repositories from a one-call settings action
  outside the provisioning request.
- Force-pushing, or merging GitHub history for the human, when histories
  diverge.
