---
id: S130-browse-ssh-repository-folders
status: implemented
tier: hermetic
driver: pytest + browser + fake ssh
covered_by:
  - tests/test_remote_repository_browser.py
  - tests/test_setup.py
  - tests/test_route_inventory.py
  - web/tests/projectSetup.test.mjs
last_checked: >-
  2026-09-02 — focused API and Web tests passed; a disposable RCP server using
  a real fake-ssh subprocess returned an authenticated-home listing and an
  exact-machine authentication repair action without accepting credentials.
---

# Browse a remote repository path without giving RCP credentials

Personal project setup can browse an SSH host from the authenticated remote
user's home while retaining the manual absolute-path field. RCP uses only the
ordinary SSH configuration and credentials already present on the machine where
RCP itself is running.

## Drive

1. Add an SSH repository, enter its host, and choose **Browse SSH…** without
   entering a path.
2. Confirm the first listing is the authenticated remote user's home and shows
   only its immediate child directories. Direct Git repositories and folders
   containing `.research` are labelled.
3. Choose one child and confirm exactly one new backend request lists that
   directory. Choose **Use this folder** and confirm its absolute path fills the
   still-editable manual field.
4. Repeat with a host whose credentials are absent. Confirm the error names the
   exact RCP machine where credentials must be added and offers no password,
   private-key, or credential-path input.
5. Attempt the personal browsing API on a team backend and confirm it is refused
   before SSH is launched.

## Pass condition

Every listing is capped and backend-validated, one directory level is inspected
per request, no recursive scan occurs, manual absolute entry remains available,
and neither the API nor persisted project configuration accepts credential
material. OpenSSH host-key checking is not bypassed.

## Boundary

Browsing does not register, preflight, create, or grant authority over a
project. It is available only in personal existing-checkout setup; team setup
continues to derive server-managed paths from its reviewed provisioning request.
