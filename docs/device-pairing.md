# Connect phones and other devices to a team space

This guide walks an operator and then a member through **device pairing**: how
a phone, a tablet, or a second computer's browser reaches an RCP team server and
signs in without ever holding the member's permanent token. It covers the
server, the desktop app, and what each member does on their own device.

Read [`server.md`](server.md) first; the team server must be installed and
serving on its loopback port before any of this applies.

## What you get

- A member opens their profile in the desktop app, chooses **Devices**, then
  **Connect a device**. RCP issues a ten-minute, single-use code and shows it as
  text and, once the server knows its address, as a QR code.
- The phone scans the QR code (or opens the address and chooses **Connect this
  device**), types a name for itself, and is signed in. It holds an ordinary
  session, never the member token; a lost phone leaks a session that idles out
  in fourteen days and can be revoked from **Devices** on any other device.
- Every member keeps their own Tailscale account and their own device list. The
  only thing that crosses accounts is the server itself.

## How a phone reaches the server

RCP listens on `127.0.0.1:8421` only; credentials never cross plaintext HTTP.
Desktops reach it through an SSH tunnel. Phones reach it through a **tailnet**:
the server joins Tailscale, and `tailscale serve` terminates HTTPS in front of
the loopback port. Nothing opens to the public internet, and member session
authentication still applies on top, so reaching the tailnet is not authority.

A tailnet is a private network that belongs to one Tailscale login. The server
is signed in with the operator's account. Members do **not** sign in with that
account. Instead, the operator **shares the server machine** with each member
from the Tailscale admin console; the member accepts once, and from then on the
server appears on the member's own tailnet as one extra machine. None of the
operator's other devices are visible to the member, and none of the member's
devices are visible to the operator.

## Part 1: the server (operator, once)

You need sudo on the host and a Tailscale account with **MagicDNS** and
**HTTPS certificates** enabled (Tailscale admin console, DNS page).

1. Install Tailscale from its apt repository (Ubuntu 22.04 shown):

   ```bash
   curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
   curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/jammy.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
   sudo apt-get update && sudo apt-get install -y tailscale
   ```

2. Sign the server in and put HTTPS in front of RCP:

   ```bash
   sudo tailscale up
   sudo tailscale serve --bg 8421
   tailscale serve status
   ```

   `tailscale up` prints a login URL; open it in your browser and approve the
   machine. The first `tailscale serve` on a tailnet prints
   `Serve is not enabled on your tailnet` with a link; open it, enable Serve
   (this also turns on HTTPS certificates), and rerun the command.
   `tailscale serve status` then prints the address members will use, of the
   form `https://<host>.<tailnet>.ts.net`.

3. Tell RCP that address. Add a `[team]` table to `/etc/rcp/server.toml`, the
   same operator-owned file that holds the release pin:

   ```toml
   [team]
   access_url = "https://wth-gpu-01.tail1234.ts.net"
   ```

   The value must be an `https://` origin with no path, query, or credentials.
   No restart is needed; RCP reads it when a member opens **Devices**.

4. Confirm with doctor, which reports the address as `team_access_url`:

   ```bash
   sudo -u rcp -H /usr/local/bin/rcp server doctor
   ```

5. Share the machine with each member. In the Tailscale admin console, open the
   server's machine, choose **Share**, and send the link to the member. Sharing
   grants network reachability only; RCP membership still comes from an
   enrollment code, as in [`server.md`](server.md).

The proxy must preserve `Host` and set `X-Forwarded-Proto: https`, because the
team mutation-origin check compares the browser `Origin` against the request's
own scheme and host. `tailscale serve` does both, and uvicorn trusts forwarded
headers from `127.0.0.1`. A front that drops either header answers 403 on every
mutation while reads keep working; pairing a device is the quickest end-to-end
check of any other terminator.

## Part 2: the desktop app (member, once per phone)

1. Open your profile in the top-right corner of the desktop app. Under
   **Devices** you see every device signed in as you, with **Current device**
   marking this one.
2. Choose **Connect a device**. A code such as `ABCD-EFGHJK` appears with its
   expiry. When the operator has set the address, a QR code appears with it.
3. Leave the card open; it watches the code and updates **Devices** the moment
   the phone connects. If the code expires or is withdrawn, the card says so.

Until the operator sets the address, the card shows the code only and says so;
the phone must then be told the address by hand.

## Part 3: the phone (member, once per phone)

1. Install the Tailscale app and sign in with **your own** account.
2. Accept the share link the operator sent. The server now appears in your
   Tailscale machine list.
3. Scan the QR code with the camera. The RCP login screen opens with the code
   filled in; type a name for the phone and choose **Connect this device**.
   Without a QR code, open the address in the browser, choose **Connect this
   device**, and type the code and the name.

The phone now has the same authority as any of your sessions. Revoke it any
time from **Devices** on another device; the operator can also revoke it by
removing the share in Tailscale, which cuts the network path.

## A second computer

A second computer's browser follows Part 3 exactly, over the same shared
machine. The source-built desktop app on a second computer still enrolls with a
member token through **Add team space**; pairing it with a code is not offered
yet.

## Troubleshooting

- **The QR code is missing.** The server has no `[team] access_url`; see Part 1
  step 3. Doctor shows `team_access_url: none`.
- **The phone cannot open the address.** Confirm the phone is signed in to
  Tailscale and has accepted the share; `tailscale status` on the phone lists
  the server. Confirm `tailscale serve status` on the server still shows the
  proxy to port 8421.
- **Certificate warning on the phone.** HTTPS certificates are not enabled for
  the tailnet, or MagicDNS is off; enable both in the admin console.
- **Reads work but every action fails with 403.** The HTTPS front is not
  preserving `Host` or setting `X-Forwarded-Proto: https`.
- **The code is refused.** Codes expire after ten minutes, are single use, lock
  after five wrong attempts, and die with the session that issued them. Issue a
  new one from **Devices**.
