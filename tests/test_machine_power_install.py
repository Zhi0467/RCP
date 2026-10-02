from __future__ import annotations

import fcntl
import json
import os
import shlex
import subprocess
import sys
import threading

import pytest

from rcp.machine_power_install import (
    InstallError,
    InstallPaths,
    MachinePowerInstaller,
    _daemon,
    _sudoers,
)

# The admin script runs only on macOS and uses BSD tools (`stat -f`, `mv -h`).
macos_tools = pytest.mark.skipif(sys.platform != "darwin", reason="macOS BSD tools")


@pytest.fixture
def installer(tmp_path):
    paths = InstallPaths(tmp_path / "RCP" / "machine", tmp_path / "sudoers", tmp_path / "daemon")
    pmset = tmp_path / "pmset"
    stat = tmp_path / "stat"
    stat.write_text('#!/bin/sh\nif [ "$2" = %u ]; then echo 0; else /usr/bin/stat "$@"; fi\n')
    stat.chmod(0o700)
    pmset.write_text("#!/bin/sh\necho ' SleepDisabled 0'\n")
    pmset.chmod(0o700)

    def run(argv, timeout):
        assert argv[:2] == ["/usr/bin/osascript", "-e"]
        script = json.loads(
            argv[2].removeprefix("do shell script ").removesuffix(" with administrator privileges")
        )
        # Execute the actual installation shell with all privileged executors
        # replaced. Nothing can reach host power settings or system directories.
        for command in ("/usr/sbin/chown", "/usr/sbin/visudo", "/bin/launchctl"):
            script = script.replace(command, "/usr/bin/true")
        script = script.replace("/usr/bin/stat", str(stat))
        script = script.replace("/usr/bin/pmset", str(pmset))
        # Keep content embedded in sudoers and plist canonical; only execution
        # lines use the fake pmset executable.
        script = script.replace(f"{pmset} -a", "/usr/bin/pmset -a")
        script = script.replace(f"<string>{pmset}</string>", "<string>/usr/bin/pmset</string>")
        script = script.replace("/private/tmp/rcp-", str(tmp_path / "rcp-"))
        return subprocess.run(
            ["/bin/sh", "-c", script], capture_output=True, text=True, timeout=timeout
        )

    return MachinePowerInstaller(run=run, paths=paths, account="tester", uid=os.getuid())


@macos_tools
def test_install_uninstall_reuses_lock_and_directory(installer):
    assert installer.status().install_problem == "not_installed"
    assert installer.install().installed
    lock = installer.paths.directory / "owner.lock"
    inode = lock.stat().st_ino
    for name in (
        "machine_power_watchdog.sh",
        "activation",
        "heartbeat",
        "ack",
        "result",
        "revoked",
    ):
        (installer.paths.directory / name).touch()
    called = []
    assert installer.uninstall(lambda: called.append(True)).install_problem == "not_installed"
    assert called == [True]
    assert set(p.name for p in installer.paths.directory.iterdir()) == {"owner.lock"}
    assert lock.stat().st_ino == inode
    assert installer.install().installed
    assert lock.stat().st_ino == inode


@macos_tools
@pytest.mark.parametrize("missing", ["sudoers", "daemon", "directory"])
def test_partial_install_repair(installer, missing):
    installer.install()
    path = getattr(installer.paths, missing)
    if missing == "directory":
        for child in path.iterdir():
            child.unlink()
        path.rmdir()
    else:
        path.unlink()
    assert installer.status().install_problem == "partial"
    assert installer.install().installed


@pytest.mark.parametrize("name", ["sudoers", "daemon", "directory"])
def test_foreign_file_refused(installer, name):
    getattr(installer.paths, name).parent.mkdir(parents=True, exist_ok=True)
    getattr(installer.paths, name).write_text("foreign")
    assert installer.status().install_problem == "foreign_file"
    with pytest.raises(InstallError, match="foreign_file"):
        installer.install()
    with pytest.raises(InstallError, match="foreign_file"):
        installer.uninstall()


