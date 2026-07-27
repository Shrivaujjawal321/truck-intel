"""Routing + corridor tests — truck-only guarantees, honest refusals, real geography.

Same two-layer contract as tests/test_api.py: pure-logic tests always run;
graph-backed tests skip when PostGIS or the route graph is unavailable. No fake
rows, and no fixture road network — the assertions below are checked against the
real NTAD National Network.

Run: uv run pytest tests/test_routing.py
"""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from api import common
from api.main import app
from api.routes_route import _point
from truckintel import corridor as corridor_mod
from truckintel.routing import NoTruckPath, get_graph, haversine_m

# Real city centres. Distances are the published road distances a driver would
# recognise, with a tolerance wide enough for truck-network-only detours.
DALLAS = (-96.797, 32.777)
OKC = (-97.517, 35.467)
CHICAGO = (-87.629, 41.878)
INDIANAPOLIS = (-86.158, 39.768)
MID_ATLANTIC = (-40.0, 35.0)   # open ocean


def _get(path: str):
    async def go():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(go())


def _graph_available() -> bool:
    try:
        with common.connect_ro() as conn:
            row = conn.execute("SELECT to_regclass('route.edges') AS t").fetchone()
            if row["t"] is None:
                return False
            row = conn.execute(
                "SELECT to_regclass('route.mainland_edges') AS t"
            ).fetchone()
            return row["t"] is not None
    except Exception:
        return False


needs_graph = pytest.mark.skipif(not _graph_available(), reason="route graph not built")


# --- pure logic --------------------------------------------------------------


def test_haversine_matches_a_known_distance():
    # Dallas -> OKC great circle is ~captured by any correct implementation.
    d = haversine_m(*DALLAS, *OKC) / 1609.344
    assert 180 < d < 210


def test_point_parsing_rejects_out_of_range_values():
    """A swapped pair is caught whenever it puts an impossible value in the
    latitude slot — which is the common case, since most US longitudes are
    outside [-90, 90]."""
    with pytest.raises(common.ApiError) as exc:
        _point("32.777,-96.797", "from")   # lat,lon by mistake: -96.797 is no latitude
    assert exc.value.code == "invalid_param"
    assert "lon,lat" in str(exc.value)

    with pytest.raises(common.ApiError) as exc2:
        _point("200.0,100.0", "from")
    assert exc2.value.code == "invalid_param"


def test_point_parsing_accepts_a_valid_pair():
    assert _point("-96.797,32.777", "from") == (-96.797, 32.777)


def test_point_parsing_requires_two_numbers():
    for bad in ("1", "a,b", "1,2,3"):
        with pytest.raises(common.ApiError):
            _point(bad, "from")


def test_feet_inches_formatting_is_what_a_driver_reads():
    assert corridor_mod._ft_in(162) == "13'6\""
    assert corridor_mod._ft_in(159) == "13'3\""
    assert corridor_mod._ft_in(None) == "unknown"


def test_legal_height_default_is_13ft6():
    assert corridor_mod.LEGAL_HEIGHT_IN == 162


def test_posting_codes_exclude_the_open_code():
    """NBI 'A' means open with no restriction — counting it would make every
    bridge on every route a finding."""
    assert "A" not in corridor_mod.POSTING_MEANING
    assert corridor_mod.POSTING_MEANING["K"] == "closed to all traffic"


def test_restriction_buffer_is_much_tighter_than_service_buffer():
    """A low bridge matters only ON the road; a repair shop matters if reachable."""
    assert corridor_mod.RESTRICTION_BUFFER_M < corridor_mod.SERVICE_BUFFER_M / 10


# --- which structure is ours, and which limit binds us --------------------


@pytest.mark.parametrize("facility,expected", [
    ("I-35", True),
    ("I-35 SB", True),
    ("I-35 NB/ I-40 EB", True),
    ("IH 35", True),
    ("I-35 SB TO I-40 EB", True),
    ("CO. RD. E2130", False),      # the real false positive this fixed
    ("HUBBARD ROAD", False),
    ("19TH ST. (FAU9260)", False),
    ("S.H. 9 E", False),
    ("I-355", False),              # a different interstate, not I-35
    ("", None),
    (None, None),
])
def test_facility_carried_decides_whether_a_structure_is_ours(facility, expected):
    assert corridor_mod._carries_our_route(facility, "I", "35") is expected


