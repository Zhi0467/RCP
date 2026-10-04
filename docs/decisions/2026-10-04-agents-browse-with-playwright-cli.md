# Agents browse with Playwright CLI

Date: 2026-10-04. Status: active. Confirmed by the human on 2026-10-04.
Implementation: [handoff](../handoffs/handoff-2026-10-04-agent-browser.md).

## Decision

An agent uses a headless browser by running Microsoft's `@playwright/cli` in its
own shell. Every CLI feature stays available, including `run-code`. RCP installs
the CLI, starts each chat's browser session outside the provider sandbox, and
keeps the session and its profile between turns. A per-chat Browser toggle, off
by default, is the human's consent.

RCP adds no MCP server to agent launches and offers no UI to add one.

## Options considered

Probe evidence is in the handoff.

- **The providers' own browsers.** Claude's computer use refuses `-p` runs, and
  RCP launches Claude with `--print`. Claude in Chrome needs the member's own
  Chrome and extension. Codex's in-app browser is drawn by the Codex desktop app,
  which RCP replaces when it drives `codex app-server`. All of them assume a human
  at a screen. RCP agents mostly run unattended on servers.
- **The agent writes its own Playwright scripts.** No state survives between
  scripts. Inside Codex's macOS sandbox, Chromium cannot start at all.
- **Playwright MCP as a built-in server.** It worked on all three providers. Its
  25 tool descriptions cost about 5k tokens every turn. Its browser lives only as
  long as the provider process, so open tabs reset every turn. It would also put
  MCP into RCP's launches.
- **Browser verbs on the staged command channel.** RCP would relay and check every
  command. That keeps the write boundary, but it drops `run-code`, adds the
  mailbox poll to every step (0.2 s locally, 2 s over SSH), and runs the browser
  on the RCP host, where a remote GPU host's localhost is out of reach.
- **The CLI with an OS-confined daemon.** Playwright's checks cannot be turned on
  in CLI mode, so confinement would have to come from the OS. Chromium hung under
  a write-only macOS sandbox profile, and Codex's own macOS sandbox blocks it.

## Why

- **The human decides what an agent may run.** Removing `run-code` would make that
  decision for every user. A per-chat toggle leaves it with the person who owns the
  chat.
- **Tokens.** The CLI costs one prompt line. Snapshots go to files, and the agent
  reads what it needs. Published comparisons report about 4x fewer tokens than
  MCP for the same task. We did not reproduce that number.
- **State.** The CLI's daemon keeps the browser alive between commands and turns,
  so tabs, pages, and logins carry over. MCP could keep only cookies.
- **Locality.** The browser runs where the agent runs, so it reaches services the
  agent started on that machine.
- **One fewer channel.** RCP launches stay free of MCP, so there is no MCP
  authentication, secret storage, or containment exception per provider.

## What this gives up

- **The write boundary, for browser actions.** The daemon runs outside the provider
  sandbox. The CLI turns off Playwright's file-path checks, and `run-code` runs
  any Playwright code. A turn with the browser on can write anywhere the RCP
  account can. This is the same class of gap as Claude Work's unbounded shell
  ([decision](2026-09-13-claude-work-runs-without-the-os-sandbox.md)). Graph
  authority is unchanged.
- **Network reach.** The browser reaches any address its host can, including
  other local services. The personal backend stops trusting loopback for this
  reason ([decision](2026-10-04-the-personal-backend-requires-an-owner-session.md)).
- **Cross-chat isolation on a team server.** Every agent runs as the service
  account and can address another chat's session by name.
- **An install on each execution host.** Node.js 18 or newer must exist. Linux
  hosts may need system libraries that only an administrator can install.
- **Services that offer only MCP.** Agents cannot use a SaaS tool that ships only
  an MCP server unless a CLI for it exists.
