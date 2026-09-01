# Team server operator guide

This guide is the terminal workflow for one source-built RCP team server. It is
written for the machine operator who has `sudo` on a disposable or dedicated
Ubuntu host. The supported host is Ubuntu 22.04 LTS or Ubuntu 24.04 LTS on
x86-64 with systemd.

The root [README](../README.md#install-a-team-server-from-source) points here;
this document is the single complete server setup and operations procedure.

The `rcp server` CLI is the complete machine workflow. It prints its full plan
before doing work. At a human boundary it names the machine or external service,
prints ordered copyable actions, explains the success signal, and prints the
exact command to resume. `--machine-readable` emits the same plan and actions as
bounded JSON Lines for the desktop wizard; the wizard does not own another
setup procedure.

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
export PATH="/usr/local/bin:/usr/bin:/bin"
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
npm --prefix web ci
npm --prefix web run build
UV_MANAGED_PYTHON=1 UV_PYTHON=3.12 uv sync --frozen
```

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

RCP first prints all nine steps. It then validates the host, creates or checks
the dedicated unprivileged `rcp` account, installs its managed Python 3.12, and
prepares isolated source access.

For a public source repository, RCP continues without a GitHub credential and
Step 7 does not occur.

## 7. Private source only: grant read access and resume

While the RCP source repository is private, the installer stops with exit status
3 and prints:

- the exact GitHub deploy-key settings page;
- the title `rcp-source:<installation-id>`;
- the generated public key and fingerprint;
- an SSH host-trust command to run as `rcp`;
- the requirement to leave **Allow write access** unchecked; and
- the exact `server install` command to resume.

Add only that public key as a read-only deploy key. Before accepting the SSH
host key, compare the displayed Ed25519 fingerprint with [GitHub's published
fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).
At this pause, success is exit status 3 with the exact source deploy-key and
resume instructions; the managed checkout and installed service do not exist
yet. Run the printed actions in order, then run the printed resume command
exactly. Success after that rerun is a separate clean checkout under
`/home/rcp/rcp-server/source`, an immutable built release, a stable
`/usr/local/bin/rcp`, and a fresh service that is still stopped and disabled.

This read-only source key is unrelated to the write-enabled deploy key each team
project receives. When RCP becomes public, the source-key pause, key material,
and this entire step are removed together.

For structured output, place `--machine-readable` after `install`:

```bash
sudo /usr/bin/env PYTHONDONTWRITEBYTECODE=1 "$PWD/.venv/bin/rcp" server install --team-name "My lab" --machine-readable
```

Every line is one validated JSON event. Exit status 3 means the final event is
an intentional human-action boundary, not an installation failure. Exit status
0 means the final service readback succeeded.

## 8. Initialize the team space

On a fresh installation, the CLI prints this command with the chosen name:

```bash
sudo -u rcp -H /usr/local/bin/rcp space init --team --name "My lab"
```

The first command must run in an interactive terminal. It prints one bootstrap
code once. Store that code outside logs and command history; the service never
needs it.

## 9. Finish installation and verify the service

Run the exact installer resume command, equivalent to:

```bash
sudo /usr/local/bin/rcp server install --team-name "My lab"
curl --fail --silent http://127.0.0.1:8421/api/health
sudo -u rcp -H /usr/local/bin/rcp server doctor
```

The final installer rerun performs the system-owned enable/start and HTTP
readback. Health must identify `status` as `ok`, `space_kind` as `team`, and the
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
it. For server-local execution that is `rcp`:

```bash
sudo -u rcp -H /bin/bash
```

Run the provider's own install and login instructions in that shell, exit it,
then use the exact `rcp server provider check ...` command printed by project
setup. RCP checks and uses provider-native state; it does not copy a member's
personal provider directory or store the credential itself.

## 12. Add the team space in the desktop app

In the source-built desktop app, choose **Add team space**, select SSH, enter the
saved server route, and enroll with the one-time bootstrap code from Step 8. The
unified project wizard can then create a team project from GitHub or move an
existing personal RCP project into the team space.

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