def test_us_and_state_route_prefixes_are_recognised():
    assert corridor_mod._carries_our_route("US 81", "U", "81") is True
    assert corridor_mod._carries_our_route("S.H. 9", "S", "9") is True
    assert corridor_mod._carries_our_route("FM 3002", "F", "3002") is True
    assert corridor_mod._carries_our_route("US 81", "I", "81") is False


def test_unlimited_clearance_sentinel_is_not_a_low_bridge():
    """NBI codes open sky above a deck as 99.99 m. Treating it as a number would
    make every open bridge in the country a clearance hazard."""
    assert corridor_mod._clearance_in("99.99") is None
    assert corridor_mod._clearance_in("0") is None
    assert corridor_mod._clearance_in("") is None
    assert corridor_mod._clearance_in("junk") is None
    assert corridor_mod._clearance_in("4.11") == pytest.approx(161.8, abs=0.2)


def test_the_binding_clearance_depends_on_which_side_you_are_on():
    """'I-35 / 4438C UNDER' has open sky above the deck and 13'3" beneath it.
    A truck driving on I-35 never meets that 13'3"."""
    bridge = {"_clearances": {
        "MIN_VERT_CLR_010": "99.99",      # unlimited over the deck
        "VERT_CLR_OVER_MT_053": "99.99",
        "VERT_CLR_UND_054B": "4.03",      # ~13'3" underneath
    }}
    assert corridor_mod._clearance_for(bridge, True) is None        # we drive over it
    under = corridor_mod._clearance_for(bridge, False)              # we pass beneath
    assert under is not None and under < corridor_mod.LEGAL_HEIGHT_IN
    # Relation unknown -> the conservative value, never a silent pick.
    assert corridor_mod._clearance_for(bridge, None) == under


def test_over_deck_clearance_binds_when_we_drive_on_it():
    bridge = {"_clearances": {
        "MIN_VERT_CLR_010": "4.03",       # a low overhead on the deck itself
        "VERT_CLR_OVER_MT_053": "99.99",
        "VERT_CLR_UND_054B": "99.99",
    }}
    over = corridor_mod._clearance_for(bridge, True)
    assert over is not None and over < corridor_mod.LEGAL_HEIGHT_IN
    assert corridor_mod._clearance_for(bridge, False) is None


# --- graph-backed ------------------------------------------------------------


@needs_graph
def test_graph_contains_only_truck_designated_edges():
    """The whole safety property: no generic road can enter a returned route."""
    rows = common.q_all("SELECT DISTINCT kind FROM route.edges")
    assert {r["kind"] for r in rows} <= {"truck_route", "synthetic_connector"}
    # And the truck edges trace back to core.truck_routes, never osm.ways.
    row = common.q_all(
        """
        SELECT count(*) AS n FROM route.edges e
        WHERE e.kind = 'truck_route'
          AND NOT EXISTS (SELECT 1 FROM core.truck_routes t WHERE t.route_id = e.route_id)
        """
    )[0]
    assert row["n"] == 0


@needs_graph
def test_synthetic_connectors_stay_within_the_declared_gap():
    row = common.q_all(
        "SELECT coalesce(max(length_m), 0) AS m FROM route.edges "
        "WHERE kind = 'synthetic_connector'"
    )[0]
    assert row["m"] <= 50.0


@needs_graph
def test_component_1_is_the_largest():
    rows = common.q_all(
        "SELECT component, count(*) AS n FROM route.node_component "
        "GROUP BY component ORDER BY n DESC LIMIT 2"
    )
    assert rows[0]["component"] == 1
    assert rows[0]["n"] > rows[1]["n"]


