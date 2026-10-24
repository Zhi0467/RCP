# Team server operator guide

Terminal procedures for an artifact-installed team server on Ubuntu 22.04/24.04
LTS x86-64 with systemd, run by a machine operator with sudo.

The CLI shows one current step. At human stops, Enter runs the displayed actions;
quitting leaves an exact continue command. Failures give bounded causes and
recovery commands. `--machine-readable` emits noninteractive JSON without running
human actions.

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
sudo apt-get install --yes age bubblewrap ca-certificates curl git iproute2 libc-bin openssh-client openssh-server passwd rsync sudo util-linux xz-utils
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
  trap 'rm -rf "$RCP_UV_DOWNLOAD_DIR"' EXIT
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
command -v age age-keygen curl git loginctl runuser ssh ssh-keygen sudo systemctl useradd uv
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

Bootstrap verifies both wheels against promoted stable and the complete five-asset
bundle, then creates `rcp`. It installs no source checkout or operator credential.

## 7. What the installer sets up

- The supervisor, in `/etc/rcp/supervisor` with its own root-owned Python.
  `/usr/local/bin/rcp-supervisor` is its entry point.
- Application releases, at `/home/rcp/rcp-server/releases/<build>/.venv`,
  installed as `rcp`.
- `rcp.service`, which runs as `rcp`. The `rcp` account gets no general sudo.
  Before each start, a root recovery step finishes any interrupted update,
  with no network access needed.
- On a server that ran from a source checkout, a one-time adoption. It backs up
  the old data first, and restores it if adoption fails.

`--machine-readable` gives a noninteractive JSON event stream. Exit status 3
means an operator action is needed; that mode never prompts.

The full ownership and recovery contract is in
[server and machine operations](specs/server-and-machine-operations.md).

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

Startup recovery needs no network. To connect phones, follow
[device pairing](device-pairing.md).

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

Any member signs Codex/Claude in, verifies and signs out in **Settings, Provider
logins**; the result and actor are shared. Operators install executables.
Stay in your operator SSH session and prefix commands with `sudo -u rcp -H`
so binaries/settings belong to `/home/rcp`. No direct service-account login is needed.

### Codex

If `codex` is not already installed for the service account, use OpenAI's
standalone installer:

```bash
sudo -u rcp -H /bin/bash -lc \
  'curl -fsSL https://chatgpt.com/codex/install.sh | sh'
```

Then, in **Settings, Provider logins**, choose **Sign in with device code** for
the Codex account. RCP runs `codex login --device-auth` as `rcp`, shows the
verification link and the one-time code, and waits for the browser flow to
finish. Open the link in any browser, sign in to ChatGPT, and enter the code.
When the CLI reports success, RCP runs one minimal real request as `rcp`; only
that answer marks the account signed in, and it resumes the work a dead login
had parked. The code expires after fifteen minutes; start again if it lapses.

### Claude Code

Install Anthropic's recommended native build into `/home/rcp/.local`:

```bash
sudo -u rcp -H /bin/bash -lc \
  'curl -fsSL https://claude.ai/install.sh | bash'
```

On a browser-equipped machine with a Claude subscription, run `claude setup-token`.
Paste its long-lived token into **Settings, Provider logins → Claude → Sign in**.
RCP saves it in app data at `providers/claude/<account>/setup-token` (0600), also
on the SSH account when applicable, and injects `CLAUDE_CODE_OAUTH_TOKEN` into
its Claude processes without API-key billing variables. A real request verifies
sign-in. This avoids competing single-use browser refresh tokens.

The token lasts about a year; RCP shows paste time and estimated expiry, but real
login failure determines validity. Paste a replacement to renew. Never put the
token in argv, logs, issues, chat or project files.

### OpenCode

Install OpenCode's own build under the `rcp` account. Its installer always
writes `~/.opencode/bin`, which RCP discovers automatically without shell
startup changes or symlinks:

```bash
sudo -u rcp -H /bin/bash -lc \
  'curl -fsSL https://opencode.ai/install | bash -s -- --no-modify-path'
```

