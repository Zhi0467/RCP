# Paper: References, arXiv export, restyle, GitHub sync

This design extends the shared Paper workspace
([spec](../specs/paper-artifacts-and-result-views.md#shared-paper)).
It is deleted before merge. Its rules move into the Paper spec, the interface
spec, and the providers spec.

The approved mockup is [beside this file](paper-git-references-export.mockup.html)
and is deleted with it.

## Problem

Members write papers in RCP, but three things still happen elsewhere.

- Co-authors outside the lab work in Overleaf or a Git clone. Today a member
  copies files back and forth by hand.
- References live in `.bib` files that nobody can browse while writing. Cite
  keys are typed from memory.
- Submitting to arXiv means assembling a source archive by hand.

## Goals

1. A paper syncs with one GitHub repository. An Overleaf project linked to the
   same repository stays in step.
2. A member can browse references, see one under a cite key, insert a cite,
   and read a reference as a page.
3. One click produces a source ZIP that arXiv compiles.
4. The Paper workspace takes the approved look.

## Non-goals

- No Overleaf API and no stored Overleaf credential.
- No Git host other than GitHub.
- No reference database outside the paper's `.bib` files. No lookup service.
- No Git history view, branches, or pull requests inside RCP.
- No restyle outside the Paper workspace.

## Decisions already made by the human

- Git is the only sync route. An Overleaf user links their project to the same
  repository from Overleaf's own menu.
- RCP generates one deploy key per paper. A member pastes the public half into
  the repository with write access.
- One link per paper, owned by the project. Any member may push. The commit
  author is that member.
- Pull is automatic. Push is manual and carries a commit message.
- A clean incoming edit applies at once. An overlapping edit becomes an accept
  or reject card.
- The References panel is read-only. Adding, fixing, deleting, and checking
  references is Coach work that returns change cards.

## What does not change

- SQLite stays the only truth for Paper. The Git repository is a transport
  copy, like the published `paper/` tree.
- Publication to `paper/` is untouched. Paper still never enters the canonical
  state repository (invariant 6) and never becomes graph state.
- Agent capability is untouched (invariant 4). The Coach still writes only its
  changes folder. No launch gains Git transport for the paper.
- Paper has no main file.
- `paper_path` keeps reserving `.git` and `.publish`.

---

# Part A: References, export, restyle

Part A needs no migration and no new backend state.

## A1. References

### Reading

All reading is client-side.

- `web/src/paper/bibtex.ts` (new) parses BibTeX: entries, fields, braces and
  quotes, `@string`. It skips `@comment` and `@preamble`. A malformed entry
  yields a row marked unreadable and never hides the rest of the file. No
  dependency is added.
- `web/src/paper/PaperReferences.tsx` (new) is the panel. It loads every
  `.bib` file in the tree through `paperRawUrl` with the content-hash cache
  pattern in `typstPreview.ts`. It reloads a file when its revision changes.
- The panel sits under Files as one folded row with a count. Unfolded, it
  groups entries by `.bib` file and filters by a search field.
- A key defined in two files shows in both groups with a duplicate mark.

### In the editor

- `web/src/paper/citeKeys.ts` (new) finds cite keys by file format:
  `\cite{…}` and its variants in LaTeX, `[@key]` in Markdown, `@key` and
  `#cite(<key>)` in Typst.
- `PaperEditor.tsx` adds a CodeMirror `hoverTooltip` from `@codemirror/view`,
  already a dependency. The card shows title, authors, venue, and year. An
  unknown key says so.
- Clicking a panel entry inserts a cite at the cursor in the open file's
  syntax. Clicking a cite key in the editor opens that reference.

### The Reference tab

`PaperWorkspace.tsx` holds two pieces of pane state today. They widen:

- desktop right pane: `preview | coach | reference`
- phone view: `write | preview | coach | refs`

The Reference tab shows one reference as a readable page: title, authors,
venue and year, abstract when the entry has one, and DOI, URL, and eprint
links that open in a new browser tab. It names the `.bib` file and opens it
at the entry.

The phone `refs` view shows the list. Choosing an entry shows its page with a
back control.

### Writing

`PromptFactory.paper_coach_task_contract` gains reference guidance, and
`PAPER_COACH_POLICY_VERSION` bumps. The guidance covers four jobs:

- add an entry to the `.bib` file the document already uses
- fix an entry against its source
- delete an entry no file cites
- check that every cited key exists and every entry is real

The Coach already launches with network access and returns `.bib` edits as
ordinary change cards. Tests check the staged contract data and the version
bump, never the wording.

## A2. Export for arXiv

Route: `GET /api/projects/{id}/paper/export/arxiv?file_id=…`.

| Result | Meaning |
| --- | --- |
| 200, `application/zip` | The archive. |
| 409, JSON `{reason, log_tail}` | The root is not LaTeX, the engine is unavailable, or the compile failed. |

- The root is the file being read, because Paper has no main file. For a
  non-LaTeX file the button is disabled and its hover says why.
- The route resolves `file_id` to a path in one snapshot. If that path is not
  in `root_candidates`, it returns 409 `not_root`.
- `PaperCompiler` gains `bibliography(project_id, root)`. It calls
  `engines.ensure("latex")`, reuses the preview's cancel-safe tree
  materialization, and calls the existing `_run` with one new argument that
  adds Tectonic's `--keep-intermediates`. It returns
  `Bibliography(status, bbl, log_tail)`; `status` is `ok`, `unavailable`, or
  `failed`, and `bbl` is the bytes of `<root stem>.bbl` or nothing. It takes
  the compile concurrency limit but not the per-project preview job slot, so
  it never cancels a live preview. The `tectonic` entry in `dependencies.py`
  names export as a second feature.
  **Why:** arXiv does not run BibTeX.
- A new `arxiv_archive` beside `folder_archive` in `api/paper.py` writes the
  ZIP the same way. It holds every file in the root's folder and below, at
  paths relative to that folder, so the root sits at the top of the archive.
  Markdown, Typst, and HTML files are left out. It adds the `.bbl` beside the
  root unless the folder already has that file.
  **Why:** arXiv compiles from the archive's top level, and a LaTeX build
  reads files of any type, so a format allowlist would drop inputs.
- A document with no bibliography exports with no `.bbl`.
- On 409 the Web shows the reason and log in a `PaperDialog`.

The button is a box icon in the editor header. At phone width it moves into
the Files sheet.

## A3. Restyle

Scope: Paper selectors in `07-paper.css` and `13-phone.css`, plus new tokens
in `01-base.css` for every theme. The mockup's `:root` block is the token
list and its values are the starting point for the light theme.

- Files, editor, and right pane become floating rounded panels on a darker
  ground.
- Toolbars shrink to one compact row. The Preview, Coach, Reference switch is
  a segmented control.
- One neutral accent. Status color stays semantic.
- **Serif titles.** A new token `--paper-serif` is the system serif stack from
  the mockup, with no bundled font. It sets the reference title in the hover
  card and the Reference tab, and nothing else. The interface spec sentence
  that keeps Paper in Figtree gains this one exception.
- No new secondary labels. Notes sit on hover. Panel folds and tab changes
  animate without a jump.

---

# Part B: GitHub sync

## B1. Mechanism

The backend keeps one **bare** repository per linked paper under
`<data dir>/paper-git/<project id>/`:

| Path | Content |
| --- | --- |
| `repo.git` | The bare repository. |
| `id_ed25519`, `id_ed25519.pub` | The deploy key. |
| `tmp/` | Scratch files for one merge, removed after it. |

It never checks files out. It reads trees with `git ls-tree -r -l -z` and
blobs with `git cat-file --batch`. It builds commits with a temporary index,
`git write-tree`, and `git commit-tree`.

**Why bare:** remote content is untrusted. With no checkout there are no
symlinks, submodules, or case collisions to defend against.

The backend holds every Paper document, including for personal projects with
SSH state roots. So the same local repository works for personal and team
spaces.

### Three versions, one merge rule

For each path the merge compares:

- **base**: the file both sides last agreed on
- **theirs**: the file at the remote head
- **ours**: the live Paper file

The merge is path-wise over trees. It never looks at Git history. A
force-push on the remote is therefore just a different "theirs".

### Where the base comes from

The link stores `synced_commit`. For most files the base is that commit's
version.

A file with an undecided conflict is **held**. A held file keeps its own base
commit until every conflict in it is decided. **Why:** if the base moved to
the new remote head while a conflict was open, the co-author's side would
exist only in a card. A later pull or an outdated card would then lose it,
and the next push would overwrite their text on GitHub.

Rules that follow:

- A **re-derive** rebuilds all conflicts in every held file from
  (held base, remote head, live text). Nothing is carried in a card alone.
  It runs in two cases only: a pull whose fetched head differs from
  `synced_commit`, and an explicit Refresh. A poll that finds the same head
  touches nothing, so cards keep their ids.
- A held text file is **undecided** while the change store has a `paper-git`
  group on its `file_id` that is `pending` or `outdated` with no committed
  receipt. That is `PaperChanges.undecided_group_ids`. A held file conflict
  is undecided until a member picks a side.
- A held file with nothing undecided is released: it leaves the `held` list
  and its base becomes `synced_commit`'s version. Release is computed under
  the sync lock at every pull, push, and conflict action. The status route
  reads the same rule without writing.
- Push is refused while any file is held.
- Local refs pin `synced_commit` and each held base commit against garbage
  collection.

A rejected conflict stays rejected across a re-derive while neither side of
that region changed. It shows again when the remote changed the region or the
member edited it, because the card then proposes a different replacement.
That is the change store's existing rule for a decided proposal, so Git sync
adds no rejection memory of its own.

## B2. Records

One new table `paper_git_links`, one row per project, in a new storage mixin
that copies `storage/paper_coach_steers.py`. It registers as schema migration
82. Schema constants sit beside the model.

| Field | Meaning |
| --- | --- |
| `project_id` | Primary key. |
| `state` | `pending` after the key exists, `linked` after the first successful probe. |
| `repository` | `owner/name` on GitHub. |
| `branch` | The branch to sync. |
| `key_fingerprint` | SHA-256 fingerprint of the deploy key. |
| `linked_by`, `linked_at` | The member who linked, and when. |
| `synced_commit` | The remote commit Paper last merged or pushed. Empty before the first sync. |
| `synced_revision` | The Paper revision that matched `synced_commit`. The pill compares it with the live revision to mark unpushed edits. |
| `held` | JSON list of `{file_id, path, base_commit, kind}`. `kind` is `text` or `file`. `file_id` is empty when Paper has no file at the path. |
| `skipped` | JSON list of `{path, reason}`: every entry of the `synced_commit` tree that Paper does not hold. |
| `last_error`, `checked_at` | The last failure text and last fetch time. |

The migration also adds 82 to `SUPPORTED_RESTORE_DATABASE_SCHEMAS` in
`server_ops/restore.py` and extends the upgrade-gate seed.

Lifecycle. The link is machine-local state, like the key:

- The table stays **out of** `PAPER_PROJECT_TABLES`. That one list drives
  backup, restore, transfer, and delete, so the row, key, and repository are
  never backed up or transferred. A restored or transferred project arrives
  unlinked.
- **Project delete** removes the row in the explicit per-project delete list
  in `storage/projects.py`, and removes the directory.
- **Git cards** do travel, because `paper_change_groups` is in that list. The
  shared Paper restore step in `backup_paper.py` drops every `paper-git`
  group as it inserts.
- **A row whose key file is missing** is deleted at backend start, with its
  Git cards, by one reconciliation step in `api/app.py`. The member links
  again. Relinking starts from the empty base: identical files do nothing
  and differing files become conflicts.
- **An old database** without the table opens as not linked. The old-writer
  regression covers that.

## B3. The key, the runner, and trust

**Key.** `rcp.paper.git_sync` generates it with
`ssh-keygen -q -t ed25519 -N "" -C rcp:paper:<project id> -f <path>` under
umask 077. This repeats the argument list of the shipped server helper; that
helper carries server account checks and is not imported. A relink always
generates a new key, because GitHub refuses a deploy key already attached to
another repository.

`ssh-keygen` is already registered in `dependencies.py`. On local and remote
installs it is optional with the stated fallback "Key setup is refused, with
the reason". Linking uses that fallback unchanged.

**Runner.** One function `rcp.paper.git_sync._git(project, argv, *, timeout)`
runs Git locally with a fixed environment:

- `PATH` from the backend, `HOME` set to the paper's `tmp/` directory
- `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`,
  `GIT_TERMINAL_PROMPT=0`, `GIT_SSH_VARIANT=ssh`
- `GIT_SSH_COMMAND`: `ssh -F /dev/null -i <key> -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null
  -o UserKnownHostsFile=<bundled file>`

It builds its own SSH command. `git_access.deploy_key_ssh_command` stays
untouched, because it trusts the execution account's known hosts and may
attach an agent socket, and Paper wants neither.

The repository is created with `git init --bare --template=`. Every command
passes `-c core.hooksPath=/dev/null` and the full URL. No remote is stored in
the repository config. Hooks never run. The directory is write-protected from
agents, which is what keeps its `config` honest.

**Host trust.** The repository field accepts only `owner/name` and always
resolves to `git@github.com:owner/name.git`. RCP ships GitHub's published SSH
host keys as `rcp/paper/github_known_hosts`, listed in
`packaging/rcp_backend.spec`. No operator step and no trust-on-first-use.
**Why:** the existing operator trust step exists only on team servers, and a
personal space has no operator. The cost: a GitHub host key rotation needs an
RCP release. The failure text says so.

**Protection from agents.** `<data dir>/paper-git` joins the hidden-read
paths built in `agents/hidden_read.py` for every launch. The whole data
directory is already write-protected by `agents/write_scope.py`. A test
asserts both for a personal data directory and a server data directory. Where a provider
cannot hide reads, the existing `unhidden` status applies and is already
shown. The key grants write access to one paper repository and nothing else.

**Limits**, all in `limits.py`:

| Constant | Value |
| --- | --- |
| `PAPER_GIT_POLL_SECONDS` | 60 |
| `PAPER_GIT_RETRY_SECONDS` | 300 |
| `PAPER_GIT_TIMEOUT_SECONDS` | 60, for every fetch, probe, and push |
| `PAPER_GIT_MAX_REPOSITORY_BYTES` | 256 MiB on disk |
| `PAPER_GIT_APPLY_RETRIES` | 3 |

Fetch is always `--depth=1` of the one branch. After each sync the backend
prunes unreferenced objects. A repository over the disk limit stops syncing
with a stated error.

## B4. Linking

1. A member opens the Git pill, enters `owner/name` and a branch (default
   `main`), and asks for a key. The backend writes a `pending` row, generates
   the key, and returns the public half. The dialog shows it with Copy.
2. The member adds it to the repository as a deploy key with write access and
   presses Link.
3. The backend probes in two steps. `git ls-remote <url>` proves the key can
   read and returns the branch head, if any. Then the backend writes a commit
   of the empty tree locally and runs
   `git push --dry-run <url> <commit>:refs/heads/rcp-link-probe`, which
   GitHub refuses for a read-only key. Nothing is created on GitHub. A
   failure returns Git's message and keeps the row `pending`. Asking for a
   key again on a `pending` row returns the same key.
4. Success sets `linked` and runs the first pull.

Every poll starts with `git ls-remote` for the branch. A fetch runs only when
the head it reports differs from `synced_commit`. A repository with no such
branch, or no commits at all, is the empty tree, not an error. The first push
creates the branch.

There is no first-link special case. Before the first sync the base is the
empty tree. A non-empty repository merges into a non-empty paper by the
merge table; files present on both sides with different content become
conflicts.

Unlink deletes the row, the directory, and all Git cards. Paper files stay.

## B5. Pull

### When

`PaperGitPoller` copies `ConsolidationPoller`: `poll_once` plus
`stop.wait(interval)`, started in `api/app.py`, with the same startup fence
and maintenance admission checks. Each round it checks every linked paper
that has a live editor room. A new `PaperLive.open_projects()` returns the
project ids with at least one room that has a subscriber; the poller thread
calls it through `live.run_sync`. Opening the Paper workspace also checks at
once. A paper nobody has open is not fetched, so remote edits never apply
with nobody watching.

### Locking

`git_sync` holds one sync lock per project. Poll, push, link, unlink, and
every decision on a Git conflict take it. Typing never does.

The Paper lock is never held across Git or a merge. A pull runs like
`PaperLive.restore`:

1. Fetch.
2. Read a Paper snapshot at revision R through `live.run_sync`.
3. Compute the merge outside any Paper lock.
4. Call `apply` with `expect` built from the snapshot, as `restore` does: a
   whole-text check for each text file replaced or deleted, a `blob_sha256`
   check for each binary file replaced or deleted, and `path_free` for each
   create. A text replace is the minimal op from `text_replacement`, so
   co-editors keep their cursors and the document state stays small.
5. On `PaperPrecondition` or `PaperLimit`, go to step 2, up to
   `PAPER_GIT_APPLY_RETRIES`. After that, or on `PaperRateLimit`, the pull
   **fails**: nothing in the link row changes, `last_error` is set, and the
   next round retries.

### The merge table

"Changed" means the bytes differ from base. Absent counts as a version.

| Ours vs base | Theirs vs base | Result |
| --- | --- | --- |
| any | same as base | Nothing. |
| same as theirs | any | Nothing. |
| same as base | changed or added | Take theirs. |
| same as base | deleted | Delete. |
| changed or added | changed or added, both text | Three-way text merge. |
| changed or added | changed or added, either binary or kinds differ | File conflict. |
| changed | deleted | File conflict. |
| deleted | changed | File conflict. |

The second row covers "both deleted", "both made the same edit", and a rerun
after a crash.

Kind is `paper_file_kind` of each side's bytes. "Take theirs" across a kind
change is a delete plus a create, because `PaperOp` has no kind change.

A remote rename is a delete plus a create. Rename detection is left out.

Bytes pass through unchanged. There is no line-ending conversion.

The merge covers only paths that are not skipped. A path in the stored
`skipped` list has an absent base, so an entry that later fits arrives as a
plain "take theirs".

The pure merge checks its own result against the Paper limits before
anything is written. A merged text, or a proposed `--theirs` text, over
`PAPER_MAX_DOC_BYTES` becomes a file conflict. So `apply` and `collect` see
only trees they accept.

### Text merge

For one file the backend writes base, ours, and theirs into `tmp/` and runs
`git merge-file -p`. A missing base is an empty file.

- Exit 0: the output is the merged text. It applies.
- Exit above 0: the file has overlaps. Two more runs follow. `--ours -p`
  gives our side of each overlap plus every non-overlapping remote edit; it
  applies. `--theirs -p` gives the text the cards propose. The file becomes
  held with kind `text`.

### What gets written

In this order, each step safe to repeat:

1. **Clean part.** All "take theirs", deletes, and merged texts go into one
   `PaperLive.apply(project_id, ops, actor, expect, checkpoint=True)` call.
   That is one Paper revision, and `checkpoint=True` saves an automatic
   version first. That version is the undo point and shows in History like
   any automatic version. The revision's actor is the fixed id `github`. An
   empty op list skips the call.
2. **Text conflicts.** One `PaperChanges.collect` call with task id
   `paper-git`, the post-apply snapshot, and a `RegularTree` holding
   `files/<path>` with the `--theirs` text of every held text file.
   `replace_group_ids` is every `paper-git` group on those files, decided
   ones included. `collect` deletes the undecided ones and uses the decided
   ones to leave out any proposal a member already decided.
3. **Link row.** `synced_commit`, `held`, `skipped`, and `synced_revision`
   update in one statement. `synced_revision` moves only when the live
   revision equaled it before the pull.

A crash between steps reruns the merge against the old base. Files already
applied now equal theirs and fall under row two.

### Entries Paper cannot hold

Every pull judges the **whole** remote tree, not only what changed. An entry
is skipped with a reason when:

- `paper_path` refuses the path
- it is a symlink or submodule
- it is over `PAPER_MAX_UPLOAD_BYTES`, or is text over `PAPER_MAX_DOC_BYTES`
- Paper does not hold it and taking it would break a Paper tree limit or
  collide with a local file or folder of the same name

Entries are judged in path order, so which ones a limit skips is stable.
Skipped entries are listed in the Git dialog. One bad entry never blocks the
rest of the pull. Push carries every skipped entry through unchanged.

## B6. Conflicts

Conflicts belong to the Git owner. They show in the Git dialog and inline in
the editor. They do not show in the Coach pane, which filters to Coach tasks.

### Text conflicts reuse the change store

- All Git groups use one fixed `task_id`, `paper-git`. The change store and
  its tables do not change.
- The card header says "GitHub". The dialog shows the remote head's short
  commit and author once, from the status.
- Accept and Reject go through the Git decide route below. It takes the sync
  lock, then calls the same `PaperChanges` decision with the member as
  actor. So a decision never races a re-derive. The Coach decision route
  refuses a `paper-git` group.
- Git cards have no Undo. History restore is the undo.
- An outdated Git card is refreshed by re-deriving. The workspace sends
  Refresh for a Git card to the Git route below, not the Coach route.
- Above 40% changed lines or 12 regions the store makes one whole-file card.
  Accept takes their file. Reject keeps ours.
- `collect` drops a trailing newline that theirs added to a base without
  one. The next pull treats that as an ordinary remote edit.

### File conflicts are two buttons

Binary, kind-change, and delete-against-edit conflicts have no line view.
The Git dialog lists each with **Take theirs** and **Keep mine**.

- Take theirs applies the matching ops: replace, delete, create, or delete
  plus create.
- Keep mine changes nothing in Paper.

Either choice releases the file.

## B7. Push

1. The member opens the Git dialog. It lists changed files with added and
   removed line counts.
2. The member types a commit message and presses Push.
3. The backend checks the remote head and runs a pull if it moved. If that
   pull fails, or `synced_commit` does not equal the remote head afterwards,
   the push stops with `retry`. **Why:** a commit built from a Paper that
   has not merged the remote head would revert the co-author's work.
4. If any file is held, the push stops. The dialog shows the conflicts.
5. If any Paper path equals a skipped entry's path or sits under or over it,
   the push stops and names the path.
6. The backend builds the commit from a snapshot at one revision:
   - start a temporary index from the `synced_commit` tree
   - remove every entry that is not in the `skipped` list
   - add every Paper file with `git hash-object -w` and `update-index
     --cacheinfo`; a path that existed keeps its mode, a new one is `100644`
   - `write-tree`, then `commit-tree` with `synced_commit` as parent
7. `git push <url> <commit>:refs/heads/<branch>`, never forced. A rejection
   reruns from step 3 once, then returns Git's message.
8. Success updates `synced_commit` and `synced_revision`.

Author and committer are the member: name `display_name`, email from a new
`member_git_email(user_id)` in `git_identity.py`, which `write_git_identity`
also calls. Both are passed as `GIT_AUTHOR_*` and `GIT_COMMITTER_*`.

The changed-file list in step 1 builds the same tree without committing and
runs `git diff --numstat` against `synced_commit`.

Push sends what Paper holds. If a member restored a version from before a
pull, the changed-file list shows the co-author's edits as removals, and the
push removes them on GitHub.

Push is a member action on that member's page, so it needs no new authority
rule under invariant 3.

## B8. Routes

All under `/api/projects/{id}/paper/git`. All require a project member.

| Route | Body | Returns |
| --- | --- | --- |
| `GET` | | `{state, repository, branch, public_key, head, unpushed, conflicts, skipped, last_error, checked_at}`; `state` is `none`, `pending`, or `linked`; `head` is `{commit, author}` with the author name stripped of control characters and cut to 120 characters; `conflicts` is the number of held files |
| `POST /key` | `{repository, branch}` | The status, now `pending`. 409 with the reason when `ssh-keygen` is absent. |
| `POST /link` | | The status. 409 `{reason}` with Git's message when the probe fails. |
| `DELETE` | | 204 |
| `POST /fetch` | | The status after one pull. |
| `GET /changes` | | `[{path, status, added, removed}]` |
| `GET /conflicts` | | `[{path, kind, ours, theirs}]` for file conflicts; `ours` and `theirs` are `present` or `absent` |
| `POST /conflicts/resolve` | `{path, take}`; `take` is `theirs` or `mine` | The status. |
| `POST /conflicts/decide` | `{group_id, decision}`; `decision` is `accept` or `reject` | The status. 404 when a re-derive replaced the card. |
| `POST /conflicts/refresh` | | The status after re-deriving. |
| `POST /push` | `{message}` | `{commit}`. 409 `{reason}`; `reason` is `retry`, `conflicts`, `collision`, or Git's message. |

Text conflict cards are still listed by `/paper/changes`.

## B9. Web

- `web/src/paper/PaperGit.tsx` (new): the pill, the link dialog, and the push
  dialog, built from `PaperDialog` and `useLayerDismiss`.
- The pill shows the branch, a mark when `unpushed`, and a count when
  conflicts exist. Its hover shows the last sync time or `last_error`.
- The push dialog shows file conflicts and text conflict cards above the
  changed-file list, using `PaperChanges` with Git handlers.
- `PaperCoach.tsx` already filters cards to its own tasks. Its unfiltered
  fallback list leaves out `paper-git` groups.

The mockup shows a number beside the branch. That becomes a mark, because an
exact count needs a tree build on every edit.

---

## Alternatives considered

| Choice | Rejected option | Why rejected |
| --- | --- | --- |
| Hold the base per conflicted file | Advance the base for every file at once | Loses the co-author's side when a card goes outdated or a second pull lands. |
| Path-wise tree merge | A real Git merge commit in a work tree | Needs a checkout of untrusted content and a second source of truth for live text. |
| Change store for text conflicts | Conflict markers in the file | Breaks compiles and shows raw markers to every co-editor. |
| Change store for text conflicts | A dedicated conflict table and card UI | Rebuilds line cards, rebasing, and inline widgets that exist. |
| Two buttons for file conflicts | Push them through the change store too | The store refuses kind changes and has no view for them. |
| Poll open papers | A GitHub webhook | Needs a public endpoint; personal spaces have none. |
| The `git` program | A Git library | `git` is already a required dependency; a library is a new one. |
| Bundled host keys | Trust on first use | Silent trust of whatever answers the first connection. |
| System serif stack | A bundled serif font | A fourth font package for two titles. |

## Spec changes at merge

| Spec | Change |
| --- | --- |
| Paper, "Shared paper" | "Paper is never committed to Git" becomes: Paper never enters the state repository; a paper may link one GitHub repository. Add the sync, conflict, References, and export rules above. |
| Interface, type | The `--paper-serif` exception. |
| Interface, Paper | Reference tab, Refs phone segment, Git pill, export control. |
| Providers and containment | `<data dir>/paper-git` is hidden from and protected against every launch. Coach policy version. |

Each changed spec gets an egm `codebase-details` pass.

## Build order

Each slice is test-first and ends green. Part A and Part B do not depend on
each other.

Part A:

1. `bibtex.ts`, `citeKeys.ts`, and their tests.
2. References panel, hover, insert, Reference tab, pane state.
3. Coach contract and version.
4. `PaperCompiler.bibliography`, the export route, the control.
5. Restyle, then the 390px pass.

Part B:

6. Storage: `paper_git_links`, migration, delete, backup and transfer lists,
   old-writer regression.
7. `git_sync`: runner, key, bare repository, tree read, commit build, agent
   protection. Tests pass a local bare repository URL through the one
   function that builds the remote URL.
8. The merge as a pure function from three trees to ops, held files, and
   skipped entries.
9. Pull wiring: snapshot, apply with checks and retry, `collect`, link row.
10. Push, conflict actions, routes, poller.
11. Web: pill and dialogs, Coach pane fallback filter.

## Verification

- Python: every merge table row as a unit test on the pure merge. A workflow
  test that links, pulls a clean edit, pulls an overlap, leaves it undecided,
  pulls again, checks the conflict is still offered, accepts it, pushes, and
  reads the commit from the bare remote. A test that push refuses while a
  file is held, and refuses when its own pull failed. A test that a skipped
  remote entry survives two pulls and a push. A crash-rerun test for each write step.
- Old writer: a database from the previous release opens, edits, and
  publishes with no link row.
- Web: parser tests on real-world `.bib` shapes; browser tests for hover,
  insert, the Reference tab, and the push dialog on `paperTestServer.mjs`.
- Served app: one journey against a real private GitHub repository with a
  second clone acting as the Overleaf side. Inspect network, console, and
  backend logs.
- arXiv ZIP: unpack it in an empty directory and compile it offline.
- 390px check of every changed surface.

## Risks

- **Untrusted text reaches the Coach.** Anyone with write access to the
  linked repository can put text into Paper, and a Coach turn reads Paper
  with network access. Linking gives repository writers the trust a member
  typing in Paper has. The link dialog says who can write.
- **A rejected conflict can reappear** when either side later changes the
  same region while the file is still held.
- **Restore then push removes remote work.** Restoring a version from before
  a pull and pushing takes the co-author's edits off GitHub. The push dialog
  lists them first.
- **The write probe** relies on GitHub refusing a dry-run push from a
  read-only key. The served journey checks that with a read-only key.
- **Host key rotation** blocks sync until an RCP release.
- **Unhidden reads.** On a provider that cannot hide paths, an agent shell
  could read the paper key.
- **A rename on the remote** becomes delete plus create. The file loses its
  identity and comment anchors but no text.
- **Large repositories.** A repository past the Paper limits links, but most
  of it shows as skipped.
- **GitHub only.** Another host needs its own host keys and URL rule.
