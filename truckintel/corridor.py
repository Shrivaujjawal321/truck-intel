"""What is on a route: restrictions, mechanics, fuel, and the rest.

Given the ordered edge list of a computed route, this reports every feature near
it, with how far along the route it sits and how far off the road it is.

Two buffers, because they answer different questions:

* **restriction_buffer_m** (default 150 m) — "will this stop my truck?" A low
  bridge only matters if it is ON the road being driven. A tight buffer is the
  point, not a limitation.
* **service_buffer_m** (default 5 km) — "can I get to it?" A repair shop 3 km off
  the highway is usable. Straight-line, and labelled as such: it is not drive
  distance, and this module never pretends otherwise.

The honesty that matters most here is about what is NOT known. 468,598 of the
629,710 NBI bridges carry no recorded vertical clearance — 74%. A route summary
that says "3 restrictions" while silently ignoring 200 bridges of unknown height
is worse than useless to a driver. So every count is paired with the number of
features that could not be judged, and `unknown` is never rounded down to zero.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from truckintel.db import get_conn

# 13'6" — the height a legal US truck is built to. Below this is a strike risk.
LEGAL_HEIGHT_IN = 162
RESTRICTION_BUFFER_M = 150
SERVICE_BUFFER_M = 5_000

# NBI Item 41, structure open/posted/closed. 'A' is open with no restriction and
# is therefore NOT a finding; everything else here restricts or closes the span.
POSTING_MEANING = {
    "P": "posted for load",
    "K": "closed to all traffic",
    "R": "posted for other limits",
    "B": "posting recommended, not yet legally implemented",
    "D": "open, temporarily shored",
    "E": "open, temporary structure",
    "G": "new structure not yet open to traffic",
}


@dataclass
class Corridor:
    """One analysed route."""

    total_m: float
    restrictions: list[dict]
    services: dict[str, list[dict]]
    counts: dict[str, Any]
    unknowns: dict[str, Any]


def _prepare_path(cur, edge_ids: list[int]) -> float:
    """Materialise the route as an indexed temp table.

    A long route is thousands of edges and the corridor query asks "which path
    edge is nearest" once per feature. Against a CTE that is a nested scan; with
    a real GIST index it is a KNN lookup.
    """
    cur.execute("DROP TABLE IF EXISTS pg_temp.path")
    cur.execute(
        """
        CREATE TEMP TABLE path AS
        SELECT o.seq, e.edge_id, e.geom, e.length_m, e.sign_type, e.sign_num,
               coalesce(sum(e.length_m) OVER (ORDER BY o.seq
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS cum_m
        FROM unnest(%(ids)s::bigint[]) WITH ORDINALITY AS o(edge_id, seq)
        JOIN route.edges e ON e.edge_id = o.edge_id
        """,
        {"ids": edge_ids},
    )
    cur.execute("CREATE INDEX path_gix ON pg_temp.path USING GIST (geom)")
    cur.execute("ANALYZE pg_temp.path")
    cur.execute("SELECT coalesce(sum(length_m), 0) FROM pg_temp.path")
    return float(cur.fetchone()[0])


# Position along the route + perpendicular offset, resolved against the nearest
# path edge. Reused by every feature query below.
_POSITION = """
    LATERAL (
        SELECT p.cum_m + ST_LineLocatePoint(p.geom, ST_ClosestPoint(p.geom, f.geom))
                       * p.length_m                                   AS m_along,
               ST_Distance(p.geom::geography, f.geom::geography)      AS offset_m,
               p.sign_type                                            AS on_sign_type,
               p.sign_num                                             AS on_sign_num
        FROM pg_temp.path p
        ORDER BY p.geom <-> f.geom
        LIMIT 1
    ) pos
"""

# NBI item 7 (FACILITY_CARRIED) names the road ON the structure. Sign types in
# core.truck_routes are single letters; these are the prefixes the same route is
# written with in NBI free text.
# MUST stay identical to route.sign_tokens() in sql/route_limits.sql — the same
# rule is implemented twice (bulk build in SQL, per-route report in Python) and
# they disagreeing is a silent correctness bug. tests/test_routing.py asserts
# the two agree on every sign type present in the graph.
_SIGN_PREFIXES = {
    "I": ("I", "IH", "INTERSTATE"),
    "U": ("US", "USH", "USHWY", "U"),
    "S": ("SH", "SR", "STATE", "S"),
    "C": ("CR", "CORD", "COUNTY", "C"),
    "F": ("FM", "F"),
    "M": ("M",),
    "N": ("N",),
    "O": ("O",),
    "R": ("R",),
    "E": ("E",),
    "T": ("T",),
}


_M_TO_IN = 39.3700787
_CLEARANCE_SENTINEL = 99.99   # NBI code for "unlimited" (open sky above the deck)


def _clearance_in(value: str | None) -> float | None:
    """NBI coded metres -> inches. 99.99, 0 and junk mean 'no limit recorded'."""
    if not value:
        return None
    try:
        metres = float(value)
    except ValueError:
        return None
    if metres <= 0 or metres == _CLEARANCE_SENTINEL:
        return None
    return round(metres * _M_TO_IN, 1)


def _clearance_for(b: dict, on_it: bool | None) -> float | None:
    """The height limit THIS truck meets at this structure.

    `core.bridges.min_vert_clearance_in` is the minimum across NBI items 10, 53
    and 54 — deliberately conservative for the table, but it conflates two
    different limits, and using it here produces false warnings:

      item 10 / 53  clearance OVER the bridge deck   -> binds traffic driving ON it
      item 54B      clearance UNDER the structure    -> binds traffic passing BELOW

    'I-35 / 4438C UNDER' carries I-35 with open sky above (item 10 = 99.99) and
    13'3" beneath it. A truck on I-35 drives over the top and never meets that
    13'3" — it belongs to the road underneath.
    """
    props = b.get("_clearances") or {}
    over = min(
        (v for v in (_clearance_in(props.get("MIN_VERT_CLR_010")),
                     _clearance_in(props.get("VERT_CLR_OVER_MT_053"))) if v is not None),
        default=None,
    )
    under = _clearance_in(props.get("VERT_CLR_UND_054B"))
    if on_it is True:
        return over
    if on_it is False:
        # The deck carries someone else's road. NBI records an underclearance for
        # whatever passes beneath, but the parser keeps only the base record, so
        # we cannot prove the road beneath is OURS rather than a rail line or a
        # side street. Report it — under-reporting a height limit is the more
        # dangerous error — and let the label say what is actually known.
        return under
    # Unknown relation: fall back to the conservative minimum rather than pick a
    # side, and the caller labels it as unverified.
    coded = [v for v in (over, under) if v is not None]
    return min(coded) if coded else None


def _norm(s: str | None) -> str:
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def _carries_our_route(facility: str | None, sign_type: str | None,
                       sign_num: str | None) -> bool | None:
    """Is the road ON this structure the road we are driving?

    Returns True (we drive over it), False (it crosses above us), or None when
    NBI did not record what the structure carries and no honest call is possible.

    This decides whether a load posting applies to us at all. 'CO. RD. E2130 /
    I-35 NB UNDER' is posted for load — but that posting governs the county road
    on top, not the truck passing underneath on I-35.
    """
    if not facility:
        return None
    fac = _norm(facility)
    if not fac or not sign_num:
        return None
    num = _norm(sign_num)
    for prefix in _SIGN_PREFIXES.get((sign_type or "").upper(), ()):
        token = prefix + num
        if fac.startswith(token):
            rest = fac[len(token):]
            # 'I35SB' carries I-35; 'I3512' is a different route, not I-35.
            if not rest or not rest[0].isdigit():
                return True
    return False


# Metres -> degrees, deliberately generous. Used only to widen a bounding box for
# the index probe; the exact geography distance is still applied afterwards, so
# over-estimating costs a few extra candidates and under-estimating loses real
# hits. 70 km per degree is below the narrowest US longitude degree (~73 km at
# the northern border), so the box is never too small.
_M_PER_DEG = 70_000.0


def _near(cur, table: str, id_col: str, columns: str, buffer_m: float) -> list[dict]:
    """Every row of `table` whose geometry is within `buffer_m` of the route.

    Driven from the path, not from the feature table. The other way round —
    scanning the table and testing each row against the path — cannot use the
    table's spatial index and turns 629,710 bridges into a full scan per request
    (measured: >10 min for one route). Here the `&&` against an expanded path
    envelope is an index probe, and the exact geography test runs only on what
    survives it.
    """
    cur.execute(
        f"""
        WITH hits AS (
            SELECT DISTINCT ON (f.{id_col}) f.*
            FROM pg_temp.path p
            JOIN {table} f
              ON f.geom && ST_Expand(p.geom, %(deg)s)
             AND ST_DWithin(f.geom::geography, p.geom::geography, %(buf)s)
        )
        SELECT {columns}, pos.m_along, pos.offset_m,
               pos.on_sign_type, pos.on_sign_num
        FROM hits f, {_POSITION}
        ORDER BY pos.m_along
        """,
        {"buf": buffer_m, "deg": buffer_m / _M_PER_DEG},
    )
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def analyse(
    edge_ids: list[int],
    restriction_buffer_m: float = RESTRICTION_BUFFER_M,
    service_buffer_m: float = SERVICE_BUFFER_M,
    clearance_threshold_in: int = LEGAL_HEIGHT_IN,
) -> Corridor:
    if not edge_ids:
        return Corridor(0.0, [], {}, {}, {})

    with get_conn() as conn:
        with conn.cursor() as cur:
            total_m = _prepare_path(cur, edge_ids)

            bridges = _near(
                cur, "core.bridges", "nbi_id",
                "f.nbi_id AS id, f.name, f.state, f.min_vert_clearance_in, "
                "f.posting_status, f.operating_rating, f.inventory_rating, "
                "f.props->>'FACILITY_CARRIED_007' AS carries, "
                "jsonb_build_object("
                "  'MIN_VERT_CLR_010',    f.props->>'MIN_VERT_CLR_010',"
                "  'VERT_CLR_OVER_MT_053', f.props->>'VERT_CLR_OVER_MT_053',"
                "  'VERT_CLR_UND_054B',   f.props->>'VERT_CLR_UND_054B') AS _clearances, "
                "ST_X(f.geom) AS lon, ST_Y(f.geom) AS lat",
                restriction_buffer_m,
            )
            tunnels = _near(
                cur, "core.tunnels", "tunnel_id",
                "f.tunnel_id AS id, f.name, f.state, f.min_vert_clearance_in, "
                "f.length_ft, f.hazmat_restricted, f.hazmat_codes, "
                "ST_X(f.geom) AS lon, ST_Y(f.geom) AS lat",
                restriction_buffer_m,
            )
            # Detail is JOINED from core.fuel_places, never copied into the OSM
            # table: Overture Places is CDLA-Permissive and osm.* is ODbL, and
            # mixing them would drag permissive data under share-alike.
            fuel = _near(
                cur,
                "(SELECT f.*, "
                "        p.name AS ov_name, p.brand AS ov_brand, p.address, "
                "        p.city, p.zip, p.phone AS ov_phone, "
                "        p.website AS ov_website, p.email, p.socials, "
                "        p.operating_status, "
                "        pr.price_usd_gal AS region_price_usd_gal, "
                "        pr.week_of AS region_price_week, pr.eia_region, "
                "        aaa.price_usd_gal AS state_price_usd_gal, "
                "        aaa.observed_on AS state_price_on, "
                "        aaa.attribution AS state_price_credit, "
                "        st.state AS resolved_state "
                " FROM osm.fuel_stations f "
                " LEFT JOIN core.fuel_places p ON p.place_id = f.ov_place_id "
                " LEFT JOIN core.fuel_station_state st ON st.osm_id = f.osm_id  LEFT JOIN core.fuel_price_by_state pr ON pr.state = st.state  LEFT JOIN LATERAL (SELECT d.price_usd_gal, d.observed_on, d.attribution                     FROM core.fuel_prices_daily d                     WHERE to_regclass('core.fuel_prices_daily') IS NOT NULL                       AND d.state = st.state AND d.product = 'diesel'                     ORDER BY d.observed_on DESC LIMIT 1) aaa ON true) ",
                "osm_id",
                "f.osm_id AS id, coalesce(f.name, f.ov_name) AS name, "
                "coalesce(f.brand, f.ov_brand) AS brand, coalesce(f.state, f.resolved_state) AS state, f.city, "
                "f.address, f.zip, "
                "coalesce(f.props->>'phone', f.props->>'contact:phone', f.ov_phone) AS phone, "
                "coalesce(f.props->>'website', f.props->>'contact:website', f.ov_website) AS website, "
                "f.email, f.socials, f.props->>'opening_hours' AS opening_hours, "
                "f.operating_status, f.has_diesel, f.hgv_access, f.has_def, "
                "f.verification_status, f.verify_confidence, f.independent_sources, "
                "f.region_price_usd_gal, f.region_price_week, f.eia_region, "
                "f.state_price_usd_gal, f.state_price_on, f.state_price_credit, "
                "ST_X(f.geom) AS lon, ST_Y(f.geom) AS lat",
                service_buffer_m,
            )
            parking = _near(
                cur, "core.parking_sites", "site_id",
                "f.site_id AS id, f.name, f.kind, f.state, f.truck_spaces, "
                "ST_X(f.geom) AS lon, ST_Y(f.geom) AS lat",
                service_buffer_m,
            )
            rest_areas = _near(
                cur, "osm.rest_areas", "osm_id",
                "f.osm_id AS id, f.name, f.state, ST_X(f.geom) AS lon, ST_Y(f.geom) AS lat",
                service_buffer_m,
            )
            weigh = _near(
                cur, "osm.weigh_points", "osm_id",
                "f.osm_id AS id, f.name, f.state, ST_X(f.geom) AS lon, ST_Y(f.geom) AS lat",
                service_buffer_m,
            )
            mechanics = _mechanics(cur, service_buffer_m)

    restrictions: list[dict] = []
    clearance_unknown = 0

    overhead_count = 0
    for b in bridges:
        posting = (b["posting_status"] or "").strip()
        on_it = _carries_our_route(b["carries"], b["on_sign_type"], b["on_sign_num"])
        # The limit that binds THIS truck: over-deck clearance if we drive on it,
        # under-clearance if it crosses above us.
        clearance = _clearance_for(b, on_it)
        reasons = []

        if clearance is not None and float(clearance) < clearance_threshold_in:
            if on_it is True:
                reasons.append(
                    f"low clearance {_ft_in(clearance)} over the deck you drive on "
                    f"(below {_ft_in(clearance_threshold_in)})"
                )
            else:
                reasons.append(
                    f"{_ft_in(clearance)} recorded beneath this structure — applies "
                    f"only if your road is the one passing under it"
                )
        # A load posting governs the deck. If this structure carries a different
        # road over ours, its posting is that road's problem, not ours.
        if posting and posting.upper() in POSTING_MEANING:
            if on_it is True:
                reasons.append(POSTING_MEANING[posting.upper()])
            elif on_it is None:
                reasons.append(
                    f"{POSTING_MEANING[posting.upper()]} — unverified whether this "
                    f"structure carries your road (NBI records no facility carried)"
                )
        if on_it is False:
            overhead_count += 1
        if clearance is None:
            clearance_unknown += 1
        if reasons:
            relation = ("you drive over it" if on_it is True
                        else f"carries {b['carries']} — not your road"
                        if on_it is False
                        else "unknown which road it carries")
            restrictions.append({
                "kind": "bridge", "id": b["id"], "name": b["name"], "state": b["state"],
                "reasons": reasons,
                "relation": relation,
                "carries": b["carries"],
                "clearance_in": float(clearance) if clearance is not None else None,
                "clearance_text": _ft_in(clearance) if clearance is not None else "unknown",
                "posting_status": posting or None,
                "m_along": float(b["m_along"]), "offset_m": float(b["offset_m"]),
                "lon": b["lon"], "lat": b["lat"],
            })

    tunnel_clearance_unknown = 0
    for t in tunnels:
        clearance = t["min_vert_clearance_in"]
        reasons = []
        if t["hazmat_restricted"] is True:
            codes = ", ".join(t["hazmat_codes"] or []) or "unspecified"
            reasons.append(f"hazmat restricted ({codes})")
        if clearance is not None and float(clearance) < clearance_threshold_in:
            reasons.append(f"low clearance {_ft_in(clearance)}")
        if clearance is None:
            tunnel_clearance_unknown += 1
        if reasons:
            restrictions.append({
                "kind": "tunnel", "id": t["id"], "name": t["name"], "state": t["state"],
                "reasons": reasons,
                "relation": "you drive through it",
                "clearance_in": float(clearance) if clearance is not None else None,
                "clearance_text": _ft_in(clearance) if clearance is not None else "unknown",
                "length_ft": float(t["length_ft"]) if t["length_ft"] is not None else None,
                "m_along": float(t["m_along"]), "offset_m": float(t["offset_m"]),
                "lon": t["lon"], "lat": t["lat"],
            })

    restrictions.sort(key=lambda r: r["m_along"])
    services = {
        "mechanics": mechanics,
        "fuel_stations": fuel,
        "truck_parking": parking,
        "rest_areas": rest_areas,
        "weigh_stations": weigh,
    }
    counts = {
        "restrictions": len(restrictions),
        "mechanics": len(mechanics),
        "fuel_stations": len(fuel),
        "fuel_stations_verified": sum(
            1 for f in fuel if f.get("verification_status") == "verified"),
        "truck_parking": len(parking),
        "rest_areas": len(rest_areas),
        "weigh_stations": len(weigh),
        "bridges_on_route": len(bridges),
        "tunnels_on_route": len(tunnels),
    }
    counts["bridges_carrying_another_road"] = overhead_count
    counts["bridges_you_drive_over"] = sum(
        1 for b in bridges
        if _carries_our_route(b["carries"], b["on_sign_type"], b["on_sign_num"]) is True
    )
    unknowns = {
        # The number that keeps the restriction count honest.
        "bridges_without_clearance": clearance_unknown,
        "tunnels_without_clearance": tunnel_clearance_unknown,
        "fuel_with_phone": sum(1 for f in fuel if f.get("phone")),
        "fuel_with_address": sum(1 for f in fuel if f.get("address")),
        "fuel_with_opening_hours": sum(1 for f in fuel if f.get("opening_hours")),
        "fuel_price_note": (
            "price shown is the EIA weekly diesel average for the station's "
            "region, not the price at that pump — no free legal source publishes "
            "per-pump prices"
        ),
        "fuel_without_diesel_flag": sum(1 for f in fuel if f["has_diesel"] is None),
        "fuel_without_hgv_flag": sum(1 for f in fuel if f["hgv_access"] is None),
        "fuel_unverified": sum(
            1 for f in fuel if f.get("verification_status") != "verified"),
        "note": (
            "NULL means unknown, never no. Clearance is missing for 74% of NBI "
            "bridges nationally, so the restriction count is what is KNOWN to "
            "restrict — not a guarantee the route is clear. Load postings are "
            "only reported for structures your road actually runs over; a bridge "
            "carrying a county road above you is posted for that road, not yours."
        ),
    }
    return Corridor(total_m, restrictions, services, counts, unknowns)


def _mechanics(cur, buffer_m: float) -> list[dict]:
    """Truck repair / towing / tyre shops near the route.

    Reads core.mechanic_shops when it exists (the enriched, verified national
    pull) and otherwise falls back to the truck-service rows in core.businesses,
    flagging which source answered so a thin result is never mistaken for
    thin coverage on the ground.
    """
    cur.execute("SELECT to_regclass('core.mechanic_shops')")
    has_table = cur.fetchone()[0] is not None
    if has_table:
        cur.execute("SELECT count(*) FROM core.mechanic_shops")
        if cur.fetchone()[0] > 0:
            rows = _near(
                cur, "core.mechanic_shops", "shop_id",
                "f.shop_id AS id, f.name, f.category, f.city, f.state, f.phone, "
                "f.website, f.verification_status, f.confidence, "
                "ST_X(f.geom) AS lon, ST_Y(f.geom) AS lat",
                buffer_m,
            )
            for r in rows:
                r["source"] = "core.mechanic_shops"
            return rows

    rows = _near(
        cur,
        "(SELECT * FROM core.businesses WHERE category IN "
        "('truck_repair','towing','tire_service','truck_dealer')) ",
        "business_id",
        "f.business_id AS id, f.name, f.category, f.city, f.state, f.phone, "
        "f.website, ST_X(f.geom) AS lon, ST_Y(f.geom) AS lat",
        buffer_m,
    )
    for r in rows:
        r["source"] = "core.businesses (fallback — national mechanic pull not loaded)"
        r["verification_status"] = None
        r["confidence"] = None
    return rows


def _ft_in(inches: float | int | None) -> str:
    """162 -> 13'6\". Drivers read feet and inches, not decimal inches."""
    if inches is None:
        return "unknown"
    total = int(round(float(inches)))
    return f"{total // 12}'{total % 12}\""
