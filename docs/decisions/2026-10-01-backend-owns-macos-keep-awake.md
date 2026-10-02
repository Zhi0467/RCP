# The backend owns macOS keep-awake, through a scoped sudo rule

Confirmed by the human 2026-10-01.

## What was decided

- The backend, not the desktop shell, decides when the Mac stays awake. It
  knows the work, and it outlives the window.
- Idle hold is on by default. Lid-closed mode is opt-in.
- Lid-closed mode sets the kernel `SleepDisabled` flag. Root comes from a
  sudoers rule for exactly two `pmset` commands, plus a boot LaunchDaemon that
  clears the flag. Both are installed with one admin prompt.
- A watchdog process is the only thing that runs `pmset -a disablesleep`.
- RCP owns the battery floor (20%) and the thermal release.
- Demand is a coarse check built on episode health.

## Why

- **Only `SleepDisabled` survives lid close** without an external display.
  Power assertions do not.
- **Sudoers, not a privileged helper.** The app is ad-hoc signed, so
  `SMAppService` is out. An admin prompt each time cannot re-arm unattended.
- **Two executors race.** If the backend and the watchdog both toggled the
  flag, a backend that stalled and then resumed could set it again after the
  watchdog cleared it. One executor with a revocable generation removes that.
- **macOS does not guard battery or heat** while the flag is set. The kernel
  refuses its own low-battery and thermal-emergency sleep then. Clearing the
  flag with the lid shut also does not sleep the Mac, so a closed-lid release
  runs `pmset sleepnow`.
- **The flag survives a reboot**, so a backend crash followed by a reboot
  would leave the Mac never sleeping. The boot LaunchDaemon clears it before
  login. Verified on Apple Silicon, macOS 26.5.2, which also showed that
  `pmset sleepnow` needs no root, so the sudoers rule stays at two commands.
- **Coarse demand.** The precise version needed eligibility checks beside six
  launch owners. Over-holding costs battery down to the floor. Under-holding
  breaks the overnight run.

## What this gives up

- **Any process in the enrolled account can toggle the flag**, agent shells
  included. The sudoers rule names a user, not an app.
- **A removed sudoers rule can strand the flag.** If something deletes it while
  the flag is set, RCP cannot clear it. The home page shows the exact command.
- **A reboot clears a flag the user set themselves.**
- **The Mac may stay awake longer than needed** when demand over-counts, until
  the battery floor.
- **No guarantee beyond orchestration.** Wi-Fi, provider sign-in, SSH, and
  remote hosts can still stop an overnight run.
