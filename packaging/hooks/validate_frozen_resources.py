"""Fail the packaged backend early when required source data was omitted."""

from rcp.skill_registry import official_registry
from rcp.sources.indexer import _record_parsing_source

parser_source = _record_parsing_source()
if "def normalize_record" not in parser_source:
    raise RuntimeError("The packaged shared conversation parser is invalid.")

if not official_registry().packages:
    raise RuntimeError("The packaged official skill registry is empty.")