@needs_graph
def test_dallas_to_okc_uses_i35_and_matches_the_real_distance():
    """The regression that caught unnoded topology: this came back 312 mi via
    US-81 because the graph could not turn onto I-35."""
    graph = get_graph()
    r = graph.route_between(*DALLAS, *OKC)
    miles = r.distance_m / 1609.344
    assert 190 < miles < 230, f"{miles:.0f} mi — expected ~205"

    rows = common.q_all(
        """
        SELECT coalesce(sign_type, '') || coalesce(sign_num, '') AS ref,
               sum(length_m) AS m
        FROM route.edges WHERE edge_id = ANY(%s) GROUP BY ref ORDER BY m DESC LIMIT 1
        """,
        (r.edge_ids,),
    )
    assert rows[0]["ref"] == "I35"


@needs_graph
def test_route_distance_is_never_shorter_than_the_great_circle():
    graph = get_graph()
    r = graph.route_between(*CHICAGO, *INDIANAPOLIS)
    assert r.distance_m >= haversine_m(*CHICAGO, *INDIANAPOLIS)


@needs_graph
def test_access_distance_is_reported_separately_from_driving_distance():
    graph = get_graph()
    r = graph.route_between(*CHICAGO, *INDIANAPOLIS)
    assert r.access_m > 0                     # city centres are not on a truck route
    assert r.access_m == pytest.approx(
        r.origin.access_m + r.destination.access_m, rel=1e-6
    )


@needs_graph
def test_a_point_in_the_ocean_is_refused_not_snapped():
    graph = get_graph()
    with pytest.raises(Exception) as exc:
        graph.route_between(*MID_ATLANTIC, *DALLAS)
    assert "no truck route" in str(exc.value).lower()


@needs_graph
def test_unconnected_components_raise_rather_than_returning_a_near_miss():
    """If the ends are in different components there is no truck-legal path, and
    saying so is the answer. Verified against a real island in the network."""
    graph = get_graph()
    rows = common.q_all(
        """
        SELECT ST_X(n.geom) AS lon, ST_Y(n.geom) AS lat
        FROM route.nodes n JOIN route.node_component c USING (node_id)
        WHERE c.component = (
            SELECT component FROM route.node_component
            GROUP BY component ORDER BY count(*) DESC OFFSET 1 LIMIT 1
        )
        LIMIT 1
        """
    )
    if not rows:
        pytest.skip("network has only one component")
    island = (rows[0]["lon"], rows[0]["lat"])
    # Snapping is allowed to reach the mainland from anywhere, so ask the graph
    # directly with the island's own snap rather than the connectivity-aware one.
    candidates = graph.snap_candidates(*island, max_access_m=100)
    mainland = graph.snap_candidates(*DALLAS)
    island_only = [s for s in candidates if s.component != 1]
    if not island_only:
        pytest.skip("island node also reachable from the mainland within tolerance")
    with pytest.raises(NoTruckPath):
        graph.route(island_only[0], [m for m in mainland if m.component == 1][0])


# --- endpoint ----------------------------------------------------------------


