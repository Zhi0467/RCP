"""Fail the packaged backend early when required source data was omitted."""

from rcp.sources.indexer import _record_parsing_source

parser_source = _record_parsing_source()
if "def normalize_record" not in parser_source:
    raise RuntimeError("The packaged shared conversation parser is invalid.")
