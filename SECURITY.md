# Security

RCP launches coding agents with shell access on your own machines and team
servers, so a security defect can matter more than an ordinary bug.

## Reporting a vulnerability

Do not open a public issue. Email <wangzhi0467@gmail.com> with a description
and steps to reproduce. You will get a reply within seven days.

## Scope

Reports about these are especially welcome:

- an agent writing outside its declared write roots or reading another
  project's data;
- a team member acting with authority the space did not grant them, such as
  approving a Proposal or dispatching a merge;
- provider credentials or team secrets reaching logs, artifacts, or another
  member; and
- the device-pairing or team sign-in flow admitting a device it should not.

## Supported versions

Only the latest release on `main` receives fixes.
