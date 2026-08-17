"""GET /v1/places + /v1/places/{business_id} — conflated business POIs
(core.businesses: Overture + FSQ, rebuilt by the businesses_conflate job).

ATTRIBUTION — deliberately NO OSM attribution here: core.businesses
structurally contains no OSM data (ruling §3.1-4c — the present_in CHECK in
sql/schema_wave2.sql admits only {'overture','fsq'}, so no OSM attribute
value can ever be in this table; OSM POIs live in the osm schema and are
served by /v1/fuel with their own ODbL attribution). Attributions carried
here are exactly the two contributing licenses:
  - Overture Maps Foundation places theme — CDLA-Permissive-2.0
  - Foursquare OS Places — Apache-2.0 (NOTICE preserved in
    data/config/category_map.yaml)

HONESTY:
- def (§6 ruling): the field appears ONLY when it is 'inferred' — a
  deterministic brand->DEF config match, rendered with an explicit marker
  ("def": "inferred" + def_note), never as observed fact. Absent field =
  unknown (the CHECK makes any other stored value impossible). There is no
  "no" state.
- confidence comes with its stored components (trust/fresh/complete/agree)
  so "why 65?" is always answerable; NULL renders "unknown".
- min_confidence= and q= filters EXCLUDE rows honestly and say so in
  filter_notes.
- No ratings, no reviews, no station prices — no free legal source exists
  (research/businesses.md §2/§3); the fields simply do not exist here.

INTEGRATOR NOTE — register in api/main.py (this file never edits main.py):
    from api import routes_places                 # add to the import block
    ...
    routes_places.router,                         # add to the include loop
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from api import common, liveness_filter

router = APIRouter()

ATTRIBUTIONS = [
    "© Overture Maps Foundation (CDLA-Permissive-2.0)",
    "Foursquare OS Places (Apache-2.0)",
]

# Mirrors the businesses_category_taxonomy CHECK (sql/schema_wave2.sql).
CATEGORY_SLUGS = frozenset({
    "truck_stop", "fuel_station", "def_retail", "truck_repair",
    "mobile_repair", "trailer_repair", "tire_service", "towing",
    "truck_wash", "truck_parts", "truck_dealer", "cat_scale",
    "weigh_station", "truck_parking", "rest_area", "restaurant",
    "fast_food", "cafe", "grocery", "motel", "hotel", "medical",
    "pharmacy", "laundry", "atm_bank", "unclassified",
})

_NOTE = (
    "Conflated Overture + FSQ business POIs. present_in shows which sources "
    "corroborate each place (2 sources -> higher agreement score). No "
    "ratings/reviews/prices — no free legal source exists. def appears only "
    "as 'inferred' (deterministic brand config, never observed fact); absent "
    "def = unknown, never 'no'."
)
_DEF_NOTE = (
    "inferred from the truck-stop chain's brand (deterministic config in "
    "git, data/config/def_brands.yaml) — not an observed fact"
)

_SELECT = f"""
SELECT b.business_id, b.name, b.category, b.brand, b.address, b.city,
       b.state, b.zip, b.address_norm, b.phone, b.website, b.present_in,
       b.def, b.confidence, b.conf_trust, b.conf_fresh, b.conf_complete,
       b.conf_agree, b.flags, b.source_id, b.run_id, b.ingested_at,
       b.observed_at, ST_AsGeoJSON(b.geom) AS gj,
       {liveness_filter.select_cols('b')}
