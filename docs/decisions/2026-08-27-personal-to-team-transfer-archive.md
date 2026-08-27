# Personal-to-team transfer uses one sanitized project archive

**Status:** accepted on 2026-08-27.

## Decision

A personal-to-team project transfer uses one versioned, checksummed archive as
its sole data-transfer format. The source creates it only after active project
tasks, episodes, and watchers have settled. The archive contains:

- the durable project id, accepted canonical Patch history, and exact exported
  head;
- every finished human-visible operational record: terminal task attempts,
  events, receipts, and usage; chats and durable attachments; Paper drafts and
  human-visible history; and stopped episode, watcher, and report history; and
- the bytes and metadata of explicitly kept project artifacts.

The export codec does not copy source database rows blindly. It removes every
provider-native session id, reusable-stage binding, execution host/root, live
continuation, scratch/cache pointer, credential, and machine configuration.
Imported task, chat, Paper, episode, watcher, report, and artifact records are
historical evidence. They cannot Resume or Retry through a source execution
binding; future work starts through the target team's current provider and
machine configuration.

The archive excludes source repositories and ordinary working-tree files. The
target provisioning flow has already prepared the declared central checkout set
through Git. The archive carries only RCP canonical and operational project
state plus kept artifact bytes.

Before changing target state, RCP validates the archive version, manifest,
checksums, project/home/request identities, canonical replay, record references,
and every excluded-field rule. It stages file content, inserts the selected
operational records in one SQLite transaction with explicit id mapping, publishes
canonical/kept files through their existing atomic owners, and records
idempotent step receipts. The target project becomes active only after database
and file readback succeed. A crash may leave a non-active repairable transfer,
never a partially imported project presented as ready.

## Why

One archive gives the transfer a concrete inspectable unit, deterministic
hashes, resumable transport, and one compatibility boundary. A raw ZIP of the
personal RCP directory cannot work: that directory contains the personal
space's other projects, members, sessions, credentials, and SQLite authority,
while the team already has its own live control plane. Literal row copying would
also preserve source-machine session and stage pointers that are unsafe and
meaningless on the team server.

Keeping all finished human-visible history makes the transferred project
understandable to teammates. Sanitizing it as history avoids pretending that an
old provider conversation or machine process can resume. Selecting all finished
record groups also avoids an accidental policy based on whichever tables are
easiest to copy.

## Rejected alternatives

- Copy the complete personal data directory or SQLite file: overwrites or
  merges unrelated space authority, members, projects, and credentials.
- Transfer only canonical history: loses the human-visible evidence explaining
  how the project reached that state.
- Copy operational tables literally: retains unsafe execution/session pointers
  and can accidentally reactivate work.
- Choose record kinds opportunistically during implementation: creates an
  incoherent, schema-order-dependent history policy.
- Use several independent bundles: multiplies partial-transfer states and makes
  one final review impossible to verify.
