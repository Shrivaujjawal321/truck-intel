"""Fuel-station verification tests — what 'verified' is allowed to mean.

The point of this job is a claim about the real world: a station is there. These
tests pin the two things that make that claim honest — the corroborating source
must be independent of OpenStreetMap, and nothing may be deleted for failing.

Run: uv run pytest tests/test_fuel_verify.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from api import common

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "fuel_verify", REPO / "scripts" / "fuel_verify.py"
)
fuel_verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fuel_verify)


def _verified_ran() -> bool:
    try:
        with common.connect_ro() as conn:
            row = conn.execute("""
                SELECT count(*) AS n FROM information_schema.columns
                WHERE table_schema = 'osm' AND table_name = 'fuel_stations'
                  AND column_name = 'verification_status'
            """).fetchone()
            if not row["n"]:
                return False
            row = conn.execute(
                "SELECT count(*) AS n FROM osm.fuel_stations "
                "WHERE verification_status IS NOT NULL"
            ).fetchone()
            return row["n"] > 0
    except Exception:
        return False


needs_verified = pytest.mark.skipif(
    not _verified_ran(), reason="fuel verification has not been run"
)


# --- the design rules --------------------------------------------------------


def test_overtures_own_pipelines_are_not_counted_as_independent():
    """`Overture` and `Overture-signals` are one organisation. Counting them as
    two corroborations is the exact error that made every mechanic score 94."""
    assert set(fuel_verify.OVERTURE_OWN) == {"Overture", "Overture-signals"}


def test_outside_datasets_are_not_excluded():
    """Meta / Microsoft / Foursquare / AllThePlaces / DAC are separate operations
    and must remain eligible as evidence."""
    for dataset in ("meta", "Microsoft", "Foursquare", "AllThePlaces", "DAC"):
        assert dataset not in fuel_verify.OVERTURE_OWN


def test_match_radius_covers_a_forecourt_but_not_the_next_station():
    assert 50 <= fuel_verify.MATCH_M <= 250


def test_fuel_categories_include_the_truck_specific_ones():
    assert "truck_gas_station" in fuel_verify.FUEL_CATS
    assert "truck_stop" in fuel_verify.FUEL_CATS
    assert "gas_station" in fuel_verify.FUEL_CATS


# --- the loaded result -------------------------------------------------------


@needs_verified
def test_nothing_was_deleted_for_failing_verification():
    """'We could not confirm this' is not 'it is not there'. Every row survives."""
    row = common.q_all("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE verification_status IS NULL) AS unlabelled
        FROM osm.fuel_stations
    """)[0]
    assert row["total"] > 100_000
    assert row["unlabelled"] == 0


@needs_verified
def test_every_verified_station_has_independent_corroboration():
    """The load-bearing invariant: 'verified' is never awarded on OSM's own say-so."""
    row = common.q_all("""
        SELECT count(*) AS n FROM osm.fuel_stations
        WHERE verification_status = 'verified'
          AND coalesce(independent_sources, 0) = 0
    """)[0]
    assert row["n"] == 0


@needs_verified
def test_verified_stations_are_never_at_an_impossible_coordinate():
    row = common.q_all("""
        SELECT count(*) AS n FROM osm.fuel_stations
        WHERE verification_status = 'verified' AND coord_ok IS NOT TRUE
    """)[0]
    assert row["n"] == 0


@needs_verified
def test_matched_stations_are_within_the_declared_radius():
    row = common.q_all(
        "SELECT coalesce(max(ov_match_m), 0) AS m FROM osm.fuel_stations"
    )[0]
    assert row["m"] <= fuel_verify.MATCH_M


@needs_verified
def test_all_three_bands_are_used_and_confidence_orders_them():
    rows = common.q_all("""
        SELECT verification_status, count(*) AS n, avg(verify_confidence) AS conf
        FROM osm.fuel_stations GROUP BY 1
    """)
    by = {r["verification_status"]: r for r in rows}
    assert set(by) <= {"verified", "probable", "unverified"}
    assert by.get("verified", {}).get("n", 0) > 0
    if "probable" in by and "unverified" in by:
        assert float(by["probable"]["conf"]) > float(by["unverified"]["conf"])


@needs_verified
def test_independent_source_names_never_include_an_overture_pipeline_alone():
    """A row credited with independent sources must name a real outside dataset."""
    rows = common.q_all("""
        SELECT ov_datasets FROM osm.fuel_stations
        WHERE independent_sources > 0 AND ov_datasets IS NOT NULL LIMIT 500
    """)
    assert rows
    for r in rows:
        outside = [d for d in r["ov_datasets"] if d not in fuel_verify.OVERTURE_OWN]
        assert outside, r["ov_datasets"]


