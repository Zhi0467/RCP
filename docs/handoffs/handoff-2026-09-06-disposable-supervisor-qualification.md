# Remaining disposable supervisor qualification

Date: 2026-09-06
Status: active. Supervisor implementation and the normal production deployment
are complete. The controller fix merged in PR #77; completing the remaining
Ubuntu reboot/restore cases is follow-up work. No production action is required
by this handoff. Archive it when the remaining drives pass or the human closes
their scope, recording that distinction explicitly.

The [operations spec](../specs/server-and-machine-operations.md) and
[supervisor decision](../decisions/2026-09-02-deployment-moves-to-an-external-supervisor.md)
own current behavior. The [closed deployment handoff](../archive/handoffs/handoff-2026-09-02-external-supervisor-and-release-artifacts.md#production-closeout-2026-09-06)
contains historical implementation and redacted production receipts.

## Executed proof

Hosted run [34056860042](https://github.com/Zhi0467/RCP/actions/runs/34056860042)
used merged `7d6546f30d958451080cce3d28818eb5297b7f4b` on disposable Ubuntu 22.04
and 24.04. Both historical-source adoption jobs passed complete encrypted
backup decryption/inventory checks, member reconnection, healthy doctor, and an
offline reboot with changed boot IDs. Both update recovery jobs passed their
first 16 online reboot cases, including forward migration, checkpoint rollback,
exact canonical/stage/attachment checks, and recovery before application startup.
The fixtures are synthetic releases built from that commit, not the promoted
production release bytes. The overall workflow failed and the full matrix did
not pass.

The next repeated-rollback case reached its second intended supervisor pause.
The controller waited for cloud-init to complete during that deliberate startup
pause and timed out instead of driving the next interruption. PR #77 waits for
cloud-init only on initial provisioning while preserving the SSH, disposable
identity, Ubuntu version, and changed-boot checks on subsequent boots. Its
focused regressions and PR CI passed; the changed controller has not yet been
qualified by another full hosted drive.

The rerun [34061040236](https://github.com/Zhi0467/RCP/actions/runs/34061040236)
used merged `0ed2d707276d70704017af7157b0ca801a399e3e`. Both adoption/offline
reboot jobs passed again. Ubuntu 22.04 recovery began its first update before
the baseline service had finished booting: the journal records the deployment
at 17.77 seconds, missing control socket at 20.95 seconds, and application
startup completion at 22.69 seconds. No recovery case passed in that job.
Case preparation now waits for baseline HTTP health before arming any faults;
actual interrupted recovery boots still use the pause-aware controller path.
This correction needs its own hosted verification. Ubuntu 24.04 verified nine
online update/rollback cases, then GitHub canceled its job while the tenth was
running. Its partial receipt still says `running`; job cancellation is not a
supervisor failure or a completed drive. All four job artifacts are retained.

Production was separately adopted under supervisor `0.1.3` using promoted
`v0.3.5`, build 484. Both projects are protected with zero backup omissions;
doctor and the idempotent update check passed, and the local desktop reconnected.
The human explicitly accepted the core disposable evidence for this normal
production deployment without waiting for additional controller iterations.

## Remaining work

1. Merge the baseline-readiness follow-up through normal PR checks. Run
   `.github/workflows/supervisor-recovery-live.yml` from merged main using only
   disposable GitHub-hosted Ubuntu 22.04/24.04 guests.
2. Complete repeated rollback, protected restore, offline update/restore,
   unknown-journal refusal, and post-admission accepted-work preservation. Keep
   exact per-case receipts, source/release identities, image hashes, changed
   boot IDs, startup ordering, and data checks. Do not replace executed proof
   with unit tests or planned checks.
3. Fix any drive-found bug in a separate PR, then rerun affected qualification.
   Keep [S135](../acceptance/S135-supervisor-recovers-automatically-after-reboot.md)
   pending until its full drive passes on both Ubuntu versions. S103 and S104
   also retain their broader, separately unexecuted assertions.

Production is never a disposable guest. No production reboot, forced failure,
rollback, or restore is authorized by this follow-up. Full machine-loss checkout
and replacement GitHub-key reconstruction were closed by the human, not proved.
Keep private host identifiers and backup/key material out of GitHub artifacts.
