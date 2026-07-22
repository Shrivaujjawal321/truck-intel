"""Per-source parsers — the ONLY per-source code in the engine.

Contract: each module exposes `parse(raw: bytes) -> Iterator[dict]` turning the
raw downloaded file into normalized row dicts (documented per module). Parsers
map fields BY NAME, never by column position (survives column shifts like the
NBI->SNBI 2028 migration). Parsers set observed_at to when the fact was true in
the world — never the download date.
"""