OpenCode's free models need no sign-in, so it is ready once installed. RCP does
not manage an OpenCode login: **Settings, Provider logins** lists OpenCode with
no sign-in state, only the command to run. For a model backend that needs a
key, run it as `rcp`:

```bash
sudo -u rcp -H /bin/bash -lc '~/.opencode/bin/opencode providers login'
```

### Verify and sign out

Native status commands prove only stored credentials, not a working login.
A classified failure fences new work and shows a notice on Runs and the space
home. **Verify sign-in** sends one real request and resumes parked work on success.
**Sign out** runs Codex logout or deletes Claude's local/remote token, then fences
new work until sign-in.

Backups exclude provider credentials. Restore marks accounts signed out and drops
readiness probes: sign in or verify again. Startup also resets a Claude account
whose data arrived without its token.

### Update provider CLIs

Run updates from the operator session. RCP invokes the native updater as `rcp`,
bounds output and verifies the executable/version without reading credentials:

```bash
sudo /usr/local/bin/rcp server provider update codex
sudo /usr/local/bin/rcp server provider update claude
sudo /usr/local/bin/rcp server provider update opencode
```

Codex reruns its noninteractive installer; Claude runs `claude update`; OpenCode
runs `opencode upgrade`. Account-local Codex wins over an older system copy, which
is left for separate cleanup. An update is not a sign-in: repair dead login in
**Settings, Provider logins**. For a project pinned to an old versioned executable,
use **Resolve** in Settings, then rerun `server provider check --project <project-id>`.
Stable provider paths survive subsequent native updates.

After the project wizard names its project id, run the exact readiness command
it prints:

```bash
sudo -u rcp -H /usr/local/bin/rcp server provider check --project <project-id>
```

Installing or authenticating a provider does not silently add it to an existing
project. Select that provider for an agent profile in project setup, then run the
printed check. RCP does not copy a member's personal provider directory.

## 12. Add the team space in the desktop app

In the desktop app, choose **Add team space**, select SSH, enter the
saved server route, and enroll with the one-time bootstrap code from Step 8. The
unified project wizard can then create a GitHub or server-only team project, or move an
existing personal RCP project into the team space.

### Move a personal project, including unpublished commits if wanted

From the personal project's Settings, choose **Move to team space**, select
the enrolled team, and review its repository/machine mapping. The same setup
wizard handles the server checkout and repository-scoped GitHub grants.

**Include local unpushed commits** starts unchecked for GitHub sources. A source
without an origin transfers as server-only and always includes its reviewed commit
and Git bundle; its target attaches that commit to `main` with no remote. Leave it off to use the
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

As the person joining, from their own RCP desktop app:

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
**Create a shared team project**. Enter the project name, execution machine and provider profiles. Enter a GitHub
URL or leave it blank for **Server only** (its code is not backed up). RCP creates one durable request and
shows its exact request id and operator command. Run that command as `rcp`, or
use the configured desktop operator route:

```bash
sudo -u rcp -H /usr/local/bin/rcp server project provision <request-id>
```

Leave the wizard running. For each repository, GitHub setup pauses for a
repository-scoped public deploy key. Add it to that repository with **Allow
write access** enabled, return to the terminal, and press Enter. The server does
not log in to a GitHub user. Each project keeps its own write-enabled key.

For an empty GitHub repository, setup announces and creates the empty
`Start RCP project` commit as `RCP <rcp@rcp.invalid>`, then pushes `main` with the
deploy key. Server-only setup makes that commit without a remote or deploy key.
Later **Add repository** and **Connect to GitHub** use this same provisioning and
review flow; divergent GitHub history needs a human merge before retry.

After the CLI reaches **ready for review**, return to the project wizard, review
the exact checkout, Git, machine, and provider answers, then choose **Create
project**. RCP creates canonical `.research/` state only after this final human
action. That state is not silently committed to the repository's human Git
history.

## Add another machine, such as a cloud VM

Agents reach every other machine over the service account's own SSH settings.
RCP collects no keys. Give the service account a route first, then add the
machine.

For a Google Compute Engine VM, open a terminal in RCP and run:

```bash
gcloud auth login
gcloud compute config-ssh --project <gcp-project>
```

