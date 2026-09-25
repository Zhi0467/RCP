# Job managers add rules, never remove the helper

Date: 2026-09-25. Status: active. Replaces the 2026-09-06 rule that a selected
scheduler turns the launch helper off.

## Decision

The launch helper is offered wherever RCP can resolve an OS process owner
(systemd user manager on Linux, launchd on macOS). A configured job manager,
Slurm today, adds its own rules to the execution instructions: Slurm first for
compute jobs. It does not remove the helper. A future job manager or detach
mechanism is added the same way, as one profile with its own instructions and
readiness.

## Why

With Slurm set, the helper was the only way to own a non-compute process, and
it was switched off. On 2026-09-25 a Codex Work turn needed a dashboard to
outlive the turn. The only owner left was Slurm, so the agent held a scheduler
slot for a web server with `high` QoS and no time limit.

"No silent fallback" still holds. The agent chooses a route from explicit
instructions; RCP never reroutes a Slurm submission to the helper.

## Consequences

- Linux machines set to Slurm now need a reachable user manager for helper
  launches. Install already enables linger for the service account.
- Readiness reports both routes per machine.
