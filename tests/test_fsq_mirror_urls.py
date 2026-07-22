"""fsq_mirror_parquet_urls — the source.coop explicit-file-list fix.

DuckDB cannot glob (`*`) generic HTTP paths, so the mirror path lists the
bucket XML and hands read_parquet an explicit URL list. These tests pin that
listing logic (no network) — including the no-silent-truncation guard.

Run: uv run pytest tests/test_fsq_mirror_urls.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_pipeline():
    spec = importlib.util.spec_from_file_location(
        "businesses_pipeline", REPO_ROOT / "scripts" / "businesses_pipeline.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bp = _load_pipeline()


class _FakeResp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


_LISTING = """<?xml version="1.0"?>
<ListBucketResult><IsTruncated>false</IsTruncated>
<Contents><Key>fsq-os-places/2025-01-10/places/0.parquet</Key></Contents>
<Contents><Key>fsq-os-places/2025-02-06/places/0.parquet</Key></Contents>
<Contents><Key>fsq-os-places/2025-02-06/places/10.parquet</Key></Contents>
<Contents><Key>fsq-os-places/2025-02-06/places/2.parquet</Key></Contents>
<Contents><Key>fsq-os-places/2025-02-06/places/_SUCCESS</Key></Contents>
</ListBucketResult>"""


def test_urls_filtered_to_release_and_sorted(monkeypatch):
    monkeypatch.setattr(bp.requests, "get", lambda *a, **k: _FakeResp(_LISTING))
    urls = bp.fsq_mirror_parquet_urls("2025-02-06")
    # only the requested release, only .parquet, full https URLs, sorted
    assert urls == [
        "https://data.source.coop/fused/fsq-os-places/2025-02-06/places/0.parquet",
        "https://data.source.coop/fused/fsq-os-places/2025-02-06/places/10.parquet",
        "https://data.source.coop/fused/fsq-os-places/2025-02-06/places/2.parquet",
    ]
    # the other release and the _SUCCESS marker are excluded
    assert not any("2025-01-10" in u or "_SUCCESS" in u for u in urls)


def test_truncated_listing_refuses(monkeypatch):
    truncated = _LISTING.replace(
        "<IsTruncated>false</IsTruncated>", "<IsTruncated>true</IsTruncated>")
    monkeypatch.setattr(bp.requests, "get", lambda *a, **k: _FakeResp(truncated))
    # no silent partial reads — a >1000-key listing must raise, not drop files
    with pytest.raises(RuntimeError, match="truncated"):
        bp.fsq_mirror_parquet_urls("2025-02-06")


def test_no_parquet_for_release_raises(monkeypatch):
    monkeypatch.setattr(bp.requests, "get", lambda *a, **k: _FakeResp(_LISTING))
    with pytest.raises(RuntimeError, match="no parquet for release"):
        bp.fsq_mirror_parquet_urls("2099-01-01")
