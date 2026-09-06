# Dev team space and source server completion handoff

Date: 2026-08-27
Last revised: 2026-09-05
Status: completed and archived on 2026-09-05. The real CoT project is transferred
to WTH UCSD, visibly open in the desktop, with both transfer requests completed,
canonical history and provider originals verified, and independent backups kept.
The human accepted the existing two production members as the two-user proof
and skipped separate disposable-host SSH qualification. The final transfer
defects are fixed and tested; their PR still requires CI and human merge.

This is historical closure evidence, not an active implementation contract.

## Authority at closure

Use current [design](../../design.md), [operations spec](../../specs/server-and-machine-operations.md),
[desktop spec](../../specs/api-web-and-desktop-projections.md), and repository
instructions. Development uses short-lived branches, PR CI, and human merge;
there is no remaining direct-main exception in this handoff. Servers consume
merged main until the supervisor cutover.

This is a human-approved completion criterion, not a claim that every old test
fixture has been exercised. Preserve unrun acceptance coverage as explicit
gaps; it no longer blocks this one-lab handoff.

## Implemented and qualified

- Installation, doctor, source update, forced rollback, protected backup, member
  removal, and fresh-host restore passed hosted Ubuntu 22.04/24.04 qualification.
  The final recorded run is 33919068513 at merged revision 276a2bb.
- Production WTH UCSD updated normally to 276a2bb on 2026-09-04; the desktop
  built from that revision reconnected. Doctor was healthy/aligned with
  protected backup. Record preservation was verified. Do not redeploy merely
  to match a later documentation-only commit.
- Backup during an active production task passed independent decryption,
  archive hashes, SQLite integrity, and task-preservation checks.
- A small disposable transfer passed source release, personal-backend restart,
  explicit manual operator import, native proof return, source retirement,
  and canonical content comparison. Its registration-only deletion preserved
  the checkout, canonical files, and repository deploy key.
- Codex and Claude maintenance passed through the installed CLI.
- Two-member production use is complete for this handoff, accepted from the
  human's 2026-09-05 confirmation of two production users. This is human-attested
  completion, not a new automated session-isolation receipt.

Detailed dated receipts and the superseded packet plan are preserved in the
[qualification history](handoff-2026-08-27-dev-team-space-and-server-qualification-history.md).
Earlier packet evidence remains in the
[implementation archive](handoff-2026-08-27-dev-team-space-and-server-evidence.md).

## Scope of the completed drive

The human authorized one real-project transfer through the desktop, with a
verified independent backup first. The existing installed CLI performed target
setup and explicit manual import through the authorized operator tmux.
No research was launched, no database or canonical files were edited by hand,
and no project deletion was tested. The source checkout and backups remain.

Restoring the whole personal database could rewind unrelated projects; any
future recovery requires an explicitly scoped plan.

## CoT transfer preflight receipt — 2026-09-05

The identified project is **Loop steer**, id
`1b584ca8-0049-4e5b-9b53-c1b6d913a252`, in personal space
`8498e05a-c93a-4423-ba82-be7902d65e00`. Canonical state is at
`tianhaowang-gpu0.ucsd.edu:/home/zhiwang/cot-loop/.research`, revision 2.
The source Git origin is `git@github.com:Zhi0467/cot-loop.git`; its clean branch
`codex/greedy-multirun-cleanup` is at `d55aa8009e801ea535b6a88818812e9e277d1c31`,
ten commits ahead of its recorded upstream. Those commits must remain preserved;
the ordinary team wizard creates its new checkout from GitHub.

Before creating any transfer request, retained mode-0600 backups were verified:

- Mac directory:
  `/Users/zhiwang/Library/Application Support/RCP Backups/cot-loop-before-team-20260905-1b584ca8/`.
  `personal.sqlite3` is a consistent online SQLite backup: integrity `ok`,
  zero foreign-key violations, project row present, one succeeded Seed task
  and one old paused Seed task, no project episodes.
  SHA-256 `ffd61bc7f67db8196f3e0100ad3d678a9f71e7f92ae2c23546218a0a6d4a0806`.
  `personal-files.tar` retains the app's manifests, attachments, snapshots,
  caches, and existing transfer-export directory.
  SHA-256 `22fe567a7fc83f1f2a0348ff4dc88aef3adebbea56de1a48c7a0c84bbbc50f9f`.
- Server directory:
  `/home/zhiwang/rcp-project-backups/cot-loop-before-team-20260905-1b584ca8/`.
  `cot-loop.tar` is a 4.1 GB archive of the checkout, including `.git`,
  `.research`, ignored reports, and logs; only `.venv` and `.ruff_cache` are
  excluded. GNU tar comparison against every archived source member passed.
  SHA-256 `50412458d07eb29142a79c6916c1b456eecd077dc1ae180e017324663c9c26b8`.
  Git HEAD and clean status were unchanged after capture.
