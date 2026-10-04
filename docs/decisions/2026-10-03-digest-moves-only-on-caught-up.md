# The digest moves only when you say you caught up

Confirmed by the human 2026-10-03.

## What was decided

- Each member has a server-side marker per project for the "Since you last
  looked" digest.
- The marker moves only when the member presses **Caught up**. It records the
  cursor of the digest on screen.
- A member with no marker starts at the current cursor with an empty digest.

## Why

- **Opening is not reading.** A member who opens a project to check one thing
  has not absorbed the overnight changes. Moving the marker on open or on a
  timer would hide them.
- **The displayed cursor, not "now".** A Caught up that stored the current
  time would swallow work that finished while the card was on screen.
- **Server-side, per member.** Members share a project but not attention, and
  one member uses several devices.
- **Start empty.** A backfill on first use would show a project's whole
  history as new, which teaches the member to ignore the card.

## What this gives up

- A member who never presses Caught up keeps a growing digest. The card
  groups by source, so it stays one screen even when large.
- Coverage is an event sequence in SQLite, not a time. Each source writes
  its event in the same transaction as its change, so nothing finishes
  "before" a Caught up and stays hidden. The cost is one extra insert on each
  of those writes and a projector that follows graph history.
