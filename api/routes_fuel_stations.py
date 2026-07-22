"""GET /v1/fuel — OSM fuel-station locations (osm.fuel_stations mirror).

ODbL LAW (§3.1-4): this route serves the UNCONFLATED osm-schema mirror at
query time; every feature (and the collection) carries
"© OpenStreetMap contributors".

HONESTY:
- has_diesel / hgv_access / has_def are TRI-STATE and rendered literally as
  true / false / null (null = tag absent in OSM = unknown, never "no").
  Filtering diesel=true or hgv=true therefore EXCLUDES unknown (null)
  stations — the response says so in filter_notes.
- Prices: there is NO free legal station-level price source (research/
  fuel.md §4). With price=true each feature optionally embeds the CURRENT
  EIA weekly REGIONAL diesel average for its state's PADD, labeled
  "kind": "regional_weekly_estimate" — never a pump price. Stations with an
  unknown state get regional_price: null, honestly.

INTEGRATOR NOTE — register in api/main.py (this file never edits main.py):
    from api import routes_fuel_stations          # add to the import block
    ...
    routes_fuel_stations.router,                  # add to the include loop
(Path /v1/fuel does not collide with routes_fuel.py's /v1/fuel/prices.)
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from api import common

router = APIRouter()

OSM_ATTRIBUTION = "© OpenStreetMap contributors"

_NOTE = (
    "OSM community data: locations are strong (~109k US stations), attribute "
    "tags are sparse — has_diesel/hgv_access/has_def are tri-state "
    "(true/false/null; null = untagged = unknown, never 'no'). No station "
    "prices exist here (no free legal source)."
)
_PRICE_NOTE = (
    "EIA weekly regional survey average for the station's state's "
    "PADD region — never a station pump price."
)

# state -> EIA diesel price regions, most specific first (parsers/eia.py
# region labels; research/fuel.md: diesel = US + PADDs + sub-PADDs + CA only).
# Standard EIA PADD district membership:
_PADD_STATES: dict[str, tuple[str, ...]] = {
    "PADD1A": ("CT", "ME", "MA", "NH", "RI", "VT"),           # New England
    "PADD1B": ("DE", "DC", "MD", "NJ", "NY", "PA"),           # Central Atlantic
    "PADD1C": ("FL", "GA", "NC", "SC", "VA", "WV"),           # Lower Atlantic
    "PADD2": ("IL", "IN", "IA", "KS", "KY", "MI", "MN", "MO",
              "NE", "ND", "OH", "OK", "SD", "TN", "WI"),
    "PADD3": ("AL", "AR", "LA", "MS", "NM", "TX"),
    "PADD4": ("CO", "ID", "MT", "UT", "WY"),
    "PADD5": ("AK", "AZ", "CA", "HI", "NV", "OR", "WA"),
}


def _build_state_regions() -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for sub in ("PADD1A", "PADD1B", "PADD1C"):
        for st in _PADD_STATES[sub]:
            out[st] = (sub, "PADD1", "US")
    for padd in ("PADD2", "PADD3", "PADD4"):
        for st in _PADD_STATES[padd]:
            out[st] = (padd, "US")
    for st in _PADD_STATES["PADD5"]:
        # EIA publishes CA on its own and "West Coast less California".
        out[st] = (("CA", "PADD5", "US") if st == "CA"
                   else ("PADD5_EX_CA", "PADD5", "US"))
    return out


#: state -> preferred EIA region lookup order (most specific available wins).
STATE_TO_REGIONS: dict[str, tuple[str, ...]] = _build_state_regions()


def _latest_diesel_prices() -> dict[str, dict]:
    """{region: {region, week_of, price_usd_gal}} — latest week per region."""
    rows = common.q_all(
        """
        SELECT DISTINCT ON (region)
               region, week_of, price_usd_gal::float8 AS price_usd_gal
        FROM core.fuel_prices
        WHERE product = 'diesel'
        ORDER BY region, week_of DESC
        """
    )
    return {r["region"]: r for r in rows}


def _regional_price(state: str | None, prices: dict[str, dict]) -> dict | None:
    """Most specific available EIA region price for a state; None = unknown
    state or no price loaded — never a fabricated number."""
    for region in STATE_TO_REGIONS.get(state or "", ()):
        hit = prices.get(region)
        if hit:
            return {
                "kind": "regional_weekly_estimate",
                "region": hit["region"],
                "week_of": hit["week_of"],
                "price_usd_gal": hit["price_usd_gal"],
                "note": _PRICE_NOTE,
            }
    return None


@router.get("/v1/fuel")
def list_fuel_stations(
    bbox: str,
    diesel: bool = Query(default=False,
                         description="true -> only fuel:diesel=yes stations "
                                     "(excludes unknown/null)"),
    hgv: bool = Query(default=False,
                      description="true -> only hgv=yes/designated stations "
                                  "(excludes unknown/null)"),
    brand: str | None = Query(default=None,
                              description="case-insensitive substring match"),
    price: bool = Query(default=False,
                        description="embed the EIA weekly REGIONAL diesel "
                                    "estimate per station (never pump prices)"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    box = common.parse_bbox(bbox)
    where = ["ST_Intersects(f.geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))"]
    params: list = [*box]
    filter_notes: list[str] = []
    if diesel:
        where.append("f.has_diesel IS TRUE")
        filter_notes.append(
            "diesel=true excludes stations whose fuel:diesel tag is absent "
            "(has_diesel null = unknown, not 'no')")
    if hgv:
        where.append("f.hgv_access IS TRUE")
        filter_notes.append(
            "hgv=true excludes stations whose hgv tag is absent "
            "(hgv_access null = unknown, not 'no')")
    if brand:
        where.append("f.brand ILIKE '%%' || %s || '%%'")
        params.append(brand)

    rows = common.q_all(
        f"""
        SELECT f.osm_id, f.name, f.brand, f.state,
               f.has_diesel, f.hgv_access, f.has_def,
               f.props ->> 'opening_hours' AS opening_hours,
               ST_AsGeoJSON(f.geom) AS gj, f.confidence,
               f.source_id, f.run_id, f.ingested_at, f.observed_at
        FROM osm.fuel_stations AS f
        WHERE {' AND '.join(where)}
        ORDER BY f.osm_id
        LIMIT %s OFFSET %s
        """,
        [*params, limit, offset],
    )
    prices = _latest_diesel_prices() if price else {}

    features = []
    for r in rows:
        props = {
            "osm_id": r["osm_id"],
            "name": common.unknown(r["name"]),
            "brand": common.unknown(r["brand"]),
            "state": common.unknown(r["state"]),
            # tri-state booleans rendered literally: true / false / null
            "has_diesel": r["has_diesel"],
            "hgv_access": r["hgv_access"],
            "has_def": r["has_def"],
            "opening_hours": common.unknown(r["opening_hours"]),
            "confidence": common.unknown(r["confidence"]),
            "source_id": r["source_id"],
            "run_id": r["run_id"],
            "ingested_at": r["ingested_at"],
            "observed_at": common.unknown(r["observed_at"]),  # PBF vintage
            "attribution": OSM_ATTRIBUTION,
        }
        if price:
            props["regional_price"] = _regional_price(r["state"], prices)
        features.append(common.feature(r["osm_id"], r["gj"], props))

    return common.feature_collection(
        features,
        note=_NOTE,
        filter_notes=filter_notes,
        attribution=OSM_ATTRIBUTION,
        limit=limit,
        offset=offset,
    )
