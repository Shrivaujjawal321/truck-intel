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


def eia_api_key() -> str | None:
    """EIA free API key. None (or empty) -> the eia_diesel connector must record
    an ops.source_runs row with status='skipped_no_key' — never crash."""
    load_dotenv()
    return os.environ.get("EIA_API_KEY") or None


def contact_email() -> str:
    """Contact for User-Agent headers (politeness contract; NWS requires it)."""
    load_dotenv()
    return os.environ.get("CONTACT_EMAIL", "shriva.ujjawal@gmail.com")


def user_agent() -> str:
    """Descriptive User-Agent sent on every outbound request."""
    return f"truckintel/0.1 (+{contact_email()})"


def raw_dir() -> Path:
    """Immutable raw-download zone (sha256-named files, gitignored)."""
    return Path(os.environ.get("TRUCKINTEL_RAW_DIR", "data/raw"))
