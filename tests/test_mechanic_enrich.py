"""Mechanic-layer enrichment tests — what may be called `verified`, and why.

The 2026-07-24 deep dive found the verification score inflated: `n_sources`
never fell below 2, so the source-agreement component awarded full marks to
every shop and separated nothing. These tests pin the corrected rule so it
cannot silently regress back into flattery:

  * the aggregator does not corroborate its own output
  * two pipelines of one organisation are one vote
  * a government registry and OpenStreetMap ARE independent votes
  * an expired licence is not a current attestation

They also pin the two normalisers the licence join depends on — both sides of
that join must be spoken the same way or the match rate silently collapses —
and the CBP denominator's legal-form filter, which triple-counts if dropped.

Run: uv run pytest tests/test_mechanic_enrich.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mechanic_list = _load("mechanic_list")
osm_extract = _load("osm_extract")


# --------------------------------------------------------------- source orgs

@pytest.mark.parametrize("feed,org", [
    ("Overture", "overture"),
    ("Overture-signals", "overture"),       # the double-count that started this
    ("meta", "meta"),
    ("Meta", "meta"),
    ("facebook", "meta"),
    ("Microsoft", "microsoft"),
    ("bing", "microsoft"),
])
def test_source_org_collapses_pipelines_to_owners(feed, org):
    assert mechanic_list.source_org(feed) == org


def test_unknown_feed_keeps_its_own_identity():
    """A new donor is a real extra vote until we learn otherwise — silently
    folding it into an existing org would understate corroboration."""
    assert mechanic_list.source_org("TomTom") == "tomtom"


# ------------------------------------------------------------- independence

def test_aggregator_alone_is_not_corroboration():
    """'Overture' + 'Overture-signals' is one organisation labelling its own
    output twice. It attests to no independent survey of the premises."""
    orgs, n = mechanic_list.independence(["Overture", "Overture-signals"])
    assert orgs == ["overture"]
    assert n == 0


def test_typical_row_has_exactly_one_real_contributor():
    """The measured shape of this dataset: an aggregator label plus ONE
    donor. If this ever returns 2, source agreement has become informative
    and the scoring bands should be revisited."""
    _, n = mechanic_list.independence(["Overture", "Overture-signals", "meta"])
    assert n == 1


def test_two_donors_count_twice():
    """Meta and Microsoft collect separately, so they are two votes."""
    _, n = mechanic_list.independence(["Overture", "meta", "Microsoft"])
    assert n == 2


def test_state_licence_is_an_independent_vote():
    _, base = mechanic_list.independence(["Overture", "meta"])
    orgs, n = mechanic_list.independence(["Overture", "meta"], licence_ok=True)
    assert n == base + 1
    assert "state_licence" in orgs


def test_expired_licence_does_not_corroborate():
    """A lapsed registration says the shop existed, not that it still trades.
    It must not buy the same vote a current one does."""
    _, n = mechanic_list.independence(
        ["Overture", "meta"], licence_ok=True, licence_expired=True)
    assert n == 1


def test_osm_is_an_independent_vote():
    orgs, n = mechanic_list.independence(["Overture", "meta"], osm_id="node/1")
    assert "osm" in orgs
    assert n == 2


def test_outside_votes_are_what_reach_the_verified_bar():
    """`verified` requires n_independent >= 2, and a plain row scores 1. So
    only the licence/OSM joins can lift a shop into it — which is exactly why
    those two stages exist."""
    _, plain = mechanic_list.independence(["Overture", "Overture-signals", "meta"])
    _, joined = mechanic_list.independence(
        ["Overture", "Overture-signals", "meta"], osm_id="way/9")
    assert plain < 2 <= joined


# ---------------------------------------------------------------- normalisers

@pytest.mark.parametrize("a,b", [
    ("A-1 ALL GERMAN CAR CORP.", "A1 All German Car"),
    ("Joe & Sons, Inc.", "JOE AND SONS"),
    ("The Truck Shop LLC", "Truck Shop"),
])
def test_name_norm_agrees_across_registry_and_places_spellings(a, b):
    assert mechanic_list.name_norm(a) == mechanic_list.name_norm(b)


def test_name_norm_returns_none_when_nothing_survives():
    """A NULL key never matches. An empty-string key would match every other
    empty-string key — which is how a join invents corroboration."""
    assert mechanic_list.name_norm("Вакансии") is None
    assert mechanic_list.name_norm("!!!") is None
    assert mechanic_list.name_norm("") is None


@pytest.mark.parametrize("a,b", [
    ("400 W 219 ST", "400 West 219th Street"),
    ("12 Main St Ste 4", "12 MAIN STREET"),
    ("1000 South Broadway Ave", "1000 S BROADWAY AVENUE"),
])
def test_addr_norm_speaks_both_abbreviation_styles(a, b):
    assert mechanic_list.addr_norm(a) == mechanic_list.addr_norm(b)


def test_addr_norm_refuses_po_boxes():
    """NJ lists several facilities at a PO box. Matching two shops because
    they share a mailbox is a false positive dressed as evidence."""
    assert mechanic_list.addr_norm("P.O. BOX 317") is None
    assert mechanic_list.addr_norm("PO Box 12") is None


# ------------------------------------------------------------- OSM classifier

def test_truck_repair_recognised_from_shop_tag():
    assert osm_extract.is_truck_repair({"shop": "truck_repair"})


def test_truck_repair_recognised_from_capability_tags():
    """The capability tags sit on shops whose primary tag is car_repair —
    Overture exposes neither, which is why OSM is worth the pass."""
    assert osm_extract.is_truck_repair(
        {"shop": "car_repair", "service:vehicle:truck_repair": "yes"})
    assert osm_extract.is_truck_repair(
        {"shop": "car_repair", "service:vehicle:trailer_repair": "yes"})


def test_plain_car_repair_is_not_truck_repair():
    assert not osm_extract.is_truck_repair({"shop": "car_repair"})
    assert not osm_extract.is_truck_repair(
        {"shop": "car_repair", "service:vehicle:truck_repair": "no"})


def test_repair_is_additive_not_exclusive():
    """A truck stop can be BOTH a fuel station and a repair shop. Folding
    repair into classify() would move those sites OUT of osm.fuel_stations
    and silently shrink a working layer."""
    tags = {"amenity": "fuel", "shop": "truck_repair"}
    assert osm_extract.classify(tags) == "fuel"
    assert osm_extract.is_truck_repair(tags)


def test_repair_row_carries_tristate_capabilities():
    row = osm_extract._row("repair", "node/1", {"shop": "truck_repair"},
                           40.0, -75.0, None)
    assert row["truck_repair"] is True
    # Says nothing about trailers -> unknown, never False.
    assert row["trailer_repair"] is None


# ---------------------------------------------------------- CBP denominator

_CBP_HEADER = "fipstate,naics,lfo,est\n"


def test_cbp_counts_only_the_all_legal_forms_row(tmp_path, monkeypatch):
    """The CBP file repeats every (state, naics) once per legal form of
    organisation and once for all of them ('-'). Summing without the filter
    double- or triple-counts the denominator, which would make every state
    look under-covered."""
    txt = tmp_path / "cbp22st.txt"
    txt.write_text(
        _CBP_HEADER
        + "36,811111,-,5346\n"      # all legal forms — the one we want
        + "36,811111,C,3000\n"      # corporations
        + "36,811111,S,2346\n"      # S-corps
        + "46,811111,-,300\n"
        + "36,811310,-,900\n"
        + "36,811121,-,9999\n"      # body shops — not our NAICS
    )
    monkeypatch.setattr(mechanic_list, "CBP_DIR", tmp_path)
    est = mechanic_list._cbp_estabs()
    assert est["NY"]["811111"] == 5346
    assert est["NY"]["811310"] == 900
    assert est["SD"]["811111"] == 300
    assert "811121" not in est["NY"]


def test_cbp_maps_fips_to_usps(tmp_path, monkeypatch):
    txt = tmp_path / "cbp22st.txt"
    txt.write_text(_CBP_HEADER + "06,811111,-,9705\n" + "72,811111,-,500\n")
    monkeypatch.setattr(mechanic_list, "CBP_DIR", tmp_path)
    est = mechanic_list._cbp_estabs()
    assert est["CA"]["811111"] == 9705
    # Puerto Rico (72) has no NTAD truck routes and no USPS entry in the map;
    # it is dropped rather than guessed at.
    assert len(est) == 1