# --- enrichment: full detail, and the price honesty --------------------------


def _enriched() -> bool:
    try:
        with common.connect_ro() as conn:
            return conn.execute(
                "SELECT to_regclass('core.fuel_places') AS t"
            ).fetchone()["t"] is not None
    except Exception:
        return False


needs_enriched = pytest.mark.skipif(
    not _enriched(), reason="fuel enrichment has not been run"
)

_enrich_spec = importlib.util.spec_from_file_location(
    "fuel_enrich", REPO / "scripts" / "fuel_enrich.py"
)
fuel_enrich = importlib.util.module_from_spec(_enrich_spec)
_enrich_spec.loader.exec_module(fuel_enrich)


def test_every_state_maps_to_an_eia_region():
    """A station in an unmapped state would silently show no price at all."""
    fifty = {
        "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
        "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
        "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
        "TX","UT","VT","VA","WA","WV","WI","WY","DC",
    }
    missing = fifty - set(fuel_enrich.STATE_TO_PADD)
    assert not missing, f"no EIA region for {sorted(missing)}"


def test_padd_subdistricts_match_eia_published_membership():
    """Checked against eia.gov/petroleum/weekly/includes/padds.php."""
    m = fuel_enrich.STATE_TO_PADD
    assert all(m[s] == "PADD1A" for s in ("CT","ME","MA","NH","RI","VT"))
    assert all(m[s] == "PADD1B" for s in ("DE","DC","MD","NJ","NY","PA"))
    assert all(m[s] == "PADD1C" for s in ("FL","GA","NC","SC","VA","WV"))
    assert all(m[s] == "PADD3" for s in ("AL","AR","LA","MS","NM","TX"))
    assert all(m[s] == "PADD4" for s in ("CO","ID","MT","UT","WY"))
    # California is reported on its own series, not folded into PADD5.
    assert m["CA"] == "CA"
    assert all(m[s] == "PADD5" for s in ("AK","AZ","HI","NV","OR","WA"))


@needs_enriched
def test_price_is_regional_and_says_so():
    """No free legal source publishes per-pump prices. The stored note must make
    that impossible to misread."""
    rows = common.q_all("SELECT DISTINCT note FROM core.fuel_price_by_state")
    assert rows
    for r in rows:
        assert "not the price at any individual pump" in r["note"]


@needs_enriched
def test_every_priced_state_uses_its_own_region():
    rows = common.q_all("SELECT state, eia_region FROM core.fuel_price_by_state")
    assert len(rows) >= 51
    for r in rows:
        assert fuel_enrich.STATE_TO_PADD[r["state"].strip()] == r["eia_region"]


