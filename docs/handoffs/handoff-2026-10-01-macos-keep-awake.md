# Keep RCP running with the lid closed on a Mac

Date: 2026-10-01
Status: design settled with the human on 2026-10-01, after two xhigh design
reviews. Implemented on this PR the same day: the backend controller,
watchdog, installer, API, the This Mac card, the home-page warning, and the
current-behavior docs. A served-app check on disposable data showed the card,
the opt-in dialog, and a real read-only status. Remaining: the packaged
checks below, listed step by step in `docs/desktop.md` under "Keep-awake
checks". No admin install has run on real hardware yet.
Issue: #228.

Close this handoff when all of these hold on the packaged candidate, on
disposable data:

- a Mac with the lid closed overnight advances a real Auto-research episode:
  provider turn, compute wait, watcher wake, continuation, and Patch apply;
- killing the backend (SIGKILL and SIGSTOP), killing the watchdog, and
  restarting the backend each leave the flag cleared or correctly owned;
- a reboot with the flag set comes back with the flag cleared before login;
- battery at the floor, and a simulated thermal warning, each release and
  sleep a closed Mac;
- install, cancel, uninstall, and a repaired interrupted install work from a
  Finder-launched app.

## What this does

A laptop-only user runs a personal space. Today lid close sleeps the Mac, and
the one backend process stops. This work keeps the backend running while it
has work to do.

There are two modes:

- **Idle hold.** On by default, with a toggle in Settings. While there is
  demand, the backend holds `caffeinate -i -w <worker pid>`. That stops idle
  sleep with the lid open. It needs no root and ends when the worker dies.
- **Lid-closed mode.** Opt-in. While there is demand, the kernel
  `SleepDisabled` flag is set (`pmset -a disablesleep 1`). That is the only
  way to keep a closed Mac awake with no external display.

The promise is continued orchestration. Provider sign-in and quota, Wi-Fi,
VPN, SSH, and remote hosts stay outside what RCP can guarantee. Display sleep
and screen lock stay on. RCP does not touch Low Power Mode, FileVault, or App
Nap.

Linux gets docs only: logind `HandleLidSwitch=ignore` and `systemd-inhibit`.
The API reports `unsupported` on any platform but macOS.

## Facts this design rests on

Verified on Apple Silicon, macOS 26.5.2, on 2026-10-01:

- `SleepDisabled` survives a reboot.
- `pmset sleepnow` needs no root. pmset prints "Sleeping now..." only when
  `IOPMSleepSystem` succeeds.
- `pmset -g therm` and `pmset -g batt` need no root.

From the XNU source (`iokit/Kernel/IOPMrootDomain.cpp`):

- With the flag set, the kernel refuses its own low-battery and
  thermal-emergency sleep. `privateSleepSystem` fails at
  `checkSystemSleepEnabled()`, which checks `userDisabledAllSleep` first. So
  RCP owns the battery and thermal guardrails.
- Clearing the flag with the lid already shut does not re-check the lid. The
  Mac stays awake until a lid, boot, or power-adapter event. So a release with
  the lid closed must also run `pmset sleepnow`.

## Install and uninstall

One admin prompt, from a Settings action. The backend runs
`osascript -e 'do shell script "…" with administrator privileges'`. It
installs three things:

1. `/etc/sudoers.d/rcp-keep-awake`, root-owned, mode 0440. It grants the
   enrolled user `(root) NOPASSWD` for exactly
   `/usr/bin/pmset -a disablesleep 0` and `/usr/bin/pmset -a disablesleep 1`.
   Absolute paths, fixed argv, no wildcards. It is checked with `visudo -cf`
   before an atomic move into place.
2. `/Library/LaunchDaemons/<id>.plist`, a root `RunAtLoad` job that runs
   `/usr/bin/pmset -a disablesleep 0`. It runs at every boot, before login.
   Loading it also runs it once, so the install loads it and reads the flag
   back as clear before activation is allowed.
