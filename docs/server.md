# Team server operator guide

This guide provides commands and UI steps for an Ubuntu 22.04 or 24.04 LTS
x86-64 systemd host.
`rcp` and `/home/rcp` are the installer-owned service account and its standard
home, not a personal account or private deployment path.
[Server operations](specs/server-and-machine-operations.md) owns trust,
permissions and recovery rules. At each wizard stop, review before pressing
Enter. After an interruption, use its exact continue command. For noninteractive
inspection, append `--machine-readable` (exit 3 requests an operator action).

## 1. Connect as the ordinary server operator

Use an existing SSH account with sudo. Let the installer create `rcp`:

```bash
ssh operator@server.example
sudo -v
```

## 2. Confirm the host

Expect `x86_64`, Ubuntu 22.04 or 24.04, and a running systemd manager:

```bash
uname -m
. /etc/os-release
printf '%s %s\n' "$ID" "$VERSION_ID"
systemctl show --property=Version --value
```

## 3. Install Ubuntu prerequisites

```bash
sudo apt-get update
sudo apt-get install --yes age bubblewrap ca-certificates curl git iproute2 libc-bin openssh-client openssh-server passwd rsync sudo util-linux xz-utils
```

## 4. Install system-wide uv

Install the pinned upstream binary without changing shell profiles:

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

Check that each prerequisite is found. Let installation create the service
account's Python; do not create `/home/rcp` yourself.

```bash
git --version
ssh -V
age --version
command -v age age-keygen curl git loginctl runuser ssh ssh-keygen sudo systemctl useradd uv
```

## 5. Select the paired promoted release wheels

