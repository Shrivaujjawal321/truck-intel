"""/v1/route — pickup to drop on the truck-designated network, and what is on it.

One request answers both halves of the question: which way a truck may legally
go, and what it will meet on the way — restrictions first, then services.

The route is searched over `route.edges`, built from `core.truck_routes` (NTAD
National Network). No part of the returned path can be a road that is not
truck-designated, because no such road is in the graph.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Query

from api import common
from truckintel import corridor as corridor_mod
from truckintel.routing import (
    GraphNotBuilt,
    NoCompliantPath,
    NoNearbyRoute,
    NoTruckPath,
    RoutingError,
    VehicleProfile,
    get_graph,
)

router = APIRouter(tags=["route"])

M_PER_MILE = 1609.344


def _point(raw: str, name: str) -> tuple[float, float]:
    """'lon,lat' -> floats. Rejects the classic swapped-argument mistake."""
    parts = raw.split(",")
    if len(parts) != 2:
        raise common.ApiError("invalid_param", f"{name} must be 'lon,lat'")
    try:
        lon, lat = float(parts[0]), float(parts[1])
    except ValueError:
        raise common.ApiError("invalid_param", f"{name} values must be numbers") from None
    if not -180 <= lon <= 180 or not -90 <= lat <= 90:
        raise common.ApiError(
            "invalid_param",
            f"{name}={raw} is out of range — the order is lon,lat (not lat,lon)",
        )
    return lon, lat


def _snap_out(s) -> dict:
    return {
        "requested": {"lon": s.lon, "lat": s.lat},
        "joined_network_at": {"lon": s.snapped_lon, "lat": s.snapped_lat},
        "access_m": round(s.access_m, 1),
        "access_mi": round(s.access_m / M_PER_MILE, 2),
        "component": s.component,
        "edge_id": s.edge_id,
    }


@router.get("/v1/route")
def route(
    from_: str = Query(..., alias="from", description="pickup as 'lon,lat'"),
    to: str = Query(..., description="drop as 'lon,lat'"),
    restriction_buffer_m: float = Query(
        corridor_mod.RESTRICTION_BUFFER_M, ge=10, le=2_000,
        description="how close a restriction must be to count as ON the route",
    ),
    service_buffer_m: float = Query(
        corridor_mod.SERVICE_BUFFER_M, ge=100, le=50_000,
        description="straight-line reach for services — not drive distance",
    ),
    clearance_in: int = Query(
        corridor_mod.LEGAL_HEIGHT_IN, ge=100, le=250,
        description="vehicle height in inches; anything lower is a restriction",
    ),
    include: str = Query(
        "all", pattern="^(all|route|counts)$",
        description="'route' skips corridor analysis, 'counts' omits the item lists",
    ),
    height_in: float | None = Query(
        None, ge=60, le=300,
        description="vehicle height in inches — segments under it are excluded "
                    "from the search, not merely flagged afterwards",
    ),
    weight_lb: float | None = Query(
        None, ge=1_000, le=500_000,
        description="gross weight in pounds — posted structures below it are excluded",
    ),
    length_ft: float | None = Query(
        None, ge=10, le=200,
        description="semitrailer length in feet — checked against 23 CFR 658, "
                    "which the National Network guarantees; not a per-edge lookup",
    ),
    width_in: float | None = Query(
        None, ge=60, le=200,
        description="width in inches — checked against the statutory 102 in",
    ),
    hazmat: bool = Query(
        False, description="carrying hazmat — hazmat-restricted tunnels are excluded",
    ),
) -> dict:
    origin_lon, origin_lat = _point(from_, "from")
    dest_lon, dest_lat = _point(to, "to")

    profile = VehicleProfile(
        height_in=height_in, weight_lb=weight_lb, length_ft=length_ft,
        width_in=width_in, hazmat=hazmat,
    )
    try:
        graph = get_graph()
        result = graph.route_between(
            origin_lon, origin_lat, dest_lon, dest_lat, profile=profile
        )
    except GraphNotBuilt as exc:
        raise common.ApiError(exc.code, str(exc), status=503) from exc
    except (NoNearbyRoute, NoTruckPath, NoCompliantPath) as exc:
        # 422, not 404: the request is well formed, the network genuinely has no
        # truck-legal answer for it. Saying so is the correct result.
        raise common.ApiError(exc.code, str(exc), status=422) from exc
    except RoutingError as exc:
        raise common.ApiError(exc.code, str(exc), status=500) from exc

    geojson = common.q_all(
        """
        SELECT ST_AsGeoJSON(ST_LineMerge(ST_Collect(geom))) AS g
        FROM route.edges WHERE edge_id = ANY(%s)
        """,
        (result.edge_ids,),
    )[0]["g"]

    legs = common.q_all(
        """
        SELECT coalesce(sign_type, '') || coalesce(sign_num, '') AS ref,
               max(route_name) AS name, state, kind,
               sum(length_m) AS length_m, count(*) AS segments
        FROM route.edges WHERE edge_id = ANY(%s)
        GROUP BY ref, state, kind
        ORDER BY sum(length_m) DESC
        """,
        (result.edge_ids,),
    )

    out: dict = {
        "route": {
            "distance_m": round(result.distance_m, 1),
            "distance_mi": round(result.distance_m / M_PER_MILE, 1),
            "access_m": round(result.access_m, 1),
            "access_note": (
                "straight-line distance from the requested points to where they "
                "join the truck network; not included in distance_m"
            ),
            "edge_count": len(result.edge_ids),
            "geometry": json.loads(geojson) if geojson else None,
            "origin": _snap_out(result.origin),
            "destination": _snap_out(result.destination),
            "roads": [
                {
                    "ref": leg["ref"] or "unknown",
                    "name": leg["name"],
                    "state": leg["state"],
                    "kind": leg["kind"],
                    "miles": round(float(leg["length_m"]) / M_PER_MILE, 1),
                    "segments": leg["segments"],
                }
                for leg in legs
            ],
            "synthetic_connectors": {
                "count": result.connector_count,
                "meters": round(result.connector_m, 1),
                "note": (
                    "inferred gap-closures of 50 m or less between a dead end and "
                    "its nearest node — not published NTAD geometry"
                ),
            },
            "network": "NTAD National Network (truck-designated only)",
            "vehicle": {
                "height_in": height_in,
                "height_text": corridor_mod._ft_in(height_in) if height_in else None,
                "weight_lb": weight_lb,
                "length_ft": length_ft,
                "width_in": width_in,
                "hazmat": hazmat,
                "constrained_the_search": profile.constrains_search,
                "segments_excluded": result.edges_excluded,
                "exclusion_reasons": result.exclusion_examples,
                "structures_passed_without_recorded_clearance":
                    result.structures_unknown_clearance,
                "statutory_notes": profile.statutory_warnings(),
                "length_width_note": (
                    "23 CFR 658 bars any state from imposing a width limit other "
                    "than 102 in, or a semitrailer length limit below 48 ft, on the "
                    "National Network — the network this route runs on. Length and "
                    "width are checked against those statutory limits; there is no "
                    "per-edge dataset because the regulation makes them uniform."
                ),
                "clearance_caveat": (
                    "Only recorded limits exclude a segment. 468,598 of 629,710 NBI "
                    "bridges record no clearance, so a compliant route means no KNOWN "
                    "restriction blocks it — not that every structure was measured."
                ),
            },
        }
    }
    if include == "route":
        return out

    analysis = corridor_mod.analyse(
        result.edge_ids,
        restriction_buffer_m=restriction_buffer_m,
        service_buffer_m=service_buffer_m,
        clearance_threshold_in=clearance_in,
    )
    out["counts"] = analysis.counts
    out["unknowns"] = analysis.unknowns
    out["buffers"] = {
        "restriction_buffer_m": restriction_buffer_m,
        "service_buffer_m": service_buffer_m,
        "clearance_threshold_in": clearance_in,
        "clearance_threshold_text": corridor_mod._ft_in(clearance_in),
    }
    if include != "counts":
        out["restrictions"] = analysis.restrictions
        out["services"] = analysis.services
    return out
