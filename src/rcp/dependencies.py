"""Every external program RCP starts, and whether a machine must have it.

This list is the one record. `rcp.dependency_check` checks machines against it,
server install and doctor read it, and `tests/test_external_dependencies.py`
holds the source and the server guide's install line to it.

Roles:

- ``local``: the machine running the RCP backend (the Mac app, a source run,
  or a team server), macOS or Linux.
- ``remote``: a Linux machine RCP reaches over SSH to run agents and jobs.
- ``server``: what an installed team server's service account needs beyond
  ``local``.
- ``server_install``: what the root installer needs before that account exists.

Required means an agent run on that machine cannot finish without the program,
so a definite absence refuses the run. Optional means only one feature stops
working; the entry says which, and what the user sees instead. A missing
optional program never refuses a run. Version and feature contracts (git 2.38
worktrees, the rsync transfer contract, Node 20, the hiding probes) stay with
the modules that use them; this list records presence only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Role = Literal["local", "remote", "server", "server_install"]
Platform = Literal["linux", "darwin"]

LINUX: frozenset[Platform] = frozenset({"linux"})
MACOS: frozenset[Platform] = frozenset({"darwin"})
BOTH: frozenset[Platform] = frozenset({"linux", "darwin"})

# Linux distributions RCP is tested on, by `/etc/os-release` ID. Others are
# allowed with a "not tested" note, never refused for that reason.
TESTED_LINUX_DISTRIBUTIONS = frozenset({"ubuntu"})
# Distributions whose install command is an apt line, by ID or ID_LIKE.
APT_DISTRIBUTIONS = frozenset({"ubuntu", "debian"})
# Packages every supported Ubuntu server image already has; the server guide's
# install line need not repeat them.
UBUNTU_BASE_PACKAGES = frozenset({"bash", "coreutils", "dash", "systemd"})


@dataclass(frozen=True)
class Dependency:
    name: str
    purpose: str
    required_on: frozenset[Role] = frozenset()
    optional_on: frozenset[Role] = frozenset()
    # For optional roles: what stops working, and what the user sees instead.
    feature: str = ""
    fallback: str = ""
    platforms: frozenset[Platform] = BOTH
    # Debian/Ubuntu package that provides it.
    apt: str | None = None
    # Install instruction where apt does not apply or is not enough.
    note: str | None = None
    # Set when the code starts it by this absolute path rather than from PATH.
    path: str | None = None

    def __post_init__(self) -> None:
        if not self.purpose:
            raise ValueError(f"{self.name}: every dependency states its purpose")
        if not self.required_on and not self.optional_on:
            raise ValueError(f"{self.name}: no role uses it")
        if self.required_on & self.optional_on:
            raise ValueError(f"{self.name}: a role is both required and optional")
        if self.optional_on and not (self.feature and self.fallback):
            raise ValueError(f"{self.name}: optional roles need a feature and a fallback")
        if "linux" not in self.platforms and (self.required_on | self.optional_on) & {
            "remote",
            "server",
            "server_install",
        }:
            raise ValueError(f"{self.name}: remote and server machines are Linux")


DEPENDENCIES: tuple[Dependency, ...] = (
    Dependency(
        "age",
        "Encrypt and decrypt protected backups.",
        required_on=frozenset({"server"}),
        platforms=LINUX,
        apt="age",
    ),
    Dependency(
        "age-keygen",
        "Create the backup identity and read back its public recipient.",
        required_on=frozenset({"server"}),
        platforms=LINUX,
        apt="age",
    ),
    Dependency(
        "apt-get",
        "Install the agent browser's system libraries on Ubuntu.",
        optional_on=frozenset({"server"}),
        feature="Automatic browser library install during server install and update.",
        fallback="Install and update report a warning, and browser readiness shows the apt command.",
        platforms=LINUX,
        apt="apt",
    ),
    Dependency(
        "bash",
        "Login shell for provider discovery, watchers, and terminals.",
        required_on=frozenset({"local", "remote"}),
        apt="bash",
        path="/bin/bash",
    ),
    Dependency(
        "bwrap",
        "Hide selected secrets from agent tool calls and the browser on Linux.",
        optional_on=frozenset({"local", "remote"}),
        feature="Secret hiding on Linux.",
        fallback="Agents run unhidden; Settings, doctor, and the machine card say so.",
        platforms=LINUX,
        apt="bubblewrap",
    ),
    Dependency(
        "basename",
        "Name the newest patch on a remote machine during state sync.",
        required_on=frozenset({"remote"}),
        platforms=LINUX,
        apt="coreutils",
    ),
    Dependency(
        "cat",
        "Read files from a run folder.",
        required_on=frozenset({"remote"}),
        platforms=LINUX,
        apt="coreutils",
    ),
    Dependency(
        "curl",
        "Download installers during server install and provider updates.",
        required_on=frozenset({"server_install"}),
        platforms=LINUX,
        apt="curl",
    ),
    Dependency(
        "env",
        "Start commands with an explicit environment.",
        required_on=frozenset({"local", "remote"}),
        apt="coreutils",
    ),
    Dependency(
        "find",
        "List a remote machine's patch log during state sync.",
        required_on=frozenset({"remote"}),
        platforms=LINUX,
        apt="findutils",
    ),
    Dependency(
        "findmnt",
        "Verify the read-only mounts of a mirrored terminal.",
        optional_on=frozenset({"local", "remote"}),
        feature="Mirrored terminals on Linux.",
        fallback="The terminal is unavailable, with the reason.",
        platforms=LINUX,
        apt="util-linux",
    ),
    Dependency(
        "getent",
        "Prove the service account's password state at install.",
        required_on=frozenset({"server_install"}),
        platforms=LINUX,
        apt="libc-bin",
    ),
    Dependency(
        "git",
        "Repository, worktree, bundle, and credential operations.",
        required_on=frozenset({"local", "remote"}),
        apt="git",
        note="On macOS, run xcode-select --install.",
    ),
    Dependency(
        "id",
        "Read the execution account's numeric user id.",
        required_on=frozenset({"local", "remote"}),
        apt="coreutils",
    ),
    Dependency(
        "launchctl",
        "Own helper jobs, the browser, and keep-awake through launchd on macOS.",
        optional_on=frozenset({"local"}),
        feature="Helper compute jobs, the agent browser, and keep-awake on macOS.",
        fallback="That feature is unavailable, with the reason.",
        platforms=MACOS,
    ),
    Dependency(
        "ldd",
        "Find the agent browser's missing shared libraries.",
        optional_on=frozenset({"local", "remote"}),
        feature="Agent browser on Linux.",
        fallback="The browser is unavailable, with the reason.",
        platforms=LINUX,
        apt="libc-bin",
    ),
    Dependency(
        "loginctl",
        "Enable and read the execution account's linger.",
        required_on=frozenset({"server"}),
        optional_on=frozenset({"local", "remote"}),
        feature="Helper jobs and the agent browser outliving the login session.",
        fallback="That feature is unavailable; the machine card offers the linger fix or its admin command.",
        platforms=LINUX,
        apt="systemd",
    ),
    Dependency(
        "mkdir",
        "Create run and state folders.",
        required_on=frozenset({"remote"}),
        platforms=LINUX,
        apt="coreutils",
    ),
    Dependency(
        "node",
        "Run the pinned Playwright CLI for the agent browser.",
        optional_on=frozenset({"local", "remote"}),
        feature="Agent browser.",
        fallback="Browser use cannot be turned on without Node.js 20 or newer.",
        note="Install Node.js 20 or newer.",
    ),
    Dependency(
        "npm",
        "Install the Playwright CLI and build the web app from source.",
        optional_on=frozenset({"local", "remote"}),
        feature="Agent browser install and source web builds.",
        fallback="That operation fails with the reason; the packaged app does not need npm.",
        note="Install Node.js 20 or newer, which includes npm.",
    ),
    Dependency(
        "osascript",
        "Ask for administrator approval to install keep-awake on macOS.",
        optional_on=frozenset({"local"}),
        feature="Keep-awake install and uninstall.",
        fallback="The install reports an error and changes nothing.",
        platforms=MACOS,
    ),
    Dependency(
        "printenv",
        "Read the execution account's home folder before resolving hidden paths.",
        optional_on=frozenset({"remote"}),
        feature="Secret hiding on a remote machine.",
        fallback="Agents run unhidden, with the reason.",
        platforms=LINUX,
        apt="coreutils",
    ),
    Dependency(
        "ps",
        "Confirm process identity when stopping a run.",
        optional_on=frozenset({"local", "remote"}),
        feature="Confirming that a stopped run's provider process ended.",
        fallback="The stop is reported as unconfirmed.",
        apt="procps",
    ),
    Dependency(
        "python3",
        "Run RCP's shipped helpers on a remote machine.",
        required_on=frozenset({"remote"}),
        platforms=LINUX,
        apt="python3",
    ),
    Dependency(
        "rm",
        "Clean up staged run handoffs.",
        required_on=frozenset({"remote"}),
        platforms=LINUX,
        apt="coreutils",
    ),
    Dependency(
        "rsync",
        "Transfer project state and run inputs.",
        required_on=frozenset({"local", "remote"}),
        apt="rsync",
    ),
    Dependency(
        "runuser",
        "Run install steps as the service account.",
        required_on=frozenset({"server_install"}),
        platforms=LINUX,
        apt="util-linux",
    ),
    Dependency(
        "sandbox-exec",
        "Hide selected secrets from agent tool calls on macOS.",
        optional_on=frozenset({"local"}),
        feature="Secret hiding on macOS.",
        fallback="Agents run unhidden; Settings says so.",
        platforms=MACOS,
        path="/usr/bin/sandbox-exec",
    ),
    Dependency(
        "setsid",
        "Start a remote provider in its own session.",
        required_on=frozenset({"remote"}),
        platforms=LINUX,
        apt="util-linux",
    ),
    Dependency(
        "sh",
        "POSIX shell for command wrappers and scripts.",
        required_on=frozenset({"local", "remote"}),
        apt="dash",
    ),
    Dependency(
        "sleep",
        "Keep the compute probe job alive while it is observed.",
        optional_on=frozenset({"remote"}),
        feature="The compute route probe.",
        fallback="The compute route reports unavailable, with the reason.",
        platforms=LINUX,
        apt="coreutils",
    ),
    Dependency(
        "sort",
        "Order a remote machine's patch log during state sync.",
        required_on=frozenset({"remote"}),
        platforms=LINUX,
        apt="coreutils",
    ),
    Dependency(
        "ssh",
        "Reach remote machines and carry Git transport.",
        required_on=frozenset({"local", "remote"}),
        apt="openssh-client",
    ),
    Dependency(
        "ssh-add",
        "Load deploy keys into the account's SSH agent.",
        optional_on=frozenset({"local", "remote"}),
        feature="Deploy keys held by the SSH agent instead of readable files.",
        fallback="The keys stay readable for that run, with the reason.",
        apt="openssh-client",
    ),
    Dependency(
        "ssh-agent",
        "Hold deploy keys for the account.",
        optional_on=frozenset({"local", "remote"}),
        feature="Deploy keys held by the SSH agent instead of readable files.",
        fallback="The keys stay readable for that run, with the reason.",
        apt="openssh-client",
    ),
    Dependency(
        "ssh-keygen",
        "Create repository-scoped keys and derive their public keys.",
        required_on=frozenset({"server"}),
        optional_on=frozenset({"local", "remote"}),
        feature="Setting up a repository key on that machine.",
        fallback="Key setup is refused, with the reason.",
        apt="openssh-client",
    ),
    Dependency(
        "sudo",
        "Prove the service account has no sudo authority at install.",
        required_on=frozenset({"server_install"}),
        platforms=LINUX,
        apt="sudo",
    ),
    Dependency(
        "systemctl",
        "Run the server's services and reach the account's user manager.",
        required_on=frozenset({"server"}),
        optional_on=frozenset({"local", "remote"}),
        feature="Mirrored terminals and helper jobs on Linux.",
        fallback="That feature is unavailable, with the reason.",
        platforms=LINUX,
        apt="systemd",
    ),
    Dependency(
        "systemd-run",
        "Start terminals, the browser, and helper jobs as transient user units.",
        optional_on=frozenset({"local", "remote"}),
        feature="Mirrored terminals, the agent browser, and helper jobs on Linux.",
        fallback="That feature is unavailable, with the reason.",
        platforms=LINUX,
        apt="systemd",
    ),
    Dependency(
        "tail",
        "Pick the newest entry of a remote machine's patch log during state sync.",
        required_on=frozenset({"remote"}),
        platforms=LINUX,
        apt="coreutils",
    ),
    Dependency(
        "tar",
        "Extract state archives on a remote machine.",
        required_on=frozenset({"remote"}),
        platforms=LINUX,
        apt="tar",
    ),
    Dependency(
        "test",
        "Check files from remote shell commands.",
        required_on=frozenset({"remote"}),
        platforms=LINUX,
        apt="coreutils",
    ),
    Dependency(
        "true",
        "Prove an SSH route works.",
        required_on=frozenset({"local", "remote"}),
        apt="coreutils",
    ),
    Dependency(
        "uname",
        "Identify the operating system.",
        required_on=frozenset({"local", "remote"}),
        apt="coreutils",
    ),
    Dependency(
        "useradd",
        "Create the service account at install.",
        required_on=frozenset({"server_install"}),
        platforms=LINUX,
        apt="passwd",
    ),
    Dependency(
        "uv",
        "Manage the server's Python and install verified releases.",
        required_on=frozenset({"server"}),
        platforms=LINUX,
        note="Install uv with the server guide's uv step.",
    ),
)

BY_NAME: dict[str, Dependency] = {dependency.name: dependency for dependency in DEPENDENCIES}
if len(BY_NAME) != len(DEPENDENCIES):
    raise ValueError("Each external program is declared once.")


def required(role: Role, platform: Platform) -> tuple[Dependency, ...]:
    """Programs a machine in this role on this platform must have."""
    if role != "local" and platform != "linux":
        return ()
    return tuple(d for d in DEPENDENCIES if role in d.required_on and platform in d.platforms)


def optional(role: Role, platform: Platform) -> tuple[Dependency, ...]:
    """Programs that only gate one feature for this role on this platform."""
    if role != "local" and platform != "linux":
        return ()
    return tuple(d for d in DEPENDENCIES if role in d.optional_on and platform in d.platforms)
