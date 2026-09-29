"""Fail the packaged backend early when required source data was omitted."""

from rcp.agents.command_protocol import staged_command_broker_source, staged_command_client_source
from rcp.artifact_comments import _comment_panel_script, _selection_script
from rcp.providers import remote_bundle
from rcp.skill_registry import official_registry
from rcp.terminals.remote import terminal_source
from rcp.transfer.repository_git import _remote_source
from rcp.transport.state import _remote_lock_holder_script, _remote_script

if "function installArtifactSelection" not in _selection_script():
    raise RuntimeError("The packaged artifact selection script is invalid.")

if "function saveSelections" not in _comment_panel_script():
    raise RuntimeError("The packaged artifact viewer script is invalid.")

if "def run_repository_transfer" not in _remote_source():
    raise RuntimeError("The packaged repository Git transfer source is invalid.")

bundle = remote_bundle("")
if "def normalize_record" not in bundle or "TURN_FENCES.update" not in bundle:
    raise RuntimeError("The packaged provider remote bundle is invalid.")

client_source = staged_command_client_source()
if "def _atomic_request" not in client_source or "watch-graph" not in client_source:
    raise RuntimeError("The packaged staged agent command client is invalid.")

broker_source = staged_command_broker_source()
if (
    "def _peer_identity" not in broker_source
    or "def _is_live_descendant" not in broker_source
    or "SO_PEERCRED" not in broker_source
):
    raise RuntimeError("The packaged staged auto-research command broker is invalid.")

for script_name, required in (
    ("remote_terminal.py", "def run_session"),
    ("remote_terminal_probe.py", "def probe_machine"),
    ("remote_lock_holder.py", "def apply_staged"),
    ("remote_archive_research.py", "def retained_history_fingerprint"),
    ("remote_read_kept_view.py", "def main"),
    ("conversation_worktree.py", "def execute"),
    ("remote_terminate_provider.py", "def terminate_provider"),
    ("provider_discovery.py", "def discover_provider"),
    ("remote_turn_supervisor.py", "def main"),
):
    if required not in _remote_script(script_name):
        raise RuntimeError(f"The packaged remote script {script_name} is invalid.")

lock_holder = _remote_lock_holder_script()
if "def replace_regular_file_in_open_directory" not in lock_holder:
    raise RuntimeError("The packaged remote lock holder lacks the artifact replacement protocol.")

for script_name, required in (
    ("profile.py", "def launch_command"),
    ("git_access.py", "def terminal_git_access"),
):
    if required not in terminal_source(script_name):
        raise RuntimeError(f"The packaged terminal script {script_name} is invalid.")

if not official_registry().packages:
    raise RuntimeError("The packaged official skill registry is empty.")