3. `/Library/Application Support/RCP/keep-awake/`, root-created and owned by
   the enrolled user. It holds the machine-wide lock, the state files, and the
   watchdog script.

Any existing file at these paths that RCP did not write is refused, never
overwritten. Installing from a second macOS account is refused while one is
enrolled. A half-finished install is detected and repaired by the next
install.

Uninstall is also one admin prompt. It clears the flag, verifies it, then
removes all three.

The opt-in dialog says:

- use it on a desk, not in a bag; the Mac gets warm;
- the battery drains, down to the 20% floor;
- any process in this macOS account, including agent shells, can toggle the
  flag once this is installed;
- a reboot always clears the flag, including one you set yourself.

## Ownership

One Mac has one lid-mode owner. The owner holds an advisory lock at
`/Library/Application Support/RCP/keep-awake/owner.lock`. The lock inode is
never replaced or unlinked. The per-data-dir `rcp.lock` does not cover this,
because two backends on two data dirs can run at once.

If the flag is set and RCP's state does not say RCP set it, RCP refuses to
activate and reports `external`. It never clears or adopts that flag.

## The watchdog is the only process that runs pmset

The backend never runs `sudo pmset` itself. It runs a safety pass every
~10 s on its own thread, not the watcher tick. Each pass writes a heartbeat
file:

- the generation number;
- the worker pid and its process start time;
- desired state: `on`, or `off` with a cause;
- the time of the pass.

The watchdog is a small `/bin/sh` script shipped in the `rcp` package. Before
the first activation it is copied to the machine-wide directory, so it
outlives a frozen backend's extraction directory. The backend spawns it
detached and waits for its acknowledgment before asking for `on`.

The watchdog loop:

- If the heartbeat says `on`, is fresh, and the pid and start time match a live
  process, it makes sure the flag is set.
- Otherwise it releases: `sudo -n /usr/bin/pmset -a disablesleep 0`, reads
  the flag back, and runs `pmset sleepnow` if the lid is closed or the lid
  state cannot be read.
- After a release caused by staleness or a dead or changed backend, it marks
  that generation revoked and exits. A backend that resumes under a revoked
  generation cannot turn the flag back on. It must start a new generation,
  which a new watchdog serves.

The heartbeat goes stale after ~60 s. The backend checks on every pass that
its watchdog is alive. A dead watchdog is itself a release with cause
`watchdog_lost`. Every command the backend or watchdog runs has a timeout.

## Demand

Demand is coarse on purpose. Holding the Mac awake too long costs battery,
down to the floor. Holding too briefly breaks the overnight run.

There is demand when any of these hold:

- a locally owned episode (`project.home_space_id == store.space_id`) has
  health `starting`, `active`, `recovering`, `stopping`, or `wrapping_up`.
  The exception is `wrapping_up` with `blocked_reason == "sign_in"`. Health
  comes from `load_episode_health()`;
- an agent task is `running` or `pausing`, or `queued` and not refused by
  `account_login_refusal()`;
- `BackgroundAgentTasks.runtime_is_idle()` is false;
- a transport retry timer is pending.

These alone are not demand: `needs_action`, `completed`, `stopped`, and
`failed` episodes, and armed watchers.

The 30 s wake gate in `machine_sleep.py` stays as it is.

## Safety and release

Each safety pass reads battery (`pmset -g batt`), thermal (`pmset -g therm`),
lid (`ioreg -r -k AppleClamshellState`), the flag (`pmset -g`), and demand.

It releases lid mode when:

- on battery at 20% or less;
- any thermal warning is recorded;
- a reading fails, times out, or cannot be parsed;
- demand is gone.

A battery or thermal release also drops the idle hold. A failed reading
releases lid mode only: the idle hold follows demand, and a desktop Mac has no
lid to read. A demand-gone release drops both
holds and does not run `sleepnow` with the lid open. Finishing work never
sleeps an open Mac.