Open the [latest promoted release](https://github.com/Zhi0467/RCP/releases/latest).
Check it includes both wheels, both dependency lock exports and `manifest.sha256`.
Copy both wheel download links from that same release.

## 6. Run the paired-wheel bootstrap

Replace both URLs and the team name:

```bash
RCP_WHEEL_URL='paste the RCP wheel download link'
RCP_SUPERVISOR_WHEEL_URL='paste the supervisor wheel download link from the same release'
sudo /usr/local/bin/uv tool run --from "$RCP_WHEEL_URL" --with "$RCP_SUPERVISOR_WHEEL_URL" rcp server install --team-name "My lab"
```

## 7. What the installer sets up

Review the displayed installation plan against the
[ownership and recovery contract](specs/server-and-machine-operations.md#machine-authority-and-the-operator-surface).
Continue in the same wizard.

## 8. Save the code when the running wizard asks

Review and confirm its initialization step, equivalent to:

```bash
sudo -u rcp -H /usr/local/bin/rcp space init --team --name "My lab"
```

Save the displayed bootstrap enrollment code privately, outside logs and
command history. Confirm it is saved when asked. Let the wizard finish activation.

## 9. Finish installation and verify the service

Expect healthy status, team space kind and the intended name. Verify restart:

```bash
curl --fail --silent http://127.0.0.1:8421/api/health
sudo -u rcp -H /usr/local/bin/rcp server doctor
sudo systemctl restart rcp.service
curl --fail --silent http://127.0.0.1:8421/api/health
```

## 10. Configure one operator route

### Preferred: named operator with one narrow command

Replace `operator` with an existing Linux account and create this sudoers file:

```bash
sudo visudo --file=/etc/sudoers.d/rcp-project-provision
```

```text
operator ALL=(rcp) NOPASSWD: /usr/local/bin/rcp server project provision * --machine-readable
```

Validate it and test that the fixed provisioning command runs but `id` refuses:

```bash
sudo visudo --check --file=/etc/sudoers.d/rcp-project-provision
sudo -u operator -H sudo -n -u rcp -H /usr/local/bin/rcp server project provision 00000000-0000-4000-8000-000000000000 --machine-readable
sudo -u operator -H sudo -n -u rcp -H /usr/bin/id
```

### Development alternative: direct key-only rcp SSH

For intentional direct access, install an operator public key. If
`authorized_keys` exists, merge with `sudoedit -u rcp` instead of replacing it:

```bash
sudo install --directory --owner=rcp --group=rcp --mode=0700 /home/rcp/.ssh
sudo install --owner=rcp --group=rcp --mode=0600 /absolute/path/to/operator-key.pub /home/rcp/.ssh/authorized_keys
ssh -o PreferredAuthentications=publickey rcp@server.example /usr/local/bin/rcp server doctor
```

## 11. Provider authentication

Run installation commands from the operator session. Use **Settings → Provider
logins** for sign-in, **Verify sign-in**, renewal and sign-out; see
[provider login rules](specs/providers-and-containment.md#login-state-and-failure).

### Codex

```bash
sudo -u rcp -H /bin/bash -lc \
  'curl -fsSL https://chatgpt.com/codex/install.sh | sh'
```

Choose **Sign in with device code**, open the displayed verification link and
enter the code. Wait for RCP's real-request verification.

### Claude Code

```bash
sudo -u rcp -H /bin/bash -lc \
  'curl -fsSL https://claude.ai/install.sh | bash'
```

On a browser-equipped machine with the intended Claude subscription, run
`claude setup-token`; paste its token into **Provider logins → Claude → Sign in**.
Never put the token in command arguments, logs, issues or project files.

### OpenCode

```bash
sudo -u rcp -H /bin/bash -lc \
  'curl -fsSL https://opencode.ai/install | bash -s -- --no-modify-path'
```

For a model backend needing credentials:

```bash
sudo -u rcp -H /bin/bash -lc '~/.opencode/bin/opencode providers login'
```

### Update provider CLIs

```bash
sudo /usr/local/bin/rcp server provider update codex
sudo /usr/local/bin/rcp server provider update claude
sudo /usr/local/bin/rcp server provider update opencode
sudo -u rcp -H /usr/local/bin/rcp server provider check --project <project-id>
```

For a stale pinned executable, use **Resolve** on its machine card, then repeat
the project check. Repair failed login in **Provider logins**.

## 12. Add the team space in the desktop app

Choose **Add team space**, select SSH, enter the saved route and bootstrap code.

### Move a personal project, including unpublished commits if wanted

In the personal project's Settings choose **Move to team space** and the enrolled
team. Review the repository and machine mapping and provisioning steps. Commit files
you want included and back up uncommitted files, other branches and external data.
For a GitHub source, enable **Include local unpushed commits** only after reviewing
the exact saved commits and their history for secrets and large files.
Follow [transfer rules](specs/server-and-machine-operations.md#personal-to-team-transfer-archive)
for supported clones, server-only sources and resumability. Create a branch before
committing on a transferred detached HEAD. If source HEAD changed, start a new review.

### Invite another person to the team space

1. In the team index, open your identity → **Team invitations → Invite member**.
   Send the displayed secret privately.
2. The recipient selects **Add team space → Bootstrap or invitation code**,
   enters their own SSH route, port `8421`, name and code, then adds the space.
3. For an existing project, open **Settings → Members**, select the person and
   choose **Invite**; they accept its separate card on the project index.

## 13. Create the first shared project

Choose **Create a shared team project**. Enter the project name, repositories
and their central machines, state repository and provider profiles. Enter a
GitHub URL or leave it blank for **Server only**. Run its displayed command:

```bash
sudo -u rcp -H /usr/local/bin/rcp server project provision <request-id>
```

At each GitHub stop, add the displayed repository deploy key with **Allow write
access**, then press Enter. At **ready for review**, check the checkout, Git,
machine and provider answers in the wizard and choose **Create project**.
Use the same flow for **Add repository** or **Connect to GitHub**. Merge divergent
GitHub history yourself before retrying.

## Add another machine, such as a cloud VM

Follow [Local and SSH execution](specs/providers-and-containment.md#local-and-ssh-execution)
for base-machine ownership. Configure an SSH route for the service account first.
For Google Compute Engine, run in an RCP terminal:

```bash
gcloud auth login
gcloud compute config-ssh --project <gcp-project>
```

In **Space Settings → Machines → Add machine**, enter the resulting SSH Host
name, review its host-key fingerprint and choose **Trust**. Add the machine to
the project and enter its access hint under **Compute**. For cloud CLI writes,
add its account configuration directory to **Writable paths** on the base card.
Rerun `gcloud compute config-ssh` after a VM address changes.

## Slurm for long-running work

Toggle **Slurm** in the machine's **Compute** fold, then check the route:

```bash
sudo -u rcp -H /usr/local/bin/rcp server compute probe --project <project-id> <machine-alias> --route scheduler
```

Follow any administrator guidance and choose **Check again** on Runs. For the
Linux launch helper, use **Allow background processes** on the machine card.
If doctor reports missing service-account linger, rerun installation.
See [machines and compute](specs/machines-and-compute.md) for route rules.

## Reach the team space from a phone

### Server setup

With sudo, install Tailscale (Ubuntu 22.04 example):

```bash
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
sudo apt-get update && sudo apt-get install -y tailscale
sudo tailscale up
sudo tailscale serve --bg 8421
tailscale serve status
```

Follow the login and Serve prompts. Enable MagicDNS and HTTPS certificates. Substitute
the printed HTTPS origin below, without a path, query or credentials:

```bash
printf 'access_url = "https://<host>.<tailnet>.ts.net"\n' | sudo tee /etc/rcp/team.toml >/dev/null
sudo chown root:rcp /etc/rcp/team.toml
sudo chmod 0640 /etc/rcp/team.toml
sudo -u rcp -H /usr/local/bin/rcp server doctor
```

Confirm doctor reports `team_access_url`. Share the server machine with each
member in the Tailscale admin console. For another HTTPS proxy, preserve Host
and set `X-Forwarded-Proto: https`. Test pairing to verify writes as well as reads.

### Member pairing

1. On a signed-in device, open profile → **Devices → Connect a device**.
2. On the phone, install Tailscale, sign in with your own account and accept the
   operator's share. Scan the QR, name the device and choose **Connect this device**.
   Without a QR code, open the server address and enter the code and name manually.
3. To revoke access, revoke the phone's session under **Devices** elsewhere.
   A second computer's browser uses this flow. Its native app uses **Add team space**.

| Symptom | Action |
| --- | --- |
| No QR code appears | Check `team.toml` and doctor. |
| The address is unreachable | Check the member's Tailscale share and `tailscale serve status`. |
| Certificate warning | Enable MagicDNS and HTTPS certificates. |
| TLS internal error | Run `sudo tailscale cert <host>.<tailnet>.ts.net`; inspect `sudo journalctl -u tailscaled`, respecting retry limits. |
| Reads work, but actions return 403 | Preserve Host and the forwarded HTTPS scheme. |
| The code is refused | Issue a fresh code. |

[Device and session rules](specs/api-web-and-desktop-projections.md#project-index-and-identity)
own expiry, revocation and membership authority.

## Inspect and stop the service

```bash
sudo systemctl status --no-pager rcp.service
sudo journalctl --unit=rcp.service --no-pager
curl --fail --silent http://127.0.0.1:8421/api/health
sudo systemctl stop rcp.service
sudo systemctl start rcp.service
sudo journalctl --unit=rcp.service --no-pager | grep 'operation=<operation-id>'
```

## Update the server release

Review and confirm the exact release identity displayed by:

```bash
sudo /usr/local/bin/rcp server update
```

To pin a promoted release, set `[release]` `pin = "vX.Y.Z"` in
`/etc/rcp/server.toml`. Remove it to follow stable. For an unsupported source,
follow the reported upgrade floor, owned by `UPDATE_SOURCE_FLOOR` in
[`deployment.py`](../src/rcp/server_ops/deployment.py), then retry.
Inspect `releases/<build>/install.log` after preparation failure.
Use [the recovery contract](specs/server-and-machine-operations.md#release-selection-deployment-and-automatic-recovery)
for interrupted updates. To prune completed work and update the supervisor:

```bash
sudo /usr/local/bin/rcp server prune
sudo /usr/local/bin/rcp server supervisor update
sudo -u rcp -H /usr/local/bin/rcp server doctor
```

If an older wrapper lacks prune, use `/usr/local/bin/rcp-supervisor server prune`
until installation refreshes it.

## Back up the team server

Choose a protected absolute destination and configure the schedule and retention:

```bash
sudo /usr/local/bin/rcp server backup configure \
  --destination /absolute/path/to/backups --schedule 02:00 --retention 30 --confirm
sudo -u rcp -H /usr/local/bin/rcp server backup run
sudo -u rcp -H /usr/local/bin/rcp server doctor
```

Copy `/etc/rcp/backup-recovery.agekey` to protected off-host storage. Retain
archives off-host too if machine loss must be survivable. For an externally
protected identity, pass only its public `age1...` recipient via `--recipient`.
Review omissions, especially server-only code, under
[backup and restore](specs/server-and-machine-operations.md#backup-and-restore).

## Restore a protected archive

On an installed server, run one of:

```bash
sudo /usr/local/bin/rcp server restore /absolute/path/archive.tar.age
sudo /usr/local/bin/rcp server restore /absolute/path/archive.tar.age \
  --identity-file /absolute/protected/path/age-identity.txt
```

Review the exact target data directory and use the printed resume commands.
Grant fresh deploy keys and review the surviving roster of members and token ids. Select
**old machine destroyed** only if it is permanently gone. Otherwise, fence its
data and revoke the deploy keys, SSH grants and provider logins the journal names.
Use the printed `--remove-stale-member <member-id>` step for credentials revoked
since capture. Reenter the same archive-bound command after interruption. Do not
edit SQLite or systemd to bypass activation.

## Remove a team member

Preview removal with the durable member id, then review and run the printed
confirmation command. Resolve last-member or orphaned-project refusals first:

```bash
sudo -u rcp -H /usr/local/bin/rcp server member remove <member-id>
```

After interruption, reenter the same initial command.

## Remove the installation

Before deleting anything, protect the recovery identity off-host and confirm
that team data, checkouts and keys can be deleted. Revoke GitHub grants separately.
Skip backup units that were never configured:

```bash
sudo systemctl disable --now rcp.service rcp-backup.timer
sudo rm -f /etc/systemd/system/rcp.service \
  /etc/systemd/system/rcp-backup.service /etc/systemd/system/rcp-backup.timer
sudo systemctl daemon-reload
sudo rm -rf /etc/rcp /usr/local/bin/rcp /usr/local/bin/rcp-supervisor
sudo userdel -r rcp
```