`config-ssh` creates the service account's key, uploads it to the project's
VMs, and writes one `Host` entry per VM to `~/.ssh/config`, named
`<vm>.<zone>.<gcp-project>`. The terminal keeps `~/.ssh` and the gcloud sign-in
between sessions. That sign-in is readable by agents running as the same
account. For any other host, add a key and a `Host` entry the same way you would
for any SSH user.

In **Space Settings → Machines**, choose **Add machine** and enter the `Host`
name as the SSH host. RCP fills in the account from the login, and the first
connection shows the machine's host-key fingerprint with **Trust**. Then add the
machine to the project. Its **Compute** block takes an access hint, such as
where code lives or how to run jobs. Every agent in the project is told every
machine's SSH name and hint.

Agents reach the VM with plain `ssh <Host name>`; that needs no gcloud. To let
them run `gcloud` as well, for example to start a stopped spot VM, add
`/home/rcp/.config/gcloud` to **Writable paths** on the card of the machine the
agents run on. gcloud writes its token cache there on every command, so a
read-only folder fails even for a `describe`. Any other cloud CLI works the same
way with its own folder, such as `/home/rcp/.aws`.

A spot or preemptible VM may get a new external IP when it restarts. Rerun
`gcloud compute config-ssh` to rewrite the address, or reserve a static IP so it
never changes. Set the VM's termination action to **Stop**: **Delete** replaces
the VM, along with its host key, at every preemption.

## Opt into Slurm for long-running work

After creating the project, open **Project Settings → Long-running jobs** for
its execution machine, enable **Use Slurm**, and Save. RCP checks the scheduler
and helper routes right after the save, and again at every startup; a route
that is not ready shows a notice on **Runs** with its fix and a **Check again**
control. An operator can run the same check from the server operator session:

```bash
sudo -u rcp -H /usr/local/bin/rcp server compute probe --project <project-id> <machine-alias> --route scheduler
```

The check runs through the actual execution account: `rcp` for server-local
work or the configured account on an SSH execution machine. It verifies the
Slurm commands and queue are reachable. If access is missing, follow its
administrator guidance and check again. RCP does not create scheduler users or
associations, submit a test job, or configure job resources. Slurm validates
submission permission and resources when the agent submits its own job.

That setup is retained across ordinary server updates. Agents choose their
submission command or script and each job's resources, then supply the existing
shell watcher to hand off a long wait. An optional saved cancel command makes
human **Cancel** available in the job row. **Stop watching** only stops
continuation; it leaves the external job alive.

Without Slurm selected, the generic launch helper uses the execution machine's
OS process owner. Linux requires the systemd user manager and linger: without
linger, systemd stops the user manager, and every job it owns, about ten seconds
after the account's last session ends. The helper probe refuses launches on an
account without linger. `rcp server install` enables linger for the service
account, and `rcp server doctor` reports a problem when it is missing, so an
installation from before that rule converges by rerunning install. On any other
machine, **Allow background processes** on the machine card enables it for that
account. Short jobs may still finish inline. See
[compute jobs](specs/compute-jobs.md) for the contract and limits.

## Reach the team space from a phone

Phones and other devices reach the loopback listener through the operator's
tailnet and sign in with a device code, never with a member token. The full
walkthrough for the operator, the desktop app, and each member's phone is
[`device-pairing.md`](device-pairing.md). In short: install Tailscale on the
host, `sudo tailscale up`, `sudo tailscale serve --bg 8421`, add
`access_url = "https://<host>.<tailnet>.ts.net"` to `/etc/rcp/team.toml`
(root:rcp, 0640), and share the machine with each member from the
Tailscale admin console. Doctor reports the address as `team_access_url`.

## Inspect and stop the service

```bash
sudo systemctl status --no-pager rcp.service
sudo journalctl --unit=rcp.service --no-pager
curl --fail --silent http://127.0.0.1:8421/api/health
sudo systemctl stop rcp.service
sudo systemctl start rcp.service
```

The listener is intentionally loopback-only. Team desktops reach it through an
SSH tunnel and phones through the tailnet above; opening port 8421 publicly is
not a supported deployment.

