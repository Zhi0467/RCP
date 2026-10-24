from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rcp.machine_login import MachineLoginFailure, login_account, trust_host_key

KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


def _runner(tmp_path, *, login_stderr="", key=KEY, fingerprint="SHA256:abc"):
    known_hosts = tmp_path / "ssh" / "known_hosts"
    global_hosts = tmp_path / "ssh_known_hosts"

    def run(argv, **kwargs):
        if argv[:2] == ["ssh", "-G"]:
            out = (
                f"hostname 10.0.0.5\nport 22\nuserknownhostsfile {known_hosts}\n"
                f"globalknownhostsfile {global_hosts}\n"
            )
        elif argv[0] == "ssh" and "StrictHostKeyChecking=accept-new" in argv:
            # SSH records the offered key in the scratch file it was given, then fails auth.
            scratch = next(o for o in argv if o.startswith("UserKnownHostsFile="))
            Path(scratch.partition("=")[2]).write_text(f"10.0.0.5 ssh-ed25519 {key}\n")
            return subprocess.CompletedProcess(argv, 255, "", "Permission denied (none).")
        elif argv[0] == "ssh":
            code = 255 if login_stderr else 0
            return subprocess.CompletedProcess(argv, code, "" if code else "alice\n", login_stderr)
        elif argv[:2] == ["ssh-keygen", "-l"]:
            out = f"256 {fingerprint} 10.0.0.5 (ED25519)\n"
        elif argv[:2] == ["ssh-keygen", "-F"]:
            recorded = Path(argv[-1])
            lines = recorded.read_text().splitlines() if recorded.exists() else []
            found = [line for line in lines if line.startswith("10.0.0.5 ")]
            out = "".join(f"# Host 10.0.0.5 found\n{line}\n" for line in found)
            return subprocess.CompletedProcess(argv, 0 if found else 1, out, "")
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, out, "")

    return run, known_hosts


def test_login_names_the_account_or_the_kind_of_failure(tmp_path) -> None:
    run, _ = _runner(tmp_path)
    assert login_account("gpu", runner=run) == "alice"
    cases = {
        "Permission denied (publickey).": ("authentication_failed", None),
        "No ED25519 host key is known for gpu.\nHost key verification failed.": (
            "host_key_unknown",
            "SHA256:abc",
        ),
        "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\nHost key verification failed.": (
            "host_key_changed",
            None,
        ),
        "ssh: connect to host gpu port 22: Connection refused": ("unreachable", None),
    }
    for stderr, expected in cases.items():
        run, _ = _runner(tmp_path, login_stderr=stderr)
        with pytest.raises(MachineLoginFailure) as failure:
            login_account("gpu", runner=run)
        assert (failure.value.code, failure.value.fingerprint) == expected


def test_trust_records_only_the_compared_key_once(tmp_path) -> None:
    moved, known_hosts = _runner(tmp_path, fingerprint="SHA256:other")
    with pytest.raises(MachineLoginFailure, match="host_key_changed"):
        trust_host_key("gpu", "SHA256:abc", runner=moved)
    assert not known_hosts.exists()

    run, known_hosts = _runner(tmp_path)
    trust_host_key("gpu", "SHA256:abc", runner=run)
    assert known_hosts.read_text() == f"10.0.0.5 ssh-ed25519 {KEY}\n"
    assert known_hosts.parent.stat().st_mode & 0o777 == 0o700
    # A recorded key is never replaced or joined from the UI.
    with pytest.raises(MachineLoginFailure, match="host_key_changed"):
        trust_host_key("gpu", "SHA256:abc", runner=run)
    assert known_hosts.read_text().count("\n") == 1


def test_trust_refuses_a_host_another_known_hosts_file_records(tmp_path) -> None:
    run, known_hosts = _runner(tmp_path)
    (tmp_path / "ssh_known_hosts").write_text("10.0.0.5 ssh-ed25519 OLDKEY\n")
    with pytest.raises(MachineLoginFailure, match="host_key_changed"):
        trust_host_key("gpu", "SHA256:abc", runner=run)
    assert not known_hosts.exists()
