from __future__ import annotations

import shlex

import pytest

from rcp.dependency_check import install_argv
from rcp.operator_commands import OperatorRoute

APT = ["apt-get", "install", "--yes", "rsync"]


@pytest.mark.parametrize(
    ("route", "root", "expected"),
    [
        # Personal space: RCP runs as the human.
        (OperatorRoute(), True, ["sudo", *APT]),
        (OperatorRoute(host="gpu"), True, ["ssh", "-t", "gpu", shlex.join(["sudo", *APT])]),
        # Team server: the human is in their own login, RCP is the service account.
        (OperatorRoute(service_account="rcp"), True, ["sudo", *APT]),
        (OperatorRoute(service_account="rcp"), False, ["sudo", "-u", "rcp", "-H", *APT]),
        (
            OperatorRoute(host="gpu", service_account="rcp"),
            True,
            ["sudo", "-u", "rcp", "-H", "ssh", "-t", "gpu", shlex.join(["sudo", *APT])],
        ),
    ],
)
def test_each_route_renders_the_whole_line_from_the_humans_prompt(route, root, expected) -> None:
    assert shlex.split(route.command(APT, root=root)) == expected


def test_node_installs_from_nodesource_after_the_plain_packages() -> None:
    assert install_argv(["rsync"]) == APT
    shell, flag, script = install_argv(["node", "npm", "rsync", "curl"])
    assert (shell, flag) == ("sh", "-c")
    steps = script.split(" && ")
    assert shlex.split(steps[0]) == ["apt-get", "install", "--yes", "curl", "rsync"]
    assert "deb.nodesource.com/setup_20.x" in steps[1]
    assert shlex.split(steps[-1]) == ["apt-get", "install", "--yes", "nodejs"]