@needs_enriched
def test_overture_detail_is_not_copied_into_the_odbl_table():
    """core.fuel_places is CDLA-Permissive; osm.fuel_stations is ODbL. The detail
    is joined through ov_place_id, never copied, so the licences stay separate."""
    cols = {r["column_name"] for r in common.q_all("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='osm' AND table_name='fuel_stations'
    """)}
    for leaked in ("address", "city", "zip", "email", "socials", "operating_status"):
        assert leaked not in cols, f"{leaked} was copied into the ODbL table"
    assert "ov_place_id" in cols, "the join key is missing"


@needs_enriched
def test_the_join_actually_adds_detail():
    """Overture fills contact detail far better than the OSM extract; if the join
    is not delivering that, the enrichment is not doing its job."""
    row = common.q_all("""
        SELECT count(*) FILTER (WHERE f.props ? 'phone') AS osm_phone,
               count(*) FILTER (WHERE p.phone IS NOT NULL) AS ov_phone,
               count(*) FILTER (WHERE p.address IS NOT NULL) AS ov_address,
               count(*) AS total
        FROM osm.fuel_stations f
        LEFT JOIN core.fuel_places p ON p.place_id = f.ov_place_id
    """)[0]
    assert row["ov_phone"] > row["osm_phone"] * 3
    assert row["ov_address"] > row["total"] * 0.5


# --- AAA daily prices: opt-in, attributed, windowed -------------------------

_aaa_spec = importlib.util.spec_from_file_location(
    "aaa_prices", REPO / "scripts" / "aaa_prices.py"
)
aaa_prices = importlib.util.module_from_spec(_aaa_spec)
_aaa_spec.loader.exec_module(aaa_prices)


def test_aaa_respects_the_published_crawl_delay():
    """gasprices.aaa.com/robots.txt says Crawl-delay: 10. Measured: two requests
    back to back earn a 403 from their edge; honouring the delay returns 200."""
    assert aaa_prices.CRAWL_DELAY_S >= 10.0


def test_aaa_keeps_a_window_not_an_archive():
    """Their terms list 'archive' among prohibited uses, so retention is capped."""
    assert 1 <= aaa_prices.KEEP_DAYS <= 90
    assert "DELETE FROM core.fuel_prices_daily" in \
        (REPO / "scripts" / "aaa_prices.py").read_text()


def test_aaa_attribution_names_both_aaa_and_opis():
    """Their licence is conditional on attribution, and OPIS owns the data."""
    a = aaa_prices.ATTRIBUTION
    assert "AAA" in a and "Oil Price Information Service" in a


def test_aaa_is_off_unless_explicitly_enabled(monkeypatch, capsys):
    monkeypatch.delenv("AAA_PRICES_ENABLED", raising=False)
    monkeypatch.setattr("sys.argv", ["aaa_prices.py"])
    assert aaa_prices.main() == 0
    out = capsys.readouterr().out
    assert "OFF" in out and "PERSONAL, NON-COMMERCIAL" in out


def test_aaa_parser_reads_the_labelled_diesel_cell_not_a_column_position():
    """Counting columns would silently return petrol if AAA re-orders the table."""
    html = """
    <tr><td><a href="https://gasprices.aaa.com?state=TX">Texas</a></td>
        <td class="regular">$2.7720</td>
        <td class="mid_grade">$3.2430</td>
        <td class="premium">$3.6610</td>
        <td class="diesel">$4.9020</td></tr>
    <tr><td><a href="?state=CA">California</a></td>
        <td class="diesel">$6.8080</td>
        <td class="regular">$4.1050</td></tr>
    """
    got = aaa_prices.parse_state_table(html)
    assert got == {"TX": 4.902, "CA": 6.808}


def test_aaa_parser_skips_rows_without_a_diesel_price():
    html = '<tr><td><a href="?state=TX">Texas</a></td><td class="regular">$2.77</td></tr>'
    assert aaa_prices.parse_state_table(html) == {}


def _aaa_loaded() -> bool:
    try:
        with common.connect_ro() as conn:
            return conn.execute(
                "SELECT to_regclass('core.fuel_prices_daily') AS t"
            ).fetchone()["t"] is not None
    except Exception:
        return False


@pytest.mark.skipif(not _aaa_loaded(), reason="AAA prices not fetched")
def test_every_stored_aaa_row_carries_its_attribution_and_caveat():
    rows = common.q_all("""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE attribution NOT LIKE '%AAA%') AS no_credit,
               count(*) FILTER (WHERE note NOT LIKE '%not the price at any individual pump%')
                   AS no_caveat,
               count(*) FILTER (WHERE observed_on < current_date - 90) AS too_old
        FROM core.fuel_prices_daily
    """)[0]
    assert rows["n"] > 0
    assert rows["no_credit"] == 0
    assert rows["no_caveat"] == 0
    assert rows["too_old"] == 0


def test_aaa_writes_one_audited_run_per_invocation():
    """A silently-dead price job must trip the same freshness alarm as a dead
    federal feed, so every run is recorded under its own source id."""
    src = (REPO / "scripts" / "aaa_prices.py").read_text()
    assert 'SOURCE_ID = "aaa_daily"' in src
    assert "INSERT INTO ops.source_runs" in src
    assert "_finish_run(run_id, \"failed\"" in src, "failures must be recorded too"
    assert aaa_prices.SLO_HOURS <= 48


@pytest.mark.skipif(not _aaa_loaded(), reason="AAA prices not fetched")
def test_the_aaa_run_is_visible_to_the_freshness_check():
    rows = common.q_all("""
        SELECT status, rows_published FROM ops.source_runs
        WHERE source_id = 'aaa_daily' ORDER BY started_at DESC LIMIT 1
    """)
    assert rows, "no audited run row for aaa_daily"
    assert rows[0]["status"] == "success"
    assert rows[0]["rows_published"] >= 50
    seeded = common.q_all(
        "SELECT slo_hours FROM ops.sources WHERE source_id = 'aaa_daily'"
    )
    assert seeded and seeded[0]["slo_hours"] <= 48


def test_a_daily_timer_exists_and_is_opt_in():
    unit = (REPO / "deploy" / "truckintel-aaa-prices.service").read_text()
    timer = (REPO / "deploy" / "truckintel-aaa-prices.timer").read_text()
    assert "AAA_PRICES_ENABLED=1" in unit, "the unit must opt in explicitly"
    assert "OnCalendar=*-*-* " in timer, "must fire daily"
    assert "Persistent=true" in timer, "a missed day must not be skipped silently"
