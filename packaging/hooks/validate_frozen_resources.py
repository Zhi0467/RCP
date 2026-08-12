"""Fail the packaged backend early when required source data was omitted."""

from rcp.agents.command_protocol import staged_command_broker_source, staged_command_client_source
from rcp.skill_registry import official_registry
from rcp.sources.indexer import _record_parsing_source

parser_source = _record_parsing_source()
if "def normalize_record" not in parser_source:
    raise RuntimeError("The packaged shared conversation parser is invalid.")

client_source = staged_command_client_source()
if "def _atomic_json" not in client_source or "watch-graph" not in client_source:
    raise RuntimeError("The packaged staged agent command client is invalid.")

broker_source = staged_command_broker_source()
if (
    "def _peer_identity" not in broker_source
    or "def _is_live_descendant" not in broker_source
    or "SO_PEERCRED" not in broker_source
):
    raise RuntimeError("The packaged staged campaign command broker is invalid.")

if not official_registry().packages:
    raise RuntimeError("The packaged official skill registry is empty.")
