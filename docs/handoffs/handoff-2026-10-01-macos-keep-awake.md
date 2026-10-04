# Keep RCP running with the lid closed on a Mac

Date: 2026-10-01, rebuilt 2026-10-03.
Issue: #228.
Status: lid-closed mode is merged onto the idle hold that shipped in #237.
Rebuild steps 1–4 are implemented: framework readers, captured watchdog
fixtures, the macOS profile split, and simulated policy tests with a macOS
reader smoke test. Current behavior is in
[the server spec](../specs/server-and-machine-operations.md#keeping-a-mac-awake),
and the tradeoffs are in
[the decision record](../decisions/2026-10-01-backend-owns-macos-keep-awake.md),
including its "Rebuild on the idle hold" section. No admin install has run on
real hardware yet.

## Rebuild

1. **Framework readers.** Replace the backend's `pmset`/`ioreg` text parsing
   (`parse_battery`, `parse_thermal`, `parse_lid`, `parse_flag`) with `ctypes`
   calls, probed on Apple Silicon macOS 26.5 on 2026-10-03:
   - battery: `IOPSCopyPowerSourcesInfo`, `IOPSCopyPowerSourcesList`,
     `IOPSGetPowerSourceDescription` (`Power Source State`,
     `Current Capacity`, `Max Capacity`);
   - thermal: `NSProcessInfo.processInfo.thermalState` through the Objective-C
     runtime. `IOPMGetThermalWarningLevel` returns `kIOReturnNotFound` on
     Apple Silicon, so it is not used. Serious (2) or critical (3) releases;
   - lid: `AppleClamshellState` on the `IOPMrootDomain` registry entry;
   - flag: `SleepDisabled` in `IOPMCopySystemPowerSettings()`.
   A failed call or missing key raises, and a failed read releases.
2. **Watchdog unchanged in kind.** It stays the shell script and still reads
   only the flag and the lid. Its parsing tests use captured real output.
3. **Profile split.** `machine_power.py` keeps the portable controller,
   demand, and lid-mode policy. `machine_power_macos.py` is the macOS profile:
   the idle-hold command, the readers, the `pmset`/`sudo`/watchdog commands,
   and the installer (moved from `machine_power_install.py`). A platform
   registry replaces `IDLE_HOLD_COMMANDS`; a profile without lid mode declares
   it absent.
4. Tests drive the policy with simulated readers; the reader module gets one
   macOS-only smoke test that calls the real frameworks.

## Remaining

After the rebuild, run the four checks in `docs/desktop.md` under
"Keep-awake checks" on a Finder-launched packaged candidate:

1. install, then uninstall;
2. with the flag set and the lid closed, SIGKILL the backend: the flag
   clears and the Mac sleeps within the watchdog's stale window;
3. reboot with the flag set: the flag is clear before login;
4. one overnight lid-closed Auto-research run advances.

CI runs on Linux, so the tests that drive the real admin script, the real
crashed watchdog, and the framework readers run only on a Mac.