Every provider process leaves two lines in the journal: one when it starts and
one when it ends. Each names the provider, capability, runtime, execution host
(or `local`), resolved executable, child pid, and the operation id that opens
the same turn in **Agent tasks**; the end line adds the exit code, duration,
and, for a non-zero exit, RCP's own reading of it. A failed turn adds one line
with its classification (`transport_lost`, `provider_auth`, or `-`). Nothing
a provider prints reaches the journal: its stderr can carry token-shaped values,
so it stays on the task row. To read one turn:

```bash
sudo journalctl --unit=rcp.service --no-pager | grep 'operation=<operation-id>'
```

## Update the server release

```bash
sudo /usr/local/bin/rcp server update
```

The supervisor resolves promoted stable, shows the exact release tag, build,
full commit, and manifest digest, and asks for confirmation of
`vX.Y.Z:<manifest-sha256>`. It installs a separate verified release as `rcp`,
takes a protected backup (an incomplete one is reported as `backup_warning`, not a
refusal), closes application admission, and verifies
copied state before changing live data. A fenced candidate probe must pass before
the durable selected-release decision allows ordinary systemd startup.

If an update is interrupted before the new release is chosen, it rolls back.
After that point, recovery finishes the chosen release. Reboot recovery needs no
network. After a successful update, RCP removes finished update workspaces and
keeps the two newest releases plus the live one. No research history or agent
scratch is removed. To clean up an existing backlog:

```bash
sudo /usr/local/bin/rcp server prune
```

On a server installed before `server prune` existed, run
`sudo /usr/local/bin/rcp-supervisor server prune` instead, until the next
`server install`. Retention limits are in the
[operations spec](specs/server-and-machine-operations.md).

The installed `[release]` table defaults to `followed = "stable"`. An operator
may set `pin = "vX.Y.Z"` in `/etc/rcp/server.toml` to hold an exact promoted release;
removing the pin follows stable again. Prereleases and build tags are refused.

The supported update source floor is **v0.4.5**. For a server on an older release,
set `pin = "v0.4.5"` in that table and run `sudo /usr/local/bin/rcp server update`,
confirming the displayed release. After v0.4.5 is serving, remove the pin and run
the update command again to follow stable. If asked to update the supervisor,
run `sudo /usr/local/bin/rcp server supervisor update` before retrying.

If preparation of a build fails, its directory `releases/<build>` keeps
`install.log` for inspection until the next update attempt, which removes the
unsealed directory and installs the build again. Before
verifying the application's managed Python, preparation drops group and other
write bits that the service account itself left there, such as bytecode caches
written from an operator shell with a permissive umask; anything owned by
another account still refuses the update.

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

RCP creates/reuses root-only 0600 `/etc/rcp/backup-recovery.agekey` and its public
0644 `.pub` sidecar; config stores only the recipient. Missing, damaged, unsafe or
mismatched identity refuses, never regenerates. It is the only decrypting key:
copy it to protected off-host storage before deleting `/etc/rcp`, or retained
archives become permanently unreadable.

The wizard verifies the destination, creates and reads back one protected
archive, and only then enables the systemd timer. The destination may be local
or mounted; RCP does not claim that an on-server disk is a disaster-recovery
copy. Check the result at any time with:

```bash
sudo -u rcp -H /usr/local/bin/rcp server backup run
sudo -u rcp -H /usr/local/bin/rcp server doctor
```

All captures share retention: the configured count plus the newest complete
archive if otherwise pruned, at most `retention + 1` archive/receipt pairs.
Only the newest failed plaintext capture remains; the next completed capture,
even partial, removes old stages. Retry reconciles interrupted publication before
cleanup. Status/diagnostics stay bounded; research size and agent scratch do not.
Server-only repository code is excluded and named in backup status.

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
again.

Final activation checkpoints an initialized server and verifies restored state
under closed admission. Pre-selection failure restores its old service, or keeps
a fresh target stopped. Post-selection recovery preserves restored data. Reenter
the same archive-bound command after interruption; never edit SQLite/systemd to
bypass it. The [spec](specs/server-and-machine-operations.md#backup-and-restore)
owns the recovery contract.

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
