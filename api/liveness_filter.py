"""Gate 6 on the /v1 surface — one rendering, one filter, three tables.

truckintel/liveness.py computes the score and writes it. This module decides
what the HTTP surface does with it, and it lives in one file so /v1/places and
/v1/parking cannot drift into disagreeing about what 'closed' means.

THE ONE RULE, and why it is not a threshold
-------------------------------------------
Rows are hidden by default when, and only when, live_state = 'closed' — which
Gate 6 sets only from a POSITIVE closure assertion by a source that carries one
(FSQ date_closed, an OSM disused:/was: lifecycle prefix). Never from a low score.

    'closed'         a source said so           -> hidden by default
    'likely_closed'  scored under 25            -> returned, badged
    'unknown'        old data, nothing current  -> returned, badged
    NULL             never scored               -> returned (unscored != closed)

A `min_liveness=` threshold IS offered, because an integrator building, say, a
"only show me places somebody has confirmed this year" view has a legitimate
need for one. It is opt-in and it says so in filter_notes, because a threshold
excludes honest unknowns and the caller should own that choice — the default
must not make it for them.

Absence of evidence is not evidence of absence: the same rule the scorer is
bound by, enforced again at the edge. See truckintel/liveness.py.
"""
from __future__ import annotations

from typing import Any

from api import common

# Column list every liveness-aware SELECT pulls. Components travel with the
# score for the same reason Gate 5's do: "why is this 32?" must be answerable
# from the response, without a second call and without re-running the scorer.
SELECT_COLS = """
       {a}.liveness, {a}.live_state, {a}.live_presence, {a}.live_sources,
       {a}.live_corrob, {a}.live_reasons, {a}.last_seen_at, {a}.last_seen_src
"""

CLOSED = "closed"

_NOTE = (
    "liveness answers 'is this place still there?' (Gate 6) and is a different "
    "question from confidence, which scores the record. Rows a source has "
    "positively asserted CLOSED are excluded by default — pass "
    "include_closed=true to see them. Rows scored 'unknown' or 'likely_closed' "
    "ARE returned: those are scores, not closure assertions, and hiding them "
    "would report absence of evidence as evidence of absence."
)


def select_cols(alias: str) -> str:
    """The liveness column list, qualified to a table alias."""
    return SELECT_COLS.format(a=alias)


def note() -> str:
    """One sentence for the collection-level `note`, so the default is stated."""
    return _NOTE


def props(r: dict) -> dict[str, Any]:
    """The liveness block for a Feature's properties.

    NULL renders 'unknown' throughout — an unscored row is not a dead one, and
    last_seen_at is a source's own observation vintage, never our fetch time.
    """
    return {
        "liveness": common.unknown(r["liveness"]),
        "live_state": common.unknown(r["live_state"]),
        "liveness_components": {
            "presence": common.unknown(r["live_presence"]),
            "sources": common.unknown(r["live_sources"]),
            "corroboration": common.unknown(r["live_corrob"]),
        },
        "liveness_reasons": r["live_reasons"] or [],
        "last_seen_at": common.unknown(r["last_seen_at"]),
        "last_seen_src": common.unknown(r["last_seen_src"]),
    }


def where(
    alias: str,
    *,
    include_closed: bool = False,
    min_liveness: int | None = None,
) -> tuple[list[str], list[Any], list[str]]:
    """(where_clauses, params, filter_notes) for a liveness-aware list route.

    Every exclusion this returns also returns the sentence that explains it —
    a row count that silently differs from the table is the failure mode this
    whole system is built to avoid.
    """
    clauses: list[str] = []
    params: list[Any] = []
    notes: list[str] = []

    if include_closed:
        notes.append(
            "include_closed=true: rows a source asserted CLOSED are included; "
            "check live_state before routing anyone to them")
    else:
        # IS DISTINCT FROM, not <>: a NULL live_state is an unscored row, and
        # `live_state <> 'closed'` would silently drop every one of them.
        clauses.append(f"{alias}.live_state IS DISTINCT FROM %s")
        params.append(CLOSED)
        notes.append(
            "rows with live_state='closed' (a source positively asserted "
            "closure) are excluded; pass include_closed=true to include them")

    if min_liveness is not None:
        clauses.append(f"{alias}.liveness >= %s")
        params.append(min_liveness)
        notes.append(
            f"min_liveness={min_liveness} excludes rows scored below it AND "
            "rows with NULL (never scored) liveness — including honest "
            "'unknown' places that may well be open")

    return clauses, params, notes