@needs_graph
def test_route_endpoint_reports_counts_restrictions_and_unknowns():
    r = _get(
        "/v1/route?from=-96.797,32.777&to=-97.517,35.467&include=counts"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["route"]["distance_mi"] > 0
    assert body["route"]["network"].startswith("NTAD")
    for key in ("restrictions", "mechanics", "fuel_stations",
                "truck_parking", "rest_areas", "weigh_stations"):
        assert key in body["counts"]
    # The count is only honest next to what could not be judged.
    assert "bridges_without_clearance" in body["unknowns"]
    assert body["counts"]["bridges_on_route"] >= body["unknowns"]["bridges_without_clearance"]


@needs_graph
def test_route_endpoint_rejects_a_swapped_coordinate_pair():
    r = _get("/v1/route?from=200,100&to=-97.517,35.467")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_param"


@needs_graph
def test_unroutable_request_is_422_with_a_stable_code():
    r = _get("/v1/route?from=-40,35&to=-97.517,35.467")
    assert r.status_code == 422
    assert r.json()["error"]["code"] in {"no_truck_route_nearby", "no_truck_path"}


# --- vehicle profile ---------------------------------------------------------


def test_statutory_limits_match_23_cfr_658():
    from truckintel.routing import (
        FEDERAL_MAX_GROSS_LB, STAA_MAX_WIDTH_IN, STAA_MIN_SEMITRAILER_FT,
    )
    assert STAA_MAX_WIDTH_IN == 102
    assert STAA_MIN_SEMITRAILER_FT == 48
    assert FEDERAL_MAX_GROSS_LB == 80_000


def test_a_legal_truck_raises_no_statutory_warning():
    from truckintel.routing import VehicleProfile
    legal = VehicleProfile(height_in=162, weight_lb=80_000, length_ft=53, width_in=102)
    assert legal.statutory_warnings() == []


def test_oversize_dimensions_are_flagged_as_permit_territory():
    """Length and width are not per-edge data — they are statute. Exceeding them
    is legal only under a permit, which this system cannot plan."""
    from truckintel.routing import VehicleProfile
    assert any("102" in w for w in VehicleProfile(width_in=120).statutory_warnings())
    assert any("permit" in w for w in VehicleProfile(length_ft=70).statutory_warnings())
    assert any("80,000" in w for w in VehicleProfile(weight_lb=105_000).statutory_warnings())


def test_only_height_weight_and_hazmat_constrain_the_search():
    """Length/width cannot exclude an edge — there is no per-edge dataset, and
    pretending otherwise would fake a guarantee."""
    from truckintel.routing import VehicleProfile
    assert VehicleProfile(length_ft=53, width_in=102).constrains_search is False
    assert VehicleProfile(height_in=162).constrains_search is True
    assert VehicleProfile(weight_lb=80_000).constrains_search is True
    assert VehicleProfile(hazmat=True).constrains_search is True


@needs_graph
def test_edge_limits_exist_and_are_sane():
    row = common.q_all("""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE min_clearance_in <= 0) AS bad_clearance,
               count(*) FILTER (WHERE max_weight_lb <= 0) AS bad_weight
        FROM route.edge_limits
    """)[0]
    assert row["n"] > 1000
    assert row["bad_clearance"] == 0
    assert row["bad_weight"] == 0


@needs_graph
def test_unrecorded_clearance_does_not_block_an_edge():
    """74% of NBI bridges record no clearance. Treating unknown as impassable
    would delete most of the network; it is reported instead."""
    from truckintel.routing import VehicleProfile
    graph = get_graph()
    blocked = [e for e, lim in graph.limits.items()
               if lim[0] is None and lim[1] is None and not lim[2] and not lim[3]]
    assert not blocked, "an edge with no recorded limit was stored as blocking"
    # A very tall vehicle must still not be blocked by an unknown-clearance edge.
    tall = VehicleProfile(height_in=250)
    sample = common.q_all("""
        SELECT edge_id FROM route.edge_limits
        WHERE min_clearance_in IS NULL AND max_weight_lb IS NULL
          AND NOT closed AND NOT hazmat_blocked LIMIT 5
    """)
    for r in sample:
        assert graph.blocked_for(r["edge_id"], tall) is None


@needs_graph
def test_a_taller_vehicle_never_gets_a_shorter_route():
    """Adding a constraint can only remove options, so distance is monotonic."""
    from truckintel.routing import VehicleProfile
    graph = get_graph()
    short = graph.route_between(*DALLAS, *OKC, profile=VehicleProfile(height_in=140))
    tall = graph.route_between(*DALLAS, *OKC, profile=VehicleProfile(height_in=180))
    assert tall.distance_m >= short.distance_m - 1.0


@needs_graph
def test_a_constraining_profile_actually_excludes_segments():
    from truckintel.routing import VehicleProfile
    graph = get_graph()
    r = graph.route_between(
        *DALLAS, *OKC, profile=VehicleProfile(height_in=180, weight_lb=105_000)
    )
    assert r.edges_excluded > 0
    assert r.exclusion_examples
    assert r.profile is not None


@needs_graph
def test_the_returned_path_never_contains_an_edge_the_vehicle_may_not_use():
    """The load-bearing guarantee: compliance is a property of the path itself."""
    from truckintel.routing import VehicleProfile
    graph = get_graph()
    profile = VehicleProfile(height_in=180, weight_lb=105_000, hazmat=True)
    r = graph.route_between(*DALLAS, *OKC, profile=profile)
    for edge_id in r.edge_ids:
        assert graph.blocked_for(edge_id, profile) is None, edge_id


@needs_graph
def test_route_endpoint_reports_what_the_profile_changed():
    r = _get(
        "/v1/route?from=-96.797,32.777&to=-97.517,35.467&include=counts"
        "&height_in=180&weight_lb=105000"
    )
    assert r.status_code == 200
    v = r.json()["route"]["vehicle"]
    assert v["constrained_the_search"] is True
    assert v["segments_excluded"] > 0
    assert v["height_text"] == "15'0\""
    assert any("80,000" in n for n in v["statutory_notes"])
    assert "23 CFR 658" in v["length_width_note"]


@needs_graph
def test_sql_and_python_facility_matchers_agree():
    """The same rule is implemented twice — route.carries_route() in SQL for the
    bulk limits build, _carries_our_route() in Python for the per-route report.
    They drifted once (SQL knew no tokens for sign types N/O/R/E/T, so every
    structure on those roads was silently treated as belonging to someone else,
    dropping its weight posting). This asserts they cannot drift again."""
    sign_types = [r["sign_type"] for r in common.q_all(
        "SELECT DISTINCT sign_type FROM route.edges "
        "WHERE kind='truck_route' AND sign_type IS NOT NULL"
    )]
    facilities = ["I-35", "I-35 SB", "US 81", "CO. RD. E2130", "S.H. 9 E",
                  "HUBBARD ROAD", "FM 3002", "N 1", "O 1", "R 1", "E 1", "T 1"]
    mismatches = []
    for st in sign_types:
        for num in ("35", "81", "9", "1", "3002"):
            for fac in facilities:
                sql = common.q_all(
                    "SELECT route.carries_route(%s, %s, %s) AS r", (fac, st, num)
                )[0]["r"]
                py = corridor_mod._carries_our_route(fac, st, num)
                if sql != py:
                    mismatches.append((fac, st, num, sql, py))
    assert not mismatches, f"{len(mismatches)} disagreements, e.g. {mismatches[:3]}"


@needs_graph
def test_every_sign_type_in_the_graph_has_matcher_tokens():
    """A sign type with no tokens can never match, so its weight postings vanish."""
    rows = common.q_all("""
        SELECT DISTINCT sign_type FROM route.edges
        WHERE kind = 'truck_route' AND sign_type IS NOT NULL AND sign_type <> ''
    """)
    unmapped = []
    for r in rows:
        st = r["sign_type"]
        if not st.strip() or not st.isalpha():
            continue          # junk codes in the source data, not real sign types
        tokens = common.q_all(
            "SELECT route.sign_tokens(%s, '1') AS t", (st,)
        )[0]["t"]
        if not tokens:
            unmapped.append(st)
        assert corridor_mod._SIGN_PREFIXES.get(st.upper()), f"python lacks {st!r}"
    assert not unmapped, f"SQL has no tokens for sign types {unmapped}"


@needs_graph
def test_an_unresolvable_structure_relation_is_conservative_about_weight():
    """When NBI records no facility carried, we cannot tell whether we drive on
    the structure. A posted bridge we might be on is a hazard, so the limit is
    applied rather than dropped."""
    row = common.q_all("""
        SELECT count(*) AS n
        FROM route.edges e
        JOIN route.edge_limits l USING (edge_id)
        WHERE (e.sign_type IS NULL OR e.sign_num IS NULL)
          AND l.max_weight_lb IS NOT NULL
    """)[0]
    assert row["n"] > 0, "unnamed edges never inherit a posting — too permissive"