- `research.tar.gz` is retained on both machines, with matching SHA-256
  `5e4e4535277c386454074c9cda7c19db10cb28fedffc21e13218a7a4ed8fb70f`.
  Its listing includes both accepted Patches, manifest, scope-base, and existing
  materialized files. The source `outputs` symlink is preserved as a symlink;
  external `/data/users/zhiwang/cot-loop-spill/outputs` bytes are not duplicated
  or changed by this RCP transfer.

Do not remove these backups after completion. No source release has occurred
at this preflight boundary.

### Live continuation — 2026-09-05

The human approved both target machine aliases (`laptop`, `remote-1`) as local
to WTH UCSD's `rcp` account, not the Mac. They also explicitly approved the
repository-only write deploy key with fingerprint
`SHA256:j05vot7/iIGju1SA2Zuyb6eXWMmbHbVra/0CSdIKp3g`; GitHub key 162388809
was added to `Zhi0467/cot-loop`. Do not revoke it while the prepared project
depends on it. No source-repository commits were pushed.

- Source request: `fa7bc2ae-f959-4038-be60-a1a03dc97e37`.
- Target/provisioning request: `2e57d889-438a-4394-ba1d-b1d3bb6d30df`.
- Target space: `d54275e0-3786-434b-8db7-0a55e6549aa5`.
- Prepared central checkout:
  `/home/rcp/rcp-server/projects/1b584ca8-0049-4e5b-9b53-c1b6d913a252/repositories/loop-steer`.
- The desktop operator bridge correctly refused noninteractive sudo. The exact
  installed `server project provision` command completed through the human's
  sudo-authorized `rcp-update` tmux; Git write/readback/deletion and all six
  provider roles passed. Final review revision 13 was published at
  16:15:25 UTC with digest
  `a06be918e99d99226e08776b2802e6e631fdb6a1d3a28c5cfa8803922687ccfb`.
- `/Applications/RCP.app` was an old September 3 copy and repeated the admission
  HTTP 415 already fixed by PR #42. Rebuilt current-main desktop, quit through
  the application menu, replaced that copy, and verified identical binary
  SHA-256 `3cff35f9fca5320b06a5155f9d551cbc1c155c98706ae71760cce3782e60264c`.
  No source fix was needed for that stale-binary failure.
- The saved wizard URL, including both request ids, was reopened through the
  visible development inspector. The ordinary project menu starts a new move
  instead of finding this request, and the startup URL is reduced to its origin;
  neither route automatically resumed the saved request. Do not create another
  request pair to work around this recovery gap.
- Target admission passed. Source release was recorded at 16:34:39.868914 UTC,
  request revision 3, phase `source_released`, then returned HTTP 409. The exact
  read-only storage settlement check reports `project transfer requires every
  agent task to be settled`: Seed `6a06feff-3c28-4ac5-9f21-47fd611716ce` is still
  `paused`; the other Seed is `succeeded`. The wizard had reported zero active
  tasks. No canonical home-transfer Patch or sealed export has been produced
  at this boundary, and the target project is not activated. Source new-work
  admission is fenced by the recorded release.

The human approved closing that paused attempt as interrupted while retaining
its history and scratch. The source transfer now settles paused standalone
attempts transactionally after confirmed release; any other live work rolls the
settlement back. Export remains strict and read-only. Source recovery decisions
also allow the desktop to resume settlement/sealing from `source_released` or
`source_fenced`, using the original human receipt rather than asking for a
different graph-head confirmation. No global task transition or archive schema
was widened, and no database rows were edited by hand.

The fix passed 3,675 backend tests (9 skipped), 606 Web tests, the Web build,
Ruff, and all pre-commit hooks. A consistent copy of the real database separately
proved that the old paused Seed becomes interrupted, the successful Seed stays
successful, and both retained scratch paths survive export. The rebuilt desktop
then advanced the real source to `archive_bound` (request revision 6), revision 3
of the canonical graph, and sealed its matched complete provider histories.
The server was not redeployed; it remains at merged 276a2bb. The source-only
fix and documentation are submitted through the closure PR; local tests are
not a claim of GitHub CI or human merge.

### Completed production transfer — 2026-09-05 evening

- The sealed archive is 465,561,600 bytes, SHA-256
  `8c2cf27bf6fc14bbbfc0f88c00fa58b0c812ca396507b59e9630df4828328920`.
  RCP's archive reader validated it. It contains 129 provider-history originals,
  four canonical-history entries, source manifest provenance, terminal
  operational records, and the protected source-release proof. The best-effort
  diagnostics omit 14 malformed and 4,074 unmatched provider files; they are not
  silently treated as captured project history. No proof bytes were exposed.
- The native source-release request timed out during capture, but the backend
  continued successfully. Refresh recovered `archive_bound`; do not click
  release again while capture is still running. This is a remaining progress/
  timeout usability gap, not lost history.
- The native Save dialog saved the exact mode-0600 archive to
  `/private/tmp/rcp-cot-relay.IBQ2bu/fa7bc2ae-f959-4038-be60-a1a03dc97e37.rcp-transfer`.
  The saved-archive picker subsequently verified and resumed that same file;
  keyboard row selection and Return worked. Its earlier apparently disabled
  accessibility controls were not proof that the picker itself was broken.
