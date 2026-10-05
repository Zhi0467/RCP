# Watcher commands run outside the provider sandbox

Date: 2026-10-05. Status: active. Confirmed by the human on 2026-10-05.
Current behavior is in the
[providers spec](../specs/providers-and-containment.md#watcher-commands).

## Decision

A `watch.json` entry's `check_command` and `cancel_command` run as written, in a
fresh `bash -lic` shell started by RCP as the execution account, locally or over
SSH. They run outside the provider sandbox and outside the turn's write scope.
RCP bounds each run with the watcher timeout and kills the process group on
expiry. It does not parse or restrict the command.

## Why

- **A watcher must see what the job sees.** Checks call scheduler clients,
  read job receipts, and probe processes. Those need the account's login
  environment, its network, and its Unix sockets, which a provider sandbox
  removes. The [Claude Work decision](2026-09-13-claude-work-runs-without-the-os-sandbox.md)
  records how a sandbox silently broke scheduler access once already.
- **Watchers outlive the turn.** A check runs long after the provider process
  has exited, so there is no live sandbox to run it in.
- **The containment model is cooperative.** Agents, members, and prompts are
  trusted not to attack the account. Write scope guards against accidental
  writes into another project. It is not a hostile-process boundary, and Claude
  and OpenCode Work shells are already unbounded.

## What this gives up

- **A Work turn's write roots do not bound its watchers.** An agent can write a
  check that edits files anywhere the execution account can write, and RCP runs
  it on every poll. Its effects are not limited to the turn's lifetime.
- **Human Cancel runs agent-authored text.** A human pressing Cancel executes
  the entry's `cancel_command` with the same reach.

Revisit this if RCP ever claims hostile-agent containment. That change would
need a sandbox that keeps scheduler, socket, and network access.
