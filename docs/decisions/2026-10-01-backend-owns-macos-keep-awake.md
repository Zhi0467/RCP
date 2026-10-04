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
  launch owners. Over-holding in lid mode costs battery down to the floor.
  Under-holding breaks the overnight run.

## What this gives up

- **A closed-lid release runs `pmset sleepnow` even with an external display attached.**

- **Any process in the enrolled account can toggle the flag**, agent shells
  included. The sudoers rule names a user, not an app.
- **A removed sudoers rule can strand the flag.** If something deletes it while
  the flag is set, RCP cannot clear it. The home page shows the exact command.
- **A reboot, install, or uninstall clears a flag the user set themselves.**
- **Uninstall holds no lock of its own.** If the backend dies just after the
  admin prompt, a second RCP on another data directory could start a `sudo
  pmset` before the rule is removed and finish after the final clear. Its watchdog then fails to clear, and the
  home page shows the command.
- **Lid mode may keep the Mac awake longer than needed** when demand
  over-counts, until the battery floor.
- **No guarantee beyond orchestration.** Wi-Fi, provider sign-in, SSH, and
  remote hosts can still stop an overnight run.

## Rebuild on the idle hold (2026-10-03)

Confirmed by the human 2026-10-03, after idle hold shipped alone in #237.

- **Lid-closed mode extends the shipped idle-hold controller.** It does not
  replace it. Idle hold keeps its settings row, toggle, and demand check.
- **The backend reads power state from macOS frameworks, not command text.**
  It calls IOKit through `ctypes` for four values: battery (power-source
  state and charge), thermal state, lid (`AppleClamshellState` on the power
  root domain), and `SleepDisabled` (system power settings). A missing key
  or a failed call is a read failure, and a read failure releases.
- **The watchdog stays a shell script.** It must outlive a dead or stopped
  backend, so it cannot depend on the backend's Python. It reads only the
  flag (`pmset -g`) and the lid (`ioreg`). Any output it does not recognize
  releases. Tests pin its parsing to captured real output.
- **The backend still never writes the flag.** The watchdog remains the only
  process that runs `pmset -a disablesleep`.
- **One profile per platform.** `machine_power.py` holds the portable parts:
  demand, the controller, and the lid-mode policy (generations, heartbeat,
  battery floor, thermal release). `machine_power_macos.py` holds the macOS
  profile: the idle-hold command, the IOKit readers, the `pmset`/`sudo`/watchdog
  commands, and the installer. A platform registry replaces
  `IDLE_HOLD_COMMANDS`. A profile without lid mode declares it absent. Only
  macOS is registered.

### Why

- Three earlier review rounds fixed text parsing: `ioreg` tree glyphs, a flag
  line that `pmset` omits until first set, and a thermal report with history
  but no all-clear. Framework calls return typed values and remove that class
  of bug from the backend.
- Policy and platform calls were mixed in one 800-line module, which made the
  idle-hold carve-out hard to merge back.

### Release gate

Four checks on a Finder-launched packaged candidate replace the earlier list:

1. Install, then uninstall.
2. With the flag set and the lid closed, SIGKILL the backend. The flag clears
   and the Mac sleeps within the watchdog's stale window.
3. Reboot with the flag set. The flag is clear before login.
4. One overnight lid-closed Auto-research run advances.

Battery floor, thermal release, SIGSTOP, cancel, and repaired installs are
covered by tests with simulated readers, not by hand.
