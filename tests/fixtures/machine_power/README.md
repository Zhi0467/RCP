# Machine power captures

Captured on Apple Silicon macOS on 2026-10-03 with read-only commands:

- `/usr/bin/pmset -g` → `pmset-g.txt`
- `/usr/sbin/ioreg -r -k AppleClamshellState` → `ioreg-clamshell.txt`

The pmset sleep-preventing process names and ioreg boot/sleep UUIDs are
redacted. Output structure, power flags, and lid properties are preserved.
Tests substitute the flag and lid values to exercise release policy.
