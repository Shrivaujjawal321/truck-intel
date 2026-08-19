"""The raw-zone retention sweep — no DB, no network.

This is the only script in the repo that deletes ingested payloads, so what it
REFUSES to delete matters more than what it removes. Each keep-rule here exists
because losing that particular directory would be silent and unrecoverable:

  * the newest dir per source — an annual feed (nbi_annual,
    ntad_national_network) has exactly one payload, and an age rule alone would
    take it
  * the last successful run's dir — the payload the live data was actually
    built from, so provenance survives the sweep
  * a source whose raw dir holds files rather than dated subdirectories (the
    data/raw/cbp shape) must be left entirely alone

Run: uv run pytest tests/test_raw_prune.py
"""
from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "raw_prune", REPO / "scripts" / "raw_prune.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


raw_prune = _load()


def _day(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


@pytest.fixture
def raw(tmp_path, monkeypatch):
    """An isolated raw tree, with the database stubbed out by default."""
    root = tmp_path / "raw"
    root.mkdir()
    monkeypatch.setattr(raw_prune, "RAW", root)
    monkeypatch.setattr(raw_prune, "last_success_dates", lambda: {})
    return root


def _payload(root: Path, source: str, day: str, name: str = "a.json") -> Path:
    d = root / source / day
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("{}")
    return d


def test_old_payloads_are_removed(raw):
    old = _payload(raw, "wzdx_az", _day(60))
    recent = _payload(raw, "wzdx_az", _day(1))
    raw_prune.main(["--days", "14"])
    assert not old.exists()
    assert recent.exists()


def test_the_newest_dir_is_kept_however_old_it_is(raw):
    """An annual feed has one payload and it is always older than any cutoff."""
    only = _payload(raw, "nbi_annual", _day(400))
    raw_prune.main(["--days", "14"])
    assert only.exists(), "the only payload a source has must never be deleted"


def test_the_last_successful_run_is_kept(raw, monkeypatch):
    old_success = _payload(raw, "eia_diesel", _day(90))
    _payload(raw, "eia_diesel", _day(80))
    newest = _payload(raw, "eia_diesel", _day(70))
    monkeypatch.setattr(raw_prune, "last_success_dates",
                        lambda: {"eia_diesel": {_day(90)}})
    raw_prune.main(["--days", "14"])
    assert old_success.exists(), "the payload the live data was built from"
    assert newest.exists(), "and the newest, by the other rule"
    assert len(list((raw / "eia_diesel").iterdir())) == 2, "the middle one goes"


def test_a_source_with_no_dated_dirs_is_untouched(raw):
    """data/raw/cbp holds two bulk files directly, not dated subdirectories."""
    d = raw / "cbp"
    d.mkdir()
    f = d / "cbp22st.txt"
    f.write_text("bulk")
    raw_prune.main(["--days", "1"])
    assert f.exists()


def test_dry_run_deletes_nothing(raw, capsys):
    old = _payload(raw, "wzdx_ks", _day(60))
    _payload(raw, "wzdx_ks", _day(1))
    raw_prune.main(["--days", "14", "--dry-run"])
    assert old.exists(), "--dry-run must not delete"
    assert "would remove" in capsys.readouterr().out


def test_a_dead_database_makes_it_keep_more_not_less(raw, monkeypatch):
    """A retention job must never become MORE destructive because a dependency
    is down. With last_success unknown, only the newest-dir rule protects — so
    the sweep still runs, and still cannot empty a source."""
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(raw_prune, "last_success_dates", boom)
    only = _payload(raw, "wzdx_mn", _day(90))
    with pytest.raises(RuntimeError):
        raw_prune.main(["--days", "14"])
    assert only.exists()


def test_the_real_helper_survives_an_unreachable_database(monkeypatch):
    """last_success_dates() itself must swallow a DB failure and return {}."""
    monkeypatch.setattr(raw_prune, "get_conn",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no db")))
    assert raw_prune.last_success_dates() == {}
