# Keep RCP running with the lid closed on a Mac

Date: 2026-10-01
Issue: #228.
Status: implemented on its PR. The backend controller, watchdog, installer,
API, the This Mac card, and the home-page warning are in. Current behavior is
in [the server spec](../specs/server-and-machine-operations.md#keeping-a-mac-awake),
and the tradeoffs are in
[the decision record](../decisions/2026-10-01-backend-owns-macos-keep-awake.md).
A served-app check on disposable data showed the card, the opt-in dialog, and a
real read-only status. No admin install has run on real hardware yet.

## Remaining

Run the packaged-candidate checks in `docs/desktop.md` under "Keep-awake
checks". Close this handoff when all of these hold:

- a Mac with the lid closed overnight advances a real Auto-research episode:
  provider turn, compute wait, watcher wake, continuation, and Patch apply;
- killing the backend (SIGKILL and SIGSTOP), killing the watchdog, and
  restarting the backend each leave the flag cleared or correctly owned;
- a reboot with the flag set comes back with the flag cleared before login;
- battery at the floor, and a simulated thermal warning, each release and
  sleep a closed Mac;
- install, cancel, uninstall, and a repaired interrupted install work from a
  Finder-launched app.

CI runs on Linux, so the tests that drive the real admin script and the real
crashed watchdog run only on a Mac.

## Known gap

Only the backend holds `owner.lock` while the root install or uninstall script
runs. If the backend dies mid-uninstall, another backend could activate before
the script removes the sudoers rule. Closing it means the root script takes the
lock itself.