def test_other_account_refused(installer):
    installer.paths.sudoers.write_text(_sudoers("someone", 12345))
    installer.paths.daemon.write_text(_daemon("someone", 12345))
    assert installer.status().install_problem == "other_account"
    with pytest.raises(InstallError, match="other_account"):
        installer.install()


@macos_tools
@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_cancel_preserves_status_and_skips_callback(installer, operation):
    installer.install()
    before = installer.status()
    installer.run = lambda argv, timeout: subprocess.CompletedProcess(
        argv, 1, "", "User canceled (-128)"
    )
    called = []
    result = (
        installer.install()
        if operation == "install"
        else installer.uninstall(lambda: called.append(True))
    )
    assert result == before
    assert installer.cancelled
    assert not called


@macos_tools
def test_active_owner_refuses_install(installer):
    installer.install()
    with (installer.paths.directory / "owner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(InstallError, match="owner_busy"):
            installer.install()


def test_admin_timeout_is_reported(installer):
    def run(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    installer.run = run
    with pytest.raises(InstallError, match="admin_failed"):
        installer.install()


def test_admin_revalidates_foreign_file_after_prompt(installer):
    original = installer.run

    def run(argv, timeout):
        installer.paths.sudoers.write_text("foreign")
        return original(argv, timeout)

    installer.run = run
    with pytest.raises(InstallError, match="foreign_file"):
        installer.install()
    assert installer.paths.sudoers.read_text() == "foreign"


@macos_tools
def test_failed_clear_keeps_installation_for_repair(installer):
    fake_pmset = installer.paths.sudoers.parent / "pmset"
    fake_pmset.write_text("#!/bin/sh\necho 'SleepDisabled 1'\n")
    with pytest.raises(InstallError, match="clear_failed"):
        installer.install()
    assert installer.paths.sudoers.exists()
    assert installer.status().install_problem == "partial"
    fake_pmset.write_text("#!/bin/sh\necho 'SleepDisabled 0'\n")
    assert installer.install().installed


def test_visudo_failure_does_not_publish(installer):
    original = installer.run

    def run(argv, timeout):
        argv = [*argv[:2], argv[2].replace("/usr/sbin/visudo", "/usr/bin/false")]
        return original(argv, timeout)

    installer.run = run
    with pytest.raises(InstallError, match="admin_failed"):
        installer.install()
    assert not installer.paths.sudoers.exists()
    assert not installer.paths.directory.exists()


@macos_tools
def test_reinstall_from_another_account_after_uninstall(installer):
    installer.install()
    lock_inode = (installer.paths.directory / "owner.lock").stat().st_ino
    installer.uninstall()
    installer.account = "another"
    installer.uid = 54321
    assert installer.status().install_problem == "not_installed"
    assert installer.install().installed
    assert (installer.paths.directory / "owner.lock").stat().st_ino == lock_inode


@macos_tools
def test_interrupted_enrollment_and_ready_are_repairable(installer):
    installer.install()
    (installer.paths.directory / "enrollment").unlink()
    (installer.paths.directory / "installed-ready").unlink()
    assert installer.status().install_problem == "partial"
    assert installer.install().installed


def test_cancel_fresh_install_creates_no_machine_files(installer):
    installer.run = lambda argv, timeout: subprocess.CompletedProcess(argv, 1, "", "(-128)")
    assert installer.install().install_problem == "not_installed"
    assert not installer.paths.directory.exists()


@macos_tools
def test_uninstall_bounds_runner_that_ignores_timeout(installer, monkeypatch):
    installer.install()
    release = threading.Event()
    finished = threading.Event()

    def run(argv, timeout):
        assert release.wait(timeout=2)
        finished.set()
        return subprocess.CompletedProcess(argv, 1, "", "(-128)")

    installer.run = run
    monkeypatch.setattr("rcp.machine_power_install.MACHINE_POWER_ADMIN_TIMEOUT_SECONDS", 0.02)
    try:
        with pytest.raises(InstallError, match="admin_failed"):
            installer.uninstall()
    finally:
        release.set()
        assert finished.wait(1)
    assert installer.status().installed


@macos_tools
def test_approved_script_gets_its_own_window(installer, monkeypatch, tmp_path):
    """Work after approval is not bounded by what is left of the prompt's window."""

    monkeypatch.setattr("rcp.machine_power_install.MACHINE_POWER_ADMIN_TIMEOUT_SECONDS", 2)
    # The post-approval flag check outlasts the whole 2 s prompt window.
    (tmp_path / "pmset").write_text("#!/bin/sh\nsleep 2.2\necho ' SleepDisabled 0'\n")
    assert installer.install().installed


@macos_tools
def test_fresh_install_parent_is_traversable(installer):
    assert not installer.paths.directory.parent.exists()
    assert installer.install().installed
    assert installer.paths.directory.parent.stat().st_mode & 0o777 == 0o755


@macos_tools
def test_repair_lock_symlink_swap_does_not_modify_target(installer, tmp_path):
    installer.install()
    target = tmp_path / "protected"
    target.write_text("protected")
    target.chmod(0o600)
    lock = installer.paths.directory / "owner.lock"
    original = installer.run

    def run(argv, timeout):
        script = json.loads(
            argv[2].removeprefix("do shell script ").removesuffix(" with administrator privileges")
        )
        # Swap after provenance validation, immediately before the ownership
        # operation. The fake ownership command rejects any following chown.
        ownership = f"bounded /usr/sbin/chown -h {installer.uid} {shlex.quote(str(lock))}"
        attack = (
            f"/bin/rm {shlex.quote(str(lock))}\n"
            f"/bin/ln -s {shlex.quote(str(target))} {shlex.quote(str(lock))}\n"
        )
        script = script.replace(ownership, attack + ownership)
        fake_chown = tmp_path / "chown"
        fake_chown.write_text('#!/bin/sh\n[ "$1" = -h ] || exit 99\n/usr/bin/true\n')
        fake_chown.chmod(0o700)
        script = script.replace("/usr/sbin/chown", str(fake_chown))
        return original(
            [*argv[:2], f"do shell script {json.dumps(script)} with administrator privileges"],
            timeout,
        )

    installer.run = run
    with pytest.raises(InstallError, match="foreign_file"):
        installer.install()
    assert target.read_text() == "protected"
    assert target.stat().st_mode & 0o777 == 0o600


@macos_tools
def test_first_install_holds_machine_lock_before_loading_daemon(installer, tmp_path):
    probe = tmp_path / "launchctl"
    observed = tmp_path / "excluded"
    probe.write_text(
        f"#!{sys.executable}\n"
        "import fcntl\n"
        "from pathlib import Path\n"
        f"with Path({str(installer.paths.directory / 'owner.lock')!r}).open() as lock:\n"
        "    try:\n"
        "        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    except BlockingIOError:\n"
        f"        Path({str(observed)!r}).touch()\n"
        "    else:\n"
        "        raise SystemExit(99)\n"
    )
    probe.chmod(0o700)
    original = installer.run

    def run(argv, timeout):
        return original([*argv[:2], argv[2].replace("/bin/launchctl", str(probe))], timeout)

    installer.run = run
    assert installer.install().installed
    assert observed.exists()


@macos_tools
def test_uninstall_clears_only_after_removing_the_rule(installer):
    """Once the backend's lock lapses, a later owner may set the flag while the rule exists."""
    assert installer.install().installed
    fake_pmset = installer.paths.sudoers.parent / "pmset"
    fake_pmset.write_text(
        f"#!/bin/sh\nif [ -e {installer.paths.sudoers} ]; then echo ' SleepDisabled 1'; "
        "else echo ' SleepDisabled 0'; fi\n"
    )
    assert installer.uninstall().install_problem == "not_installed"


@macos_tools
@pytest.mark.parametrize(
    "output,cleared",
    [
        ("System-wide power settings:\\n", True),
        (" disablesleep 0\\n", True),
        ("truncated\\n", False),
        (" disablesleep 1\\n", False),
    ],
)
def test_clear_check_reads_the_flag_like_the_controller(installer, output, cleared):
    fake_pmset = installer.paths.sudoers.parent / "pmset"
    fake_pmset.write_text(f"#!/bin/sh\nprintf '{output}'\n")
    if cleared:
        assert installer.install().installed
    else:
        with pytest.raises(InstallError, match="clear_failed"):
            installer.install()
