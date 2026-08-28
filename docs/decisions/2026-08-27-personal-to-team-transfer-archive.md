# Personal-to-team transfer uses one bounded project archive

**Status:** accepted on 2026-08-27; provider-history and cross-space authority
boundaries refined on 2026-08-28.

## Decision

A personal-to-team project transfer uses one versioned, checksummed archive as
its sole data-transfer format. The source creates it only after active project
tasks, episodes, and watchers have settled. The archive contains:

- the durable project id, accepted main and graph-branch canonical Patch
  histories, exact exported heads, immutable branch metadata, and merge receipts;
- every finished human-visible operational record: terminal task attempts,
  events, receipts, and usage; the current Paper draft; and stopped episode,
  watcher, and report history, including the finished Auto-research child,
  message, recovery, lifecycle, Apply-result, and receipt records needed to
  render that stopped episode;
- typed canonical RCP chat transcripts with display-only attachment metadata,
  the canonical Paper introduction, and opaque safe regular files under
  `.research/facts/`;
- one complete read-only historical source for every provider-native
  conversation matched to the project, including the whole selected
  conversation rather than only records after `last_refresh_at`; and
- the bytes and metadata of referenced kept project artifacts and legacy kept
  result views.

The export codec does not copy source database rows blindly. RCP's existing
native conversation index automatically makes a best-effort selection using the
transcript's recorded working path and the project's declared repository paths;
configured provider profiles supply the roots and the index keeps ownership of
local/SSH retrieval. A positive match is included in full under the saved source
profile's exact local or SSH execution account; no different provider home or
member laptop is substituted. Rewritten, unmatched, or unreadable conversations
are skipped with a non-blocking count and diagnostic. There is no
transcript-selection screen and no completeness claim. RCP-owned project chats
are carried separately as project history rather than selected again as
provider sources. The project watermark is preserved as an agent-facing
overlap boundary, not used as a deletion boundary: it does not prove that every
earlier provider record was ingested, and a conversation may cross it.

RCP chat files are not raw-copied. Transfer parses the canonical JSONL and
preserves stable RCP chat/message ids, text, timestamps, provider/model labels,
graph receipts, and display-only attachment metadata while clearing native
provider session ids, execution-machine/cwd fields, and source operation
bindings that were not deliberately remapped. The current Paper draft retains
its base/ancestor conflict content and the canonical introduction travels as a
separate file. Completed Paper-coach task answers remain terminal history, but
`writing_sessions` and `chat_session_contexts` are excluded: they are resumable
native-session/prompt indexes, not the durable human content being transferred.
Imported terminal tasks retain their honest status and answer but receive a
durable history-only marker. Backend projection and control admission both make
Pause, Resume, and Retry unavailable and expose no native-session id as an
executable continuation; future target work starts as a new task under target
configuration rather than executing a source request or pretending an imported
failure succeeded.

The source `manifest.toml` is carried only as checksummed provenance for
validation; it is not published as the target manifest. Repository and machine
aliases referenced by historical Patches and `SourceRef`s, the state repository,
and truth-scope provenance stay stable. The reviewed target request rebuilds all
live repository paths, hosts/accounts, provider binaries and native-source
roots, and profile execution choices, then replay-checks main and every retained
branch. Retained `.research` already present after the target Git clone is
accepted only when it is byte-identical compatible history for the same project
and source home and contains no archive-external canonical commit; it is never
silently overwritten or adopted as a different project.

The archive removes reusable-stage bindings, execution host/root bindings, live
continuations, scratch/cache pointers, temporary human-input attachment bytes,
credentials, and machine configuration. Chat history retains the attachment
name/type/size/expiry already visible in the transcript; it does not turn
seven-day turn context into a new durable source.
Imported provider histories live under
`<RCP_DATA_DIR>/project-sources/<project-id>/provider-history/<provider>/` with
content-addressed filenames and read-only modes. That project-owned app-data
source is never canonical `.research`, a checkout, a rebuildable cache, or the
target account's native provider home, and it receives no Resume or Retry
binding. Seed/Refresh can read it alongside new native logs produced under
whatever provider authentication is already present on the target execution
account. Local execution reads the project-owned source directly. SSH execution
copies only that bounded imported inventory into the existing immutable task
input stage, keeps live remote provider roots in place, and verifies the same
staged fingerprint on Resume. Future operational work starts through that
target machine/account configuration.

Best-effort ends when the archive is sealed. A file that was selected and
successfully imported is durable project data; if it later disappears or fails
its content-address check, Seed/Refresh reports project-source corruption rather
than silently proceeding with less history.

After the canonical home change, the personal backend retains the one sealed
archive as a mode-0600 request-derived file under
`<RCP_DATA_DIR>/transfer-exports/`. The digest receipt binds to those exact
bytes, and every relay retry re-hashes and streams them rather than rebuilding
an archive whose best-effort provider selection might now differ. A missing or
corrupt sealed archive is a visible repair state. The file and retired source
catalog row remain until the matching target-activation receipt; then the source
may unlink only that exact request file. This source-side recovery copy is
personal app data, not a target transfer inbox or team backup entry, and it
keeps ordinary project Delete unavailable while the transfer is unfinished.
The team backup does not promote a partial target inbox into durable project
state. After target restore, any old upload lease is invalid and the linked
request requires a fresh relay of the same source-bound digest; a committed
source home change remains fenced rather than being reversed.

