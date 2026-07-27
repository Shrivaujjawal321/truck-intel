"""Environment loading. Reads .env once with a tiny parser — no extra dependency.

.env is looked up relative to the current working directory (run tools from the
repo root, which the Makefile does).
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_DATABASE_URL = "postgresql://truckintel:truckintel_dev@localhost:5432/truckintel"
_dotenv_loaded = False


def load_dotenv(path: str | Path = ".env") -> None:
    """Load KEY=VALUE lines from .env into os.environ. Real env vars win.

    Ignores blank lines, comments, and lines without '='. Runs at most once.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def database_url() -> str:
    load_dotenv()
    return os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)


def track_database_url() -> tuple[str, bool]:
    """DSN for the tracking-ingest write path, and whether it is the narrow role.

    Returns (dsn, is_narrow). `TRACK_DATABASE_URL` should point at the
    `truckintel_track` login created by sql/schema_tracking.sql, which can INSERT
    pings and update three columns of core.truck_devices — and nothing else.

    If it is unset we fall back to DATABASE_URL so a fresh checkout still works,
    but `is_narrow` comes back False and /v1/health reports the posture. A
    silently-privileged write path that *looks* sandboxed is worse than an
    obviously privileged one.
    """
    load_dotenv()
    dsn = os.environ.get("TRACK_DATABASE_URL")
    if dsn:
        return dsn, True
    return database_url(), False


def eia_api_key() -> str | None:
    """EIA free API key. None (or empty) -> the eia_diesel connector must record
    an ops.source_runs row with status='skipped_no_key' — never crash."""
    load_dotenv()
    return os.environ.get("EIA_API_KEY") or None


def contact_email() -> str:
    """Contact for User-Agent headers (politeness contract; NWS requires it).

    SET CONTACT_EMAIL IN YOUR .env. The fallback is deliberately a non-routable
    placeholder rather than a real address: this repo is public, and baking a
    maintainer's inbox into the default would both publish it to scrapers and
    mean every fork silently sends someone else's contact to NWS.

    The placeholder is visible, not silent — an operator who never configures
    it can see exactly that in the User-Agent the servers receive, which is the
    point. A fake-but-plausible address would be worse than an obvious one.
    """
    load_dotenv()
    return os.environ.get("CONTACT_EMAIL", "CONTACT_EMAIL-unset@example.invalid")


def user_agent() -> str:
    """Descriptive User-Agent sent on every outbound request."""
    return f"truckintel/0.1 (+{contact_email()})"


def raw_dir() -> Path:
    """Immutable raw-download zone (sha256-named files, gitignored)."""
    return Path(os.environ.get("TRUCKINTEL_RAW_DIR", "data/raw"))
