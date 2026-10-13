# Connect phones and other devices to a team space

Install the team server using [server.md](server.md) first. This guide connects
member browsers without putting a permanent member token on each device.
The [API spec](specs/api-web-and-desktop-projections.md#project-index-and-identity)
owns code, session, and revocation behavior.

## What you get

Devices → Connect a device issues a ten-minute, single-use code and, when the
server address is configured, a QR code. The new device gets an ordinary session,
not the member token. Sessions idle out after fourteen days and can be revoked
from another device. Each member uses their own Tailscale account.

## How a phone reaches the server

RCP listens on loopback port 8421. Desktops use SSH tunnels; phones use Tailscale
Serve, which terminates HTTPS at that port without public internet exposure.
The operator shares only the server machine with each member's own tailnet.
Members accept the share; they do not use the operator's login. Network access
never replaces RCP membership/session authentication.

## Part 1: the server (operator, once)

You need sudo and Tailscale with MagicDNS/HTTPS certificates enabled.

1. Install Tailscale (Ubuntu 22.04 example):

   ```bash
   curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
   curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
   sudo apt-get update && sudo apt-get install -y tailscale
   ```

2. Sign in and enable HTTPS:

   ```bash
   sudo tailscale up
   sudo tailscale serve --bg 8421
   tailscale serve status
   ```

   Follow the login URL. If Serve asks to be enabled, follow its link and rerun.
   Status prints `https://<host>.<tailnet>.ts.net`.

3. Set that address in `/etc/rcp/team.toml` beside `server.toml`, substituting
   the actual origin before running:

   ```bash
   printf 'access_url = "https://<host>.<tailnet>.ts.net"\n' | sudo tee /etc/rcp/team.toml >/dev/null
   sudo chown root:rcp /etc/rcp/team.toml && sudo chmod 0640 /etc/rcp/team.toml
   ```

   Use an HTTPS origin without path, query, or credentials. No restart is needed.

4. Verify doctor reports `team_access_url`:

   ```bash
   sudo -u rcp -H /usr/local/bin/rcp server doctor
   ```

5. In the Tailscale admin console, share the server machine with each member.
   RCP membership still requires [enrollment](server.md).

A different HTTPS proxy must preserve Host and set `X-Forwarded-Proto: https`.
Uvicorn trusts loopback forwarded headers. Missing either produces mutation 403s
while reads work; pairing is the end-to-end proxy check.

## Part 2: the desktop app (member, once per phone)

1. Open your top-right profile → Devices. Current device identifies this session.
2. Choose Connect a device. Keep its code/QR open while the phone connects.
3. The card tracks consumed, expired, or withdrawn codes and refreshes Devices
   after connection. Without a configured address it shows only the code;
   give the phone the address separately.

## Part 3: the phone (member, once per phone)

1. Install Tailscale and sign in with your own account.
2. Accept the operator's machine-share link.
3. Scan the QR, name the phone, and choose Connect this device. Without QR, open
   the server address and enter the code/name manually.

The phone has your session authority. Revoke it from Devices elsewhere; removing
its Tailscale share only cuts network access, not the RCP session record.

## A second computer

Its browser follows Part 3. Its desktop app instead enrolls through Add team space
with a member token; native code pairing is not offered.

## Troubleshooting

| Symptom | Check or action |
| --- | --- |
| No QR | Set `team.toml`; doctor reports absent/invalid/unreadable address. |
| Address unreachable | Check the phone's Tailscale login/share and server `tailscale serve status`. |
| Certificate warning | Enable MagicDNS and HTTPS certificates. |
| TLS internal error | Run `sudo tailscale cert <host>.<tailnet>.ts.net` once and await completion; inspect `sudo journalctl -u tailscaled` for ACME errors. Respect any reported retry limit instead of repeated reloads. |
| Reads work, actions 403 | Preserve Host and forwarded HTTPS scheme. |
| Code refused | Issue another: codes expire, are single use, lock after five wrong attempts, and die with their issuing session. |