The archive excludes source repositories and ordinary working-tree files. Only
kept filenames referenced by captured RCP metadata enter from repository-level
`artifacts/` or legacy `views/`; unrelated human files in those directories do
not become transfer data. The target provisioning flow has already prepared the
declared central checkout set through Git. The archive carries only RCP
canonical and operational project state plus referenced kept bytes. Main and
graph-branch materialized outputs remain excluded and are regenerated from their
retained immutable histories.

Before changing target state, RCP validates the archive version, manifest,
checksums, project/home/request identities, canonical replay, record references,
and every excluded-field rule. It stages file content, inserts the selected
operational records in one SQLite transaction with explicit id mapping, publishes
canonical, kept, and imported-provider-history files through their concrete
atomic owners, and records idempotent step receipts. The target project becomes
active only after database and file readback succeed. A crash may leave a
non-active repairable transfer, never a partially imported project presented as
ready.

The transfer crosses two human-authority domains. One final desktop review
records target admission through the authenticated team backend before source
release through the authenticated personal backend. Each backend persists its
own actor and idempotent request-bound receipt; their user ids need not match.
The target-only intermediate state creates no project and leaves the source
writable. The service-account import CLI is machine authority only and cannot
activate the target until both human receipts and the later source-fence/archive
receipt validate. The canonical home-change record carries both space-scoped
human actors so target history does not lose either side of the authority chain.

Before preparation or either confirmation, both requests bind one nonsecret
source-configuration digest and a mutually supported source-schema/archive-codec
version. The target reviews rebuilt team execution configuration against the
source's stable repository/machine aliases and truth-scope provenance. Source
release revalidates the digest and negotiated version before its write fence, so
configuration drift or incompatible server code fails while the personal home
is still writable rather than after authority has moved.

Because a JSON receipt relayed by a client is forgeable, each linked request
also precommits to two independent random one-time proofs. The source reveals
its proof only inside the archive sealed after its write fence; the target
verifies the commitment before activation. The target reveals its proof only
after activation commits; the native relay returns it directly to the source,
which verifies the commitment before deleting its recovery copy. Raw proofs are
request-scoped transition evidence, never member/provider/Git/SSH credentials or
imported project history, and only their consumed hashes/receipts remain.

This makes a serialized receipt, request id, archive path, or successful CLI
exit insufficient in the supported protocol. It does not claim to defend the
database from root or from the `rcp` account that owns it; machine privilege is
outside RCP's product-authority boundary.

## Why

One archive gives the transfer a concrete inspectable unit, deterministic
hashes, resumable transport, and one compatibility boundary. A raw ZIP of the
personal RCP directory cannot work: that directory contains the personal
space's other projects, members, sessions, credentials, and SQLite authority,
while the team already has its own live control plane. Literal row copying would
also preserve source-machine session and stage pointers that are unsafe and
meaningless on the team server.

Keeping all finished human-visible history makes the transferred project
understandable to teammates. Keeping the complete project-matched provider
corpus also means Seed/Refresh does not silently forget research merely because
the personal and team provider accounts use different operating-system homes.
Treating it as read-only history avoids pretending that an old provider
conversation or machine process can resume. Selecting all finished record groups
also avoids an accidental policy based on whichever tables are easiest to copy.

The source index copies each selected original native transcript file
byte-for-byte and rechecks its recorded working path before admission. RCP does
not translate it into a supposedly provider-neutral transcript: the current
normalizer is deliberately lossy, and a new translation layer could silently
drop tool calls, outputs, or future provider records. The raw file may contain
passive provider session ids, original working paths, or sensitive text printed
during the conversation. Those bytes are historical evidence, not an RCP
execution binding. They remain outside the provider home, receive no Resume or
Retry route, and are visible only under the transferred project's authority.

Conversation selection is intentionally modest. RCP does not build repository
identity inference, historical checkout discovery, or a human classification
workflow for this slice. Missing a conversation is reported but does not block
transfer; once a file is selected, truncating or rewriting it is not allowed.

After import, these project-owned historical sources, transformed RCP chats,
Paper introduction, facts, and referenced kept files are durable project data
and are included in encrypted server backup and restore. Live provider homes,
authentication/configuration stores, and newly produced native logs remain
outside backup. Otherwise a fresh restore could lose the only team-side copy and
make later Seed/Refresh less complete than it was before the failure.

## Rejected alternatives

- Copy the complete personal data directory or SQLite file: overwrites or
  merges unrelated space authority, members, projects, and credentials.
- Transfer only canonical history: loses the human-visible evidence explaining
  how the project reached that state.
- Leave native provider conversations in the personal account: makes later
  Seed/Refresh incomplete whenever the target Linux account or machine has a
  different provider state directory.
- Copy only records newer than `last_refresh_at`: mistakes an overlap-tolerant
  run timestamp for exact per-record coverage and can split a conversation from
  its required context.
- Normalize provider transcripts into one RCP message schema: silently drops
  provider-specific tool and metadata records and creates a parser that must
  chase every provider format change.
- Require a human to classify every candidate transcript: adds a high-friction
  migration surface for a best-effort contextual source rather than product
  authority.
- Promote temporary human-input attachment bytes into durable transfer history:
  breaks their existing expiry/privacy contract and turns untrusted turn context
  into a new evidence store.
- Copy operational tables literally: retains unsafe execution/session pointers
  and can accidentally reactivate work.
- Choose record kinds opportunistically during implementation: creates an
  incoherent, schema-order-dependent history policy.
- Use several independent bundles: multiplies partial-transfer states and makes
  one final review impossible to verify.
- Treat the target machine command or the source confirmation as authority for
  both spaces: lets one credential domain admit or release a project on behalf
  of a human authenticated only in the other domain.
