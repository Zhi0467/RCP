# Team server operator guide

This guide is the terminal workflow for one source-built RCP team server. It is
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
[source updates](#update-the-source-built-server),
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

Success is an exit status of zero. Then continue with the shared Node.js and
`uv` commands below.

## 4. Install Node.js 24 and system-wide uv

The supported server contract is any system-wide Node.js `24.x`. RCP does not
use an operator's NVM/asdf installation because production builds run as the
separate `rcp` account with a clean system PATH. Node 18 is also too old for the
current Vite dependency, which requires Node 20.19+ or 22.12+.

If `/usr/local/bin/node --version` already reports `v24.x`, keep that installation
and skip the Node download. Otherwise install the exact patch qualified by the
two-Ubuntu live matrix from its checksummed upstream archive. The subshell keeps
the operator's working directory unchanged for the later bootstrap clone:

```bash
(
  RCP_NODE_VERSION="v24.20.0"
  RCP_NODE_ARCHIVE="node-${RCP_NODE_VERSION}-linux-x64.tar.xz"
  RCP_NODE_DOWNLOAD_DIR="$(mktemp -d)"
  cd "$RCP_NODE_DOWNLOAD_DIR"
  curl --fail --show-error --location --remote-name "https://nodejs.org/dist/${RCP_NODE_VERSION}/${RCP_NODE_ARCHIVE}"
  curl --fail --show-error --location --remote-name "https://nodejs.org/dist/${RCP_NODE_VERSION}/SHASUMS256.txt"
  grep " ${RCP_NODE_ARCHIVE}$" SHASUMS256.txt | sha256sum --check --strict
  sudo tar --extract --xz --file "$RCP_NODE_ARCHIVE" --directory /usr/local --strip-components=1 --no-same-owner
)
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
hash -r
node --version
npm --version
```

The PATH change applies only to this SSH session. It prevents a personal NVM
version from leaking into the bootstrap build without changing the operator's
shell profile. Success is Node.js major `24` and a nonempty npm version; the
documented archive reports `v24.20.0`.

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
command -v age curl getent git node npm runuser ssh ssh-keygen sudo systemctl useradd uv
```

Success is a path for every command, Node.js major 24, and age major 1.

## 5. Build the disposable bootstrap checkout

Clone through the operator's ordinary GitHub access. This credential is used
only for the disposable bootstrap checkout; the production checkout will not
inherit it.

```bash
git clone git@github.com:Zhi0467/RCP.git rcp-bootstrap
cd rcp-bootstrap
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
hash -r
node --version
npm --prefix web ci
npm --prefix web run build
UV_MANAGED_PYTHON=1 UV_PYTHON=3.12 uv sync --frozen
```

Repeat the PATH assignment here even if Step 4 already set it. This makes the
bootstrap safe after opening a new shell or tmux session whose NVM/asdf setup
would otherwise put an older personal Node.js ahead of the qualified system
Node.js. Continue only when `node --version` reports `v24.x`.

If this operator uses GitHub HTTPS instead, use the credential-free repository
URL and the operator's normal Git credential mechanism. Never put a token in
the URL. Success is a built `web/dist` and an executable `.venv/bin/rcp`.

## 6. Run the installer

Still in the bootstrap checkout, enter the first RCP command as root. Replace
the team name, but keep the executable path absolute:

```bash
sudo /usr/bin/env PYTHONDONTWRITEBYTECODE=1 "$PWD/.venv/bin/rcp" server install --team-name "My lab"
```

The fixed environment flag prevents a root invocation from leaving root-owned
Python cache files in the operator-owned disposable checkout.

Leave this command running. RCP shows one current step while it validates the
host, creates or checks the dedicated unprivileged `rcp` account, installs its
managed Python 3.12, and prepares isolated source access.

For a public source repository, RCP continues without a GitHub credential and
does not stop for a source deploy key.

## 7. Private source only: grant read access in the running wizard

The RCP repository has been public since 2026-09-02, so new installations never
see this step. An installation that still records a deploy-key source converges
to the public HTTPS origin on its next `rcp server update`, or if
`rcp server install` is rerun; the wizard then identifies the retired GitHub
deploy key for the operator to revoke after the command completes and
`server doctor` shows the public origin.

If the transition fires unexpectedly, its probe ran with credential helpers,
askpass, and global Git configuration disabled, so `ready` means the repository
was readable anonymously. The next install or update finishes an interrupted
transition by removing leftover source-key files, rewriting a matching SSH
checkout, and repeating the deploy-key revocation instruction. If the repository
is later made private again, follow the teardown and reinstall procedure and run
`sudo rcp server install ...` to create a fresh deploy-key identity; the new
`installation_id` will differ.

While the RCP source repository is private, the running wizard pauses and shows:

- the exact GitHub deploy-key settings page;
- the title `rcp-source:<installation-id>`;
- the generated public key and fingerprint;
- an SSH host-trust command to run as `rcp`;
- the requirement to leave **Allow write access** unchecked; and
- the exact command that can continue later if this terminal is closed.

Add only that public key as a read-only deploy key. Before accepting the SSH
host key, compare the displayed Ed25519 fingerprint with [GitHub's published
fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).
After adding the key in GitHub, return to the still-running wizard and press
Enter. RCP runs the displayed SSH trust command as `rcp`. When SSH asks `Are you
sure you want to continue connecting?`, compare the fingerprint first, then type
`yes` in that same terminal and press Enter. GitHub's successful authentication
message still has SSH exit status 1 because GitHub does not provide shell
access; the wizard understands that result and continues by rechecking source
access. Do not copy or reconstruct another installer command.

If you intentionally type `q` or close the terminal, no later installation step
runs. Return later with the exact Continue command the wizard printed. Success
after the wizard continues is a separate clean checkout under
`/home/rcp/rcp-server/source`, an immutable built release, a stable
`/usr/local/bin/rcp`, and a fresh service that is still stopped and disabled.

This read-only source key is unrelated to the write-enabled deploy key each team
project receives. After every existing installation has migrated and its retired
source key has been revoked, a later pull request removes the source-key pause,
key handling, and this entire step together.

For structured output, place `--machine-readable` after `install`:

```bash
sudo /usr/bin/env PYTHONDONTWRITEBYTECODE=1 "$PWD/.venv/bin/rcp" server install --team-name "My lab" --machine-readable
```

Every line is one validated JSON event. Exit status 3 means the final event is
an intentional human-action boundary for an external driver; no prompt or
action runs in this mode. Exit status 0 means final service readback succeeded.

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

After that final success, remove the bootstrap checkout. The installed checkout
and release are separate:

```bash
cd ..
rm -rf -- rcp-bootstrap
sudo systemctl restart rcp.service
curl --fail --silent http://127.0.0.1:8421/api/health
```

Only remove the exact disposable directory you just created. Do not use a home
directory, workspace root, variable, or wildcard as the removal target.

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
not log in to a GitHub user or reuse the read-only RCP source key.

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

## Update the source-built server

Run one command and leave it open:

```bash
sudo /usr/local/bin/rcp server update
```

If the installed configuration still names the retired deploy-key SSH source,
this command first converges it to the public HTTPS origin; rerunning
`rcp server install` performs the same transition. The wizard names the retired
GitHub deploy key to revoke after this update and a public-origin `server doctor`
readback.

RCP fetches `origin/main` with the installed source identity, shows the exact
current and target commits, and waits for review. Press Enter to bind that exact
target and continue in the same wizard. RCP then builds a separate release,
rehearses it against copied server state with external effects closed, performs
the guarded switch, and reads back the running commit. A team space that has
been initialized but has no enrolled member yet is valid; rehearsal proves that
unauthenticated project access is still closed instead of rejecting that empty
membership state.

If any step fails, the old release remains serving or is restored before the
wizard reports failure. Read the displayed cause. The wizard prints a complete
`--machine-readable` diagnostic rerun and a normal Continue command. A copied-
state rehearsal failure also prints the exact retained `candidate-result.json`,
a bounded inspection command, and the exact failed rehearsal/capture paths that
may be deleted after the cause is fixed. Never delete a broader checkpoint or
data directory.

## Back up the team server

Create the recovery identity on a separate trusted machine that has `age`; do
not generate or retain the private identity in the RCP server's data directory:

```bash
age-keygen --output rcp-team-backup-key.txt
age-keygen -y rcp-team-backup-key.txt
```

The first command creates the protected `AGE-SECRET-KEY-...` identity. Store
that file in the lab's credential or disaster-recovery system. The second
prints its nonsecret `age1...` recipient. Copy only that public recipient to the
server operator terminal.

Configure one absolute backup destination, public recipient, server-local daily
time, and retained archive count:

```bash
sudo /usr/local/bin/rcp server backup configure \
  --destination /absolute/path/to/backups \
  --recipient <age1-public-recipient> \
  --schedule 02:00 \
  --retention 30 \
  --confirm
```

The wizard verifies the destination and systemd timer. The destination may be a
local or mounted filesystem; RCP does not claim that an on-server disk is a
disaster-recovery copy. Run and verify an immediate archive after configuration:

```bash
sudo -u rcp -H /usr/local/bin/rcp server backup run
sudo -u rcp -H /usr/local/bin/rcp server doctor
```

Keep the newest verified archive and the private recovery identity in locations
that survive loss of the server. Never pass the private identity to `backup
configure`; encryption needs only the public recipient.

## Restore a protected archive

Restore requires a fresh installed server whose configured data directory is
empty. Keep the native `age` recovery identity off-server until the restore and
copy it only into a root-protected file for this run:

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
again. The root-only final activation starts the still-disabled systemd unit
behind closed admission, proves detached work cannot recover, persists exact
space/commit/project readback, and only then opens HTTP and enables the unit. If
the root command disappears first, the fenced process exits cleanly after its
bounded timeout and stays stopped. Any other failure stops and disables the
service; rerun the same archive-bound operation rather than editing SQLite or
systemd.

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

The guarded **Team server install qualification** GitHub Actions workflow drives
this install on separate `ubuntu-22.04` and `ubuntu-24.04` x86-64 hosts. It runs
only by manual dispatch from `main`, because the production installer itself is
fixed to GitHub `main`.

The private RCP source repository requires one repository Actions secret named
`RCP_LIVE_GITHUB_ADMIN_TOKEN`. Use a fine-grained token scoped only to
`Zhi0467/RCP` with repository **Administration: write**, which is the GitHub
permission required to create and remove deploy keys. The workflow writes it to
a mode-0600 temporary file. The tests create temporary read-only source keys and
write-enabled project keys where each workflow requires them, and unconditional
cleanup removes those keys and protected files. A protected receipt records each
generated key's nonsecret label before the API call, so cleanup can revoke it
even if pytest is interrupted. The token never enters RCP CLI argv, event
output, or the installed service environment.

The live test refuses a reused host, requires an explicit destructive-test
confirmation, removes its disposable bootstrap checkout, and checks the
installed account, files, process, loopback listener, HTTP health, journal,
password refusal, direct public-key route, and narrow named-operator sudo route.
It bounds every subprocess stream and removes the temporary direct-login key,
sudoers rule, and named test operator after checking them. Do not point it at a
real lab server.

Exact-head run
[33456906376](https://github.com/Zhi0467/RCP/actions/runs/33456906376)
passed on Ubuntu 22.04 and 24.04. Its install jobs exercised source installation,
service/SSH/doctor readback, forced update rollback, protected backup, and
member removal. Separate fresh hosts then installed from source, reconstructed
the captured repository using fresh write deploy keys, resumed the protected
restore through old-authority and member-roster review, activated the service,
and completed health/project/member and cleanup readback. This is automated
disposable-host evidence, not the complete desktop/provider/collaboration lab
drill.

## Current implementation boundary

The terminal owners for install, doctor, provider readiness, project
provisioning, backup, restore, update, and member removal are concrete. Their
live qualification status is tracked in the active team-server handoff and
acceptance scenarios. The unified desktop wizard, fixed operator bridge,
personal-to-team transfer import, native archive relay, and crash-recovery
coordinator are implemented and hermetically verified. The disposable two-
release server lifecycle and fresh-host restore drive pass. The remaining work
is the source-built desktop/SSH drive against real team spaces and the genuine
one-lab collaboration, provider execution, concurrent/partial backup, and
transfer qualification. Do not substitute manual Git pulls, service-file edits,
or direct database access for any owner.