- An expired local team proxy was recovered through Home → Open team space →
  Exit team space. The saved wizard URL was reopened, the archive reselected,
  and Open command again completed without the earlier team-health error.
- SSH copying repeatedly disconnected. Bounded resumable copying eventually
  produced the exact complete mode-0600 archive at
  `/tmp/rcp-cot-transfer-20260905-2e57d889/fa7bc2ae-f959-4038-be60-a1a03dc97e37.rcp-transfer`.
  Size and SHA-256 matched the local archive before import.
- After the human refreshed sudo, the installed CLI imported those verified
  bytes through `rcp-update`. Upload and activation both succeeded, exit 0,
  at 2026-09-06 00:34:14 UTC (September 5 local time).
- Desktop **Check target and finish** verified the activation proof and completed
  source retirement. It displayed **Transfer complete**. Source request:
  `completed`, revision 10; target request: `completed`, revision 6.
- The personal index no longer lists Loop steer. Its WTH UCSD card opens the
  same project id, graph revision 3, 40 nodes, eight hypotheses, and the expected
  home-change explanation. The target registration has the correct team home,
  no retirement marker, and the reviewed central manifest path.
- The two original accepted Patches are identical on source and target:
  `000001.json` SHA-256
  `98a92b254ebd86e6f7f7cef061c75c7a195f7c7d74eb548f5b58c603903fb1cc`;
  `000002.json` SHA-256
  `d4aa5be11d01bc7313831de49afa1b11bf5abe4fcdb4515b029c53b5780560b6`.
  The expected home-change Patch `000003.json` matches on both:
  `530fcb1a642155053c98bdbedc5efbe0dcc16a44e89949135663402268d90134`.
- Both Seed histories are on the target: the succeeded attempt remains
  succeeded; the approved paused attempt is interrupted, retaining its original
  finish time. Source scratch references were not rewritten; reusable scratch
  is deliberately not imported as execution authority.
- RCP's installed inventory validator verified all 129 imported provider originals:
  128 Codex and one Claude, 465,291,748 payload bytes, fingerprint
  `a397be6a08cbac84bb3c8e23b6196c9b2df5b66140800bc412ff4dbf11a01458`.
  The project had no operational episodes, watchers, separate Paper draft,
  or retained artifact payloads to exercise; these empty categories do not prove
  the richer acceptance fixtures.
- Original checkout HEAD remains
  `d55aa8009e801ea535b6a88818812e9e277d1c31`, clean. Its ten unpublished commits
  remain there and in the independent Git-inclusive backup. The new central
  checkout is the GitHub revision
  `ab8fb2e55e050060d1ec612ce29425d565dd7a5c`; only RCP's `.research/` is untracked.
  This transfer did **not** publish those local Git commits or copy external
  outputs. Any later synchronization of that research code needs its own review.
  The difference includes live-steering implementation and experiment setup,
  not only documentation. Do not resume the latest CoT experiment from the
  older central checkout until that separately authorized Git synchronization
  is complete; successful RCP ownership transfer is not code-version parity.
- All independent backup files remain private and present. The shared tmux's
  sudo timestamp was invalidated and its shell closed after verification.
  The protected manual relay copies remain; their proof is consumed.

## Remaining non-blocking rough edges

The saved-request discovery gap, long-capture request timeout, and native
operation's stale team-proxy recovery required manual navigation in this drive.
They are not fixed by the paused-attempt patch and are not evidence of automatic
recovery. Repeated SSH bulk-copy disconnects were observed, not attributed to an
RCP code defect. These gaps do not erase the completed ownership/content proof.

## Deliberately skipped coverage

The human removed these from the blocking closure gate on 2026-09-05:

- separate reachable/unreachable SSH execution-account and operator-route
  qualification on disposable machines;
- a new artificial two-member desktop enrollment drill, because production
  already has two users;
- the earlier exhaustive V1 fixture matrix beyond existing production/hosted
  receipts and the final real-project transfer.

Unattended/partial-stream relay, every interruption boundary, populated
backup/restore permutations, and independent cookie/Keychain isolation checks
remain unqualified where acceptance records lack evidence. Preserve those gaps
without claiming success or changing supported behavior. The existing desktop
SSH transport to WTH UCSD is still needed for this transfer.

The Dark Matter attribution failure belongs to
[S41](../../acceptance/S41-bounded-experiment-control.md#open-live-failure--2026-09-04);
it is separate from this transfer and the revised server closure.

## Closure and next work

The human-confirmed one-lab closure condition is met. This handoff and its
temporary surface freeze are archived; exhaustive unrun acceptance coverage
remains explicitly pending.

The [external-supervisor handoff](handoff-2026-09-02-external-supervisor-and-release-artifacts.md)
is ready to dispatch: finish the remaining public-source cleanup, then implement
the independent supervisor package, cutover, deletion, and lab qualification.
Its predecessor gate is removed. Retain the private CLI connection for
non-deployment operations, as the human confirmed. No supervisor implementation
or deployment completion is claimed by this closure.