The release cause is saved first, best effort. A failed save never blocks the
release. A failed clear and a failed `sleepnow` are recorded as separate
cleanup failures.

## Re-arm

- **Thermal** and **cleanup failure** latch lid mode off. The human re-enables
  it in Settings. The latch survives a backend restart. The space home page
  shows a warning while it is latched.
- **Battery** releases until the Mac is on AC power, then re-arms on its own.
- **A failed clear** also shows the exact command on the home page:
  `sudo pmset -a disablesleep 0`.

If the sudoers rule disappears while the flag is set, nothing in RCP can clear
it. That is accepted. The home-page warning is the remedy.

## Where state lives

Machine-local, outside project manifests and transferable research data:

- preferences (`idle_hold`, `lid_mode`) and latches go in the RCP data dir's
  machine-local state, and survive restarts;
- the activation record (generation, owner, whether RCP set the flag) goes in
  the machine-wide directory.

`src/rcp/core/models.py` and `src/rcp/config.py` do not change.

## API contract

`GET /api/machine-power` returns:

```json
{
  "platform": "macos",
  "supported": true,
  "installed": true,
  "install_problem": null,
  "idle_hold": {"enabled": true, "active": true},
  "lid_mode": {"enabled": true, "active": false},
  "demand": true,
  "demand_reasons": ["episode"],
  "latched": null,
  "last_release": {"cause": "battery_floor", "at": "2026-10-01T03:12:00Z"},
  "cleanup_failure": null,
  "external_owner": false
}
```

- `platform`: `macos`, `linux`, or `other`. `supported` is false unless macOS.
- `install_problem`: null, `not_installed`, `partial`, `other_account`, or
  `foreign_file`.
- `demand_reasons`: any of `episode`, `task`, `runtime`, `retry`.
- `latched`: null, `thermal`, or `cleanup_failure`.
- `last_release.cause`: `demand_gone`, `battery_floor`, `thermal`,
  `reading_failed`, `watchdog_lost`, `heartbeat_stale`, `disabled`, or
  `shutdown`.
- `cleanup_failure`: null, or
  `{"kind": "clear_failed" | "sleep_failed", "command": "sudo pmset -a disablesleep 0"}`.

`PUT /api/machine-power` takes `{"idle_hold": bool}` and/or
`{"lid_mode": bool}`. Enabling `lid_mode` clears a latch. It returns the same
body as `GET`.

`POST /api/machine-power/install` and `POST /api/machine-power/uninstall`
run the admin prompt and return the same body. A cancelled prompt is not an
error: it returns 200 with the unchanged status.

The endpoints exist only in a personal space. A team space returns 404.

## Slices

1. **Backend.** `src/rcp/machine_power.py` (controller, readers, demand,
   state), `src/rcp/machine_power_install.py` (admin install and uninstall),
   the shipped watchdog script under `src/rcp/`, constants in `limits.py`,
   `src/rcp/api/machine_power.py`, and lifespan wiring in `src/rcp/api/app.py`.
   It also lists the script in `packaging/rcp_backend.spec` and
   `packaging/hooks/validate_frozen_resources.py`. Every OS command goes
   through injected runners. The watchdog script is tested by running it
   against fake executables.
2. **UI.** `web/src/types.ts`, `web/src/api.ts`, `SpaceSettings.tsx` (a "This
   Mac" card with both toggles, the opt-in dialog, install and uninstall), and
   the home-page warning in `ProjectLanding.tsx`, plus web tests.
3. **Docs.** Current behavior goes into
   `docs/specs/server-and-machine-operations.md` and the Settings section of
   `docs/specs/api-web-and-desktop-projections.md`. Install, uninstall, the
   Linux how-to, and the candidate checks go into `docs/desktop.md`.

Slices 1 and 2 run in parallel against the API contract above.
