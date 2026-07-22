"""truck-intel ingestion engine.

MVP spine: registry (YAML) -> polite fetch -> raw file on disk -> parse ->
validation gates -> PostGIS -> ops.source_runs audit row. See README.md.
"""
