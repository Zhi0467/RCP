---
id: S135-supervisor-recovers-automatically-after-reboot
status: pending
tier: live
driver: pytest + ssh + systemd + vm
covered_by:
  - tests/test_supervisor_operations.py
  - tests/test_supervisor_checkpoint.py
  - tests/test_supervisor_driver.py
  - tests/test_supervisor_reboot_vm.py
  - tests/supervisor_reboot_live.py
  - .github/workflows/supervisor-recovery-live.yml
invariants: [6, 7, 8, 9]
last_checked: >-
  2026-09-06 — human confirmed automatic recovery and an actual reboot proof.
  The supervisor implementation and disposable Ubuntu reboot harness are coded.
  Local regression and fixture checks pass. Hosted run 34054020695 booted real
  Ubuntu 22.04/24.04 guests but failed during installation/fixture setup before
  any recovery case or changed-boot proof. This scenario remains pending.
---

# A reboot cannot bypass deployment recovery

This scenario is human-confirmed on 2026-09-06. The deployment ownership and
recovery contract are in the
[supervisor decision](../decisions/2026-09-02-deployment-moves-to-an-external-supervisor.md).

## Setup

Disposable Ubuntu 22.04 and 24.04 virtual machines run an installed RCP release
with representative team data. The test controller runs outside the guest so it
survives the guest reboot. The previous and target release artifacts and the
protected restore archive are local and verified. The target includes a forward
storage migration. No drive uses the lab server's live data directory.

## Drive

1. Begin an authorized update. At each durable checkpoint, switch, and rollback
   publication boundary, interrupt the coordinator and reboot the guest.
   Record the Linux boot ID before and after; require them to differ.
2. Observe systemd invoking supervisor recovery without a human rerunning the
   command. Verify that RCP cannot admit mutations or background effects until
   the supervisor has verified the recovered release and state.
3. Verify the selected release's identity and exact applicable checkpoint bytes,
   including local canonical roots, stages, and attachments. Exercise recovery
   after the candidate has applied the forward migration and after a second
   interruption during rollback. The old release must never open migrated data.
4. Repeat for protected-archive restore, interrupting candidate publication and
   checkpoint restoration. Verify that no mixed live data tree is served.
5. Repeat with guest network access disabled. Recovery uses the same authorized
   local artifacts and journal; it neither fetches a release nor needs GitHub.
6. Supply an inconsistent or unsupported journal. Reboot and verify RCP remains
   stopped with an actionable diagnostic rather than guessing a recovery path.
7. Complete activation, accept new work, and reboot at the post-activation
   recovery boundary. Verify the accepted work survives; the old checkpoint
   must no longer be eligible for automatic restoration.

## Assert

- `a_changed_boot_id_proves_an_actual_guest_reboot`
- `systemd_runs_recovery_without_operator_reentry`
- `recovery_precedes_application_admission_and_background_effects`
- `recovery_after_forward_migration_never_starts_old_code_on_migrated_data`
- `interrupted_update_and_restore_recover_without_mixed_roots`
- `recovery_uses_local_artifacts_without_network_access`
- `unknown_recovery_state_keeps_the_service_stopped`
- `post_activation_recovery_preserves_subsequently_accepted_work`

Record the guest versions, release identities, boot IDs, injected boundaries,
systemd ordering, and data-verification results in the active supervisor handoff.
Leave this scenario pending until the real reboot drives pass on both Ubuntu
versions; process-restart and fake-service tests are necessary supporting checks.