"""


def _feature(r: dict, *, include_props: dict | None = None) -> dict:
    props = {
        "business_id": r["business_id"],
        "name": r["name"],
        "category": r["category"],
        "brand": common.unknown(r["brand"]),
        "address": common.unknown(r["address"]),
        "city": common.unknown(r["city"]),
        "state": common.unknown(r["state"]),
        "zip": common.unknown(r["zip"]),
        "address_norm": common.unknown(r["address_norm"]),
        "phone": common.unknown(r["phone"]),
        "website": common.unknown(r["website"]),
        "present_in": r["present_in"],
        "confidence": common.unknown(r["confidence"]),
        "confidence_components": {
            "trust": common.unknown(r["conf_trust"]),
            "fresh": common.unknown(r["conf_fresh"]),
            "complete": common.unknown(r["conf_complete"]),
            "agree": common.unknown(r["conf_agree"]),
        },
        "flags": r["flags"],
        "source_id": r["source_id"],
        "run_id": r["run_id"],
        "ingested_at": r["ingested_at"],
        "observed_at": common.unknown(r["observed_at"]),  # source vintage
        "attribution": ATTRIBUTIONS,
        # Gate 6. Separate from `confidence` on purpose: confidence scores the
        # RECORD, liveness scores the SUBJECT. A well-sourced, well-formed row
        # about a cafe that shut in 2021 is a high-confidence row about a place
        # that is not there.
        **liveness_filter.props(r),
    }
    # §6: def is rendered ONLY when inferred, and always with its marker.
    # Absent = unknown (never "no") — the column CHECK admits nothing else.
    if r["def"] == "inferred":
        props["def"] = "inferred"
        props["def_note"] = _DEF_NOTE
    if include_props is not None:
        props["sources"] = include_props  # per-source blobs (reversibility)
    return common.feature(r["business_id"], r["gj"], props)


@router.get("/v1/places")
def list_places(
    bbox: str,
    category: str | None = Query(
        default=None, description="one taxonomy slug, e.g. truck_stop"),
    q: str | None = Query(
        default=None, min_length=2,
        description="name/brand/city search (FTS + trigram fallback)"),
    min_confidence: int | None = Query(
        default=None, ge=0, le=100,
        description="drop rows scored below this (excludes unscored rows)"),
    include_closed: bool = Query(
        default=False,
        description="include places a source asserted CLOSED (default: hidden)"),
    min_liveness: int | None = Query(
        default=None, ge=0, le=100,
        description="drop rows scored below this (also drops unscored rows)"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    box = common.parse_bbox(bbox)
    where = ["ST_Intersects(b.geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))"]
    params: list = [*box]
    filter_notes: list[str] = []
    order = "b.business_id"

    # Gate 6 first, so the closed-row exclusion is stated in filter_notes even
    # when the caller passes no other filter at all.
    live_where, live_params, live_notes = liveness_filter.where(
        "b", include_closed=include_closed, min_liveness=min_liveness)
    where.extend(live_where)
    params.extend(live_params)
    filter_notes.extend(live_notes)

    if category is not None:
        if category not in CATEGORY_SLUGS:
            raise common.ApiError(
                "invalid_param",
                f"category must be one of {sorted(CATEGORY_SLUGS)}",
            )
        where.append("b.category = %s")
        params.append(category)
    if q is not None:
        # FTS on the generated tsvector (name+brand+city), OR pg_trgm
        # similarity fallback on name for partial/misspelled queries — both
        # index-backed (businesses_tsv_gix / businesses_name_trgm).
        where.append(
            "(b.search_tsv @@ plainto_tsquery('english', %s) OR b.name %% %s)"
        )
        params.extend([q, q])
        order = "similarity(b.name, %s) DESC, b.business_id"
        filter_notes.append(
            "q= matches full-text (name/brand/city) or fuzzy name similarity; "
            "results are ordered by name similarity")
    if min_confidence is not None:
        where.append("b.confidence >= %s")
        params.append(min_confidence)
        filter_notes.append(
            "min_confidence excludes rows with NULL (unscored) confidence")

    order_params = [q] if q is not None else []
    rows = common.q_all(
        f"""
        {_SELECT}
        FROM core.businesses AS b
        WHERE {' AND '.join(where)}
        ORDER BY {order}
        LIMIT %s OFFSET %s
        """,
        [*params, *order_params, limit, offset],
    )
    return common.feature_collection(
        [_feature(r) for r in rows],
        note=f"{_NOTE} {liveness_filter.note()}",
        filter_notes=filter_notes,
        attribution=ATTRIBUTIONS,
        limit=limit,
        offset=offset,
    )


@router.get("/v1/places/{business_id}")
def get_place(business_id: str) -> dict:
    rows = common.q_all(
        f"{_SELECT}, b.props FROM core.businesses AS b WHERE b.business_id = %s",
        [business_id],
    )
    if not rows:
        raise common.ApiError(
            "not_found", f"no business {business_id!r}", status=404)
    r = rows[0]
    # Detail view exposes the per-source blobs (props.overture / props.fsq) —
    # the reversible-merge evidence (quality-ai.md §3.2).
    feature = _feature(r, include_props=r["props"])
    feature["attribution"] = ATTRIBUTIONS
    return feature
