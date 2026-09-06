# Team server operator guide

This guide is the terminal workflow for one artifact-installed RCP team server. It is
written for the machine operator who has `sudo` on a disposable or dedicated
Ubuntu host. The supported host is Ubuntu 22.04 LTS or Ubuntu 24.04 LTS on
x86-64 with systemd.

The root [README](../README.md#team-server) points here;
this document is the single complete server setup and operations procedure.

The `rcp server` CLI is the complete machine workflow and is itself a continuous
terminal wizard. It keeps one current-step line on screen instead of dumping its
internal plan or every completed step. At a human boundary it names the machine
or external service, gives the required action and success signal, and waits;
pressing Enter runs declared terminal actions and continues the same operation.
Every stop also prints an exact command that can continue later. Failures print
bounded diagnosis and exact diagnostic/recovery commands instead of raw command
output. `--machine-readable` is the noninteractive append-only JSON event stream;
it never prompts or runs a human action.

Use the numbered sections for a fresh installation. For an existing server,
jump directly to [member invitations](#invite-another-person-to-the-team-space),
[provider authentication and updates](#11-provider-authentication),
[service inspection](#inspect-and-stop-the-service),
[release updates](#update-the-server-release),
[backup](#back-up-the-team-server), [restore](#restore-a-protected-archive), or
[member removal](#remove-a-team-member).

## 1. Connect as the ordinary server operator

The operator needs SSH access and `sudo`. Do not log in as `rcp` or create that
account yourself; RCP's installer owns it.

```bash
ssh operator@server.example
sudo -v
```

Keep this SSH session open while following the remaining steps.

## 2. Confirm the host

Run these as the ordinary named operator, not as `rcp`:

```bash
uname -m
. /etc/os-release
printf '%s %s\n' "$ID" "$VERSION_ID"
systemctl show --property=Version --value
```

Success is `x86_64`, then either `ubuntu 22.04` or `ubuntu 24.04`, followed by a
nonempty systemd version. Do not continue on a container without a running
systemd manager.

## 3. Install Ubuntu prerequisites

The same prerequisite command applies to both supported Ubuntu releases:

```bash
sudo apt-get update
sudo apt-get install --yes age ca-certificates curl git iproute2 libc-bin openssh-client openssh-server passwd sudo util-linux xz-utils
```

Success is an exit status of zero. Then install system-wide `uv` below.

## 4. Install system-wide uv

Install the selected `uv` release into `/usr/local/bin` without
changing a user's shell profile. The archive digest is pinned from the immutable
upstream 0.12.7 release rather than trusting a downloaded installer script:

```bash
(
  RCP_UV_VERSION="0.12.7"
  RCP_UV_ARCHIVE="uv-x86_64-unknown-linux-gnu.tar.gz"
  RCP_UV_SHA256="788f18abea7c5f55d6216e4f5613fd89d4d59b631efeec117b2b07fe72f1da21"
  RCP_UV_DOWNLOAD_DIR="$(mktemp -d)"
  cd "$RCP_UV_DOWNLOAD_DIR"
  curl --fail --show-error --location --remote-name "https://releases.astral.sh/github/uv/releases/download/${RCP_UV_VERSION}/${RCP_UV_ARCHIVE}"
  printf '%s  %s\n' "$RCP_UV_SHA256" "$RCP_UV_ARCHIVE" | sha256sum --check --strict
  tar --extract --gzip --file "$RCP_UV_ARCHIVE"
  sudo install --owner=root --group=root --mode=0755 "uv-x86_64-unknown-linux-gnu/uv" /usr/local/bin/uv
)
uv --version
```

Success is output beginning with `uv 0.12.7`; the upstream binary appends its
build hash and date. RCP later invokes this binary as `rcp` to install that
account's application-owned Python 3.12. Do not create `/home/rcp` or install a
Python there yourself.

Finally check every system prerequisite:

```bash
git --version
ssh -V
age --version
command -v age age-keygen curl getent git runuser ssh ssh-keygen sudo systemctl useradd uv
```

Success is a path for every command and age major 1. The server installs prebuilt Python and Web assets; Node.js and npm are not server installation prerequisites.

## 5. Select the paired promoted release wheels

Open the [latest promoted RCP release](https://github.com/Zhi0467/RCP/releases/latest).
It must contain all five assets: the stamped RCP wheel, `requirements.lock.txt`,
the independently versioned supervisor wheel, `supervisor-requirements.lock.txt`,
and `manifest.sha256`. Copy the two wheel download links from that same release.
A prerelease, build tag, `main`, or older release missing supervisor assets is
refused explicitly.

## 6. Run the paired-wheel bootstrap

Set the two URLs to the copied public download links, then enter the command as
root with the intended team name:

```bash
RCP_WHEEL_URL='paste the RCP wheel download link'
RCP_SUPERVISOR_WHEEL_URL='paste the supervisor wheel download link from the same release'
sudo /usr/local/bin/uv tool run --from "$RCP_WHEEL_URL" --with "$RCP_SUPERVISOR_WHEEL_URL" rcp server install --team-name "My lab"
```

The two-wheel invocation is the explicit trust boundary for first installation.
It gives the disposable bootstrap access to the matching supervisor without
adding a permanent supervisor dependency to RCP. Bootstrap checks that both
versions still match promoted stable, verifies the full five-asset bundle, and
creates the dedicated `rcp` account. No source checkout, source deploy key, or
operator GitHub credential is installed.

## 7. Independent supervisor and recovery ownership

The supervisor owns `/etc/rcp/supervisor`, including its own root-owned Python,
versioned environments, selected-release receipt, and recovery journal.
`/usr/local/bin/rcp-supervisor` is the root-owned entry point. Application releases
live at `/home/rcp/rcp-server/releases/<build>/.venv` and are installed as `rcp`.
The application data, project, credential, and `/etc/rcp/current` paths remain
fixed. The `rcp` account receives no general sudo authority.

The retained privileged `server backup configure` and `server provider update`
commands run from a separate root-owned operator environment pinned to the
selected application build. That environment is installed from the same verified
application wheel and locked dependencies; it does not add application imports
to the supervisor runtime.

`rcp.service` still runs as `rcp`. Its root `ExecStartPre` recovery guard finishes
an interrupted transaction before the application starts. It needs no release
network request during reboot recovery. The root-owned `selected.json` binds
the promoted tag, build, full Git commit, manifest digest, and release path.
The launcher exports that identity for application and backup metadata.

An initialized source installation uses an explicit one-time adoption journal.
It retains the original launch files, installs the reboot guard, stops the old
service, and preserves opaque original bytes before current application code
interprets copied data. Candidate admission requires a complete protected backup
and current application verification. A failure restores original data and
launch authority before old source code can run again; a durable committed
selection resumes only the chosen release.

`--machine-readable` emits the sealed noninteractive event stream. Exit status
3 marks an operator action; it never reads terminal input or runs that action.

## 8. Save the code when the running wizard asks

On a fresh installation, the same wizard pauses before activation and shows the
team initialization command it is about to run. Press Enter. RCP runs the
equivalent of:

```bash
sudo -u rcp -H /usr/local/bin/rcp space init --team --name "My lab"
```

The command prints one bootstrap enrollment code once. Store that code outside
logs and command history; the service never needs it. The wizard then waits a
second time specifically for you to confirm that the code is saved. Press Enter
only after saving it. RCP re-enters installation, enables and starts the service,
and verifies health without returning you to a shell between those steps.

## 9. Finish installation and verify the service

When the original installer command exits successfully, verify its result:

```bash
curl --fail --silent http://127.0.0.1:8421/api/health
sudo -u rcp -H /usr/local/bin/rcp server doctor
```

Health must identify `status` as `ok`, `space_kind` as `team`, and the
expected `space_name`; doctor must report a healthy installed release.

After success, confirm ordinary systemd re-entry:

```bash
sudo systemctl restart rcp.service
curl --fail --silent http://127.0.0.1:8421/api/health
```

The independent supervisor recovers locally before systemd admits the service.

## 10. Configure one operator route

RCP does not enable SSH password authentication, create a human Linux account,
or edit sudo policy. Choose one route deliberately.

### Preferred: named operator with one narrow command

As root, replace `alice` with an existing named Linux account. Create this file
with `visudo`:

```bash
sudo visudo --file=/etc/sudoers.d/rcp-project-provision
```

Its one line is:

```text
alice ALL=(rcp) NOPASSWD: /usr/local/bin/rcp server project provision * --machine-readable
```

Then validate and probe it:

```bash
sudo visudo --check --file=/etc/sudoers.d/rcp-project-provision
sudo -u alice -H sudo -n -u rcp -H /usr/local/bin/rcp server project provision 00000000-0000-4000-8000-000000000000 --machine-readable
sudo -u alice -H sudo -n -u rcp -H /usr/bin/id
```

The first command must say the file parsed successfully. The fixed RCP command
runs only the named provisioning request. The unlisted `/usr/bin/id` command
must be refused. The UUID parser and fixed desktop argv keep this rule from
becoming a general command surface.

### Development alternative: direct key-only rcp SSH

Only when direct service-account SSH is intentionally wanted, install one
operator public key:

```bash
sudo install --directory --owner=rcp --group=rcp --mode=0700 /home/rcp/.ssh
sudo install --owner=rcp --group=rcp --mode=0600 /absolute/path/to/operator-key.pub /home/rcp/.ssh/authorized_keys
ssh -o PreferredAuthentications=publickey rcp@server.example /usr/local/bin/rcp server doctor
```

The `install` command replaces `authorized_keys`; if that file already exists,
review and merge keys with `sudoedit -u rcp` instead. Password login remains
impossible because the account has the exact unusable `*NP*` shadow value. RCP
does not change global `sshd_config`.

## 11. Provider authentication

RCP does not log in to Codex, Claude, or a later provider. Authenticate with the
provider's native command under the operating-system account that will execute
it. For server-local execution that account is `rcp`.

Stay in the ordinary operator SSH session. You do not need to log in directly as
`rcp`, enable its password, or open an interactive shell as it. Prefix each
provider command with `sudo -u rcp -H`; `-H` makes the provider store its binary,
settings, sessions, and credential under `/home/rcp` instead of the operator's
home.

### Codex

If `codex` is not already installed for the service account, use OpenAI's
standalone installer:

```bash
sudo -u rcp -H /bin/bash -lc \
  'curl -fsSL https://chatgpt.com/codex/install.sh | sh'
```

On a remote or headless server, use device-code login. Open the displayed URL in
the operator's local browser and enter the displayed one-time code there:

```bash
sudo -u rcp -H /bin/bash -lc 'codex login --device-auth'
```

Confirm that the credential belongs to `rcp` and is usable:

```bash
sudo -u rcp -H /bin/bash -lc 'command -v codex && codex login status'
```

### Claude Code

Install Anthropic's recommended native build into `/home/rcp/.local`:

```bash
sudo -u rcp -H /bin/bash -lc \
  'curl -fsSL https://claude.ai/install.sh | bash'
```

Start the Claude subscription login. Open the displayed URL in the operator's
local browser. If Claude asks for a returned code, paste it only into this
terminal prompt:

```bash
sudo -u rcp -H /bin/bash -lc 'claude auth login --claudeai'
```

Confirm the installed binary and authentication state:

```bash
sudo -u rcp -H /bin/bash -lc \
  'command -v claude && claude --version && claude auth status'
```

The login commands may be rerun safely if the SSH connection closes before the
browser flow finishes. Never paste a provider token, returned login code, or a
provider credential file into RCP, a command argument, a log, an issue, or chat.
RCP only invokes the provider-native executable and checks its native status.

### Update provider CLIs

Stay in the ordinary operator SSH session for updates too. Do not enable a
password or direct login for `rcp`, and do not run provider maintenance under
the operator's home. RCP wraps each supported provider's native update, runs it
under the `rcp` account, keeps its output bounded, and verifies the resulting
executable, version, and existing login:

```bash
sudo /usr/local/bin/rcp server provider update codex
sudo /usr/local/bin/rcp server provider update claude
```

The Codex command reruns OpenAI's supported standalone installer under
`/home/rcp`; the Claude command runs `claude update`. These are the current
provider-owned update paths documented by
[OpenAI](https://learn.chatgpt.com/docs/codex/cli) and
[Anthropic](https://code.claude.com/docs/en/cli-usage). RCP does not download or
store provider credentials and an update never substitutes for login.

RCP runs the Codex installer in its supported noninteractive mode, so it does
not ask whether to launch Codex or remove an older npm-managed installation.
RCP leaves that older installation in place, gives the account-local standalone
command in `/home/rcp/.local/bin` precedence, then checks that command and the
existing login before reporting success. The older system installation can be
removed separately after the server is qualified; it does not need to be
removed during this update.

If the provider updated but its native login is unavailable, the same command
stops with the exact `sudo -u rcp -H ... login` recovery command. Complete that
browser/device flow in the operator terminal, then use the printed Continue
command. If an older project recorded a version-numbered executable before RCP
preserved provider symlink paths, use **Resolve** once for that provider in
Project Settings, then rerun `server provider check --project <project-id>`.
Future native updates retain the stable command path.

After the project wizard names its project id, run the exact readiness command
it prints:

```bash
sudo -u rcp -H /usr/local/bin/rcp server provider check --project <project-id>
```

Installing or authenticating a provider does not silently add it to an existing
project. Select that provider for an agent profile in project setup, then run the
printed check. RCP does not copy a member's personal provider directory or store
the credential itself.

## 12. Add the team space in the desktop app

In the source-built desktop app, choose **Add team space**, select SSH, enter the
saved server route, and enroll with the one-time bootstrap code from Step 8. The
unified project wizard can then create a team project from GitHub or move an
existing personal RCP project into the team space.

### Move a personal project, including unpublished commits if wanted

From the personal project's Settings, choose **Move to team space**, select
the enrolled team, and review its repository/machine mapping. The same setup
wizard handles the server checkout and repository-scoped GitHub grants.

**Include local unpushed commits** starts unchecked. Leave it off to use the
GitHub version of the code; commits that exist only in your personal checkout
will stay there. Check it to copy each repository's currently saved commit and
its history directly to the team, without pushing anything to GitHub. Review
shows the exact commits. Commit any changes you want included first:
uncommitted files, other local branches, and external data/output directories
are not included. Keep a separate backup of those files.
Committed files and reachable history are copied as saved, including any large
files or secrets already committed there; review what you are sharing.

When this option changes the team checkout's revision, it has **detached HEAD**
at the reviewed commit. An already-matching checkout is verified and left
unchanged. Its GitHub origin is unchanged. Create a branch before starting
new Git commits from detached HEAD. RCP does not overwrite a dirty destination or tracked research
state to make the transfer pass. If the source HEAD changes, start a fresh
review; if export/import is interrupted, resume the same saved request/archive.
Update both source desktop and target server before using this option; an older
target rejects it before the personal project is released.

This option currently requires ordinary full Git clones, not linked worktrees,
shallow clones, submodules, or Git LFS/filter-managed contents. Those cases
refuse explicitly; use a supported clean checkout and start a fresh review,
or leave the option off and handle that repository's content separately.

### Invite another person to the team space

The invitation is an RCP membership secret, not an SSH credential. The person
joining must separately have an SSH account that can reach the lab server; they
do not need the `rcp` Linux account, server-operator sudo, or access to another
member's provider credential.

As an existing member:

1. Open the team-space project index and select your identity in the top bar.
2. Under **Team invitations**, select **Invite member**.
3. Copy the invitation while it is visible and send it privately to the person
   joining. Do not put it in a URL, issue, log, or command argument.
4. Keep or close the panel. The raw code is shown only when created, while its
   nonsecret status remains under **Created by you**.

As the person joining, from their own source-built RCP desktop app:

1. On the personal project index, select **Add team space** and
   **Bootstrap or invitation code**.
2. Enter their own SSH route, such as `alice@lab-server`, and server port
   `8421`. This SSH account only carries the loopback tunnel.
3. Enter their display name and paste the bootstrap or invitation code into the
   secret field.
4. Select **Add team space**. RCP exchanges the single-use code once, stores the
   resulting permanent member credential in that person's operating-system
   credential store, and opens the team space as that member.

Back in the inviter's identity panel, **Refresh** updates both the quiet
**Team members** roster and the invitation ledger. A pending code says
**Waiting for someone to join**; successful enrollment says **Name joined**.
Expired, locked, and revoked codes remain inert and are labelled accordingly.
Only the member who created an invitation sees its ledger entry.

Joining a team space does not grant access to every project. To add the new
person to an existing project, open that project, go to **Settings → Members**,
select the newly enrolled team member, and choose **Invite**. They then accept
the separate project invitation card on their project index. Project
invitations carry no enrollment secret.

## 13. Create the first shared project

From the enrolled team space, open the ordinary project wizard and choose
**Create a shared team project**. Enter the GitHub repository, project name,
execution machine, and provider profiles. RCP creates one durable request and
shows its exact request id and operator command. Run that command as `rcp`, or
use the configured desktop operator route:

```bash
sudo -u rcp -H /usr/local/bin/rcp server project provision <request-id>
```

Leave the wizard running. For each repository, GitHub setup pauses for a
repository-scoped public deploy key. Add it to that repository with **Allow
write access** enabled, return to the terminal, and press Enter. The server does
not log in to a GitHub user. Each project keeps its own write-enabled key.

If GitHub reports that the repository is empty, push the intended codebase or a
visible first commit through the ordinary human Git workflow, then press Enter
to resume the same request. RCP does not create a hidden initialization commit.
After the CLI reaches **ready for review**, return to the project wizard, review
the exact checkout, Git, machine, and provider answers, then choose **Create
project**. RCP creates canonical `.research/` state only after this final human
action. That state is not silently committed to the repository's human Git
history.

## Inspect and stop the service

```bash
sudo systemctl status --no-pager rcp.service
sudo journalctl --unit=rcp.service --no-pager
curl --fail --silent http://127.0.0.1:8421/api/health
sudo systemctl stop rcp.service
sudo systemctl start rcp.service
```

The listener is intentionally loopback-only. Team desktops reach it through an
SSH tunnel; opening port 8421 publicly is not a supported deployment.

## Update the server release

```bash
sudo /usr/local/bin/rcp server update
```

The supervisor resolves promoted stable, shows the exact release tag, build,
full commit, and manifest digest, and asks for confirmation of
`vX.Y.Z:<manifest-sha256>`. It installs a separate verified release as `rcp`,
requires a complete protected backup, closes application admission, and verifies
copied state before changing live data. A fenced candidate probe must pass before
the durable selected-release decision allows ordinary systemd startup.

Before that decision, interruption chooses verified rollback. Afterwards,
recovery completes the selected release. Both paths retain failed preparation,
checkpoints, and quarantined roots for inspection. Reboot follows the same
root-owned journal without fetching a release or consulting `main`.

The installed `[release]` table defaults to `followed = "stable"`. An operator
may set `pin = "vX.Y.Z"` in `/etc/rcp/server.toml` to hold an exact promoted release;
removing the pin follows stable again. Prereleases and build tags are refused.
Supervisor self-update is a separate explicit command:

```bash
sudo /usr/local/bin/rcp server supervisor update
sudo -u rcp -H /usr/local/bin/rcp server doctor
```

Doctor reads the public root-owned `status.json` projection, selected receipt,
current pointer, and authenticated process metadata. It does not read the private
journal or require sudo. Missing or invalid projections are reported as unavailable.

## Back up the team server

Configure one absolute backup destination. The daily time and retained archive
count are optional and default to 02:00 server-local time and 30 archives:

```bash
sudo /usr/local/bin/rcp server backup configure \
  --destination /absolute/path/to/backups \
  --schedule 02:00 \
  --retention 30 \
  --confirm
```

On first setup, RCP creates one recovery identity at
`/etc/rcp/backup-recovery.agekey`, keeps it root-owned with mode `0600`, and
stores only its public recipient in the unchanged backup table in
`server.toml`. It also stores that public recipient in the root-owned mode-`0644`
`/etc/rcp/backup-recovery.agekey.pub` sidecar, which lets RCP recognize a missing
server-managed identity without adding a config key. Later configuration reuses
the same identity. If that file is missing, damaged, unsafe, or does not match
the configured recipient, RCP stops and tells you to restore it; it never
silently makes a replacement. That file is also the only key that decrypts the
archives made with it: before the teardown sequence removes `/etc/rcp`, copy it
to protected storage off the host, or accept that the retained archives become
permanently undecryptable.

The wizard verifies the destination, creates and reads back one protected
archive, and only then enables the systemd timer. The destination may be local
or mounted; RCP does not claim that an on-server disk is a disaster-recovery
copy. Check the result at any time with:

```bash
sudo -u rcp -H /usr/local/bin/rcp server backup run
sudo -u rcp -H /usr/local/bin/rcp server doctor
```

This simple default does not survive loss of the whole machine unless the
backup destination and recovery identity are also retained elsewhere. Labs
that need that stronger disaster-recovery setup may generate and protect an
identity elsewhere, then pass only its public `age1...` value through the
advanced `--recipient` option. Never pass private `AGE-SECRET-KEY-...` text to
RCP.

## Restore a protected archive

Restore runs on an installed server, either into a fresh uninitialized data
directory or over an existing initialized team after you confirm the exact data
directory the wizard prints. On the same server, RCP uses its fixed root-only
identity by default:

```bash
sudo /usr/local/bin/rcp server restore /absolute/path/lab.tar.age
```

On a replacement host or when backups use an external recipient, copy the
matching identity into a root-protected file and select it explicitly:

```bash
sudo /usr/local/bin/rcp server restore /absolute/path/lab.tar.age \
  --identity-file /absolute/protected/path/age-identity.txt
```

Run the exact resume commands RCP prints. They bind the configured data
directory, fresh GitHub deploy-key grants, old server-authority disposition, and
the surviving member/permanent-token-id roster. Do not select the destroyed-old
machine disposition unless that machine is permanently gone. Otherwise first
fence its data and revoke every source/project deploy key, server-to-remote SSH
grant, and provider-native login named by the protected restore journal. RCP
does not collect or perform those external revocations.

For a member credential known to have been revoked after the archive was
captured, use the printed `--remove-stale-member <member-id>` command. This is
the ordinary member-removal transaction running offline: another active member
must remain, no project may be orphaned, and the changed roster must be reviewed
again. The root-only final activation runs behind closed admission. Over an
initialized team the supervisor first takes a complete protected backup, closes
new work, stops the service, and retains a rollback checkpoint of the current
data before the archive replaces it. The candidate then starts as a fenced
service-account probe, proves detached work cannot recover, and persists exact
space/commit/project readback. Only after that durable choice does the unit
start, open HTTP, and become enabled. If the root command disappears first, the
fenced process exits cleanly after its bounded timeout. A failure before the
choice restores the previous bytes and launch authority: an initialized server
returns to serving its previous data, and a fresh target stays stopped and
uninitialized. After the choice, recovery keeps the restored data and never
reapplies the old checkpoint. Rerun the same archive-bound command rather than
editing SQLite or systemd.

## Remove a team member

Member removal is a server-console operation because it fences credentials,
project membership, invitations, and live work together. Obtain the durable
member ID from that person's identity record and preview the exact consequences:

```bash
sudo -u rcp -H /usr/local/bin/rcp server member remove <member-id>
```

RCP refuses removal if this is the last enrolled member or if any project would
be left without a member. Add another person to the team and affected projects
through the ordinary invitation flows first. Otherwise review the displayed
memberships, credentials, invitations, and active-work boundary, then run the
exact confirmation command printed by the wizard. Re-enter the same initial
command after an interruption; RCP resumes the durable removal fence instead of
restoring access.

## Maintainer live qualification

One manually dispatched workflow, `supervisor-recovery-live.yml`, qualifies
installation and recovery. It runs only on GitHub-hosted disposable Ubuntu 22.04
and 24.04 x86-64 runners, refuses to start anywhere it cannot prove real guest
virtualization, builds explicitly synthetic local release bundles from the
checkout, and drives them in a disposable VM. It cuts VM power at durable
transaction boundaries, boots the same disk without release network access,
checks that the boot identity changed, and verifies the exact chosen release and
application records. Process-interruption tests alone do not establish reboot or
power-loss recovery. Never point these fixtures at real lab data or a production
host.

Two operational promises are not covered by that workflow and remain open gates
in the deployment handoff: installing from promoted GitHub release assets rather
than synthetic bundles, and fresh-host restore that reconstructs project
checkouts and deploy keys through GitHub. The fixture restore reuses an existing
local Git checkout and proves neither.

## Current implementation boundary

The independent supervisor, artifact bootstrap, guarded source adoption,
application maintenance protocol, protected recovery, operator delegation, and
reboot qualification harness are implemented together. Local tests establish
bounded parsing and transaction behavior. Acceptance records and the deployment
handoff distinguish completed live drives from checks still requiring a promoted
release or a disposable host. Production adoption and recovery must be driven
against the merged promoted release, with any resulting defects fixed in separate
reviewed changes.
