"""Render status.html — the honest ops page (MASTER_PLAN §4 Monitoring, §11).

Reads ops.sources + ops.source_runs, plus per-source data vintage from the core
tables. Output is one self-contained file (inline CSS, no external assets).
An empty DB renders an honest empty page; an unreachable DB renders a degraded
banner (and exits 1 so the systemd run is visibly failed).

Run from anywhere: output always lands at <repo root>/status.html.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Template

from truckintel.db import get_conn

OUT_PATH = Path(__file__).resolve().parents[1] / "status.html"
# Both statuses prove the feed is alive (unchanged data re-verified = fresh).
FRESH_STATUSES = ["success", "skipped_unchanged"]
# source_id -> core table holding its published rows (for the vintage line).
VINTAGE_TABLES = {
    "nbi_annual": "core.bridges",
    "ntad_parking": "core.parking_sites",
    "nws_alerts": "core.live_events",
    "eia_diesel": "core.fuel_prices",
}
# Honest per-source caveats (from registry notes). observed_at = when the fact
# was true in the world, never the download date.
HONEST_NOTES = {
    "nbi_annual": "annual FHWA file; clearances converted meters → inches",
    "ntad_parking": "amenities date to the ~2019 Jason's Law survey era, not the download date",
    "nws_alerts": "live feed; observed_at is the alert issue time",
    "eia_diesel": "regional weekly averages — never station-level pump prices",
}

PAGE = Template("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>truck-intel status</title>
<style>
body{font:14px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:64rem;padding:0 1rem;color:#1b2430;background:#f7f8fa}
h1{font-size:1.35rem}table{border-collapse:collapse;width:100%;background:#fff}
th,td{padding:.5rem .6rem;text-align:left;border-bottom:1px solid #dde3ea;vertical-align:top}
th{font-size:.8rem;text-transform:uppercase;letter-spacing:.03em;color:#5a6675}
.dot{display:inline-block;width:.7rem;height:.7rem;border-radius:50%;margin-top:.25rem}
.green{background:#2e9e4f}.yellow{background:#d9a406}.red{background:#cc3b2e}.off{background:#9aa4b0}
.note{color:#5a6675;font-size:.85rem}.err{background:#fdeceb;border:1px solid #cc3b2e;padding:.6rem .8rem}
footer{margin-top:2rem;font-size:.8rem;color:#5a6675}code{background:#eef1f5;padding:0 .3em}
</style></head><body>
<h1>truck-intel — source status</h1>
<p class="note">generated {{ generated_at }} · colour = age of last successful run vs the source's
freshness SLO (green fresh · yellow ≥75% of SLO · red over SLO or never · grey disabled)</p>
{% if error %}<p class="err">DATABASE UNREACHABLE — {{ error }}. Nothing below can be trusted as current.</p>
{% elif not sources %}<p>No sources synced yet (<code>ops.sources</code> is empty).
Run <code>make sync</code>, then <code>make ingest SOURCE=…</code>. This page is honestly empty, not broken.</p>
{% else %}<table><tr><th></th><th>source</th><th>last run</th><th>rows in / pub / rej</th><th>last success</th><th>SLO</th></tr>
{% for s in sources %}<tr><td><span class="dot {{ s.cls }}"></span></td>
<td><b>{{ s.source_id }}</b>{% if not s.enabled %} (disabled){% endif %}<br>
<span class="note">{{ s.name }}</span><br>
<span class="note">vintage: {{ s.vintage }} — {{ s.note }}</span></td>
<td>{% if s.status %}{{ s.status }}<br><span class="note">{{ s.run_at }}</span>
{% if s.message %}<br><span class="note">{{ s.message }}</span>{% endif %}{% else %}never ran{% endif %}</td>
<td>{{ s.rows }}</td><td>{{ s.age }}</td><td>{{ s.slo_hours }} h</td></tr>
{% endfor %}</table>{% endif %}
<footer><p>Attributions: {% for a in attributions %}{{ a }}{{ " · " if not loop.last }}{% else %}(none synced yet){% endfor %}</p>
<p>Advisory data from public sources — not for enforcement; obey posted signs. A NULL/missing value
renders as “unknown”, never as “no”. Every published row carries (source_id, run_id, ingested_at, observed_at).</p>
</footer></body></html>
""", autoescape=True)


def fmt_age(hours: float | None) -> str:
    if hours is None:
        return "never"
    return f"{hours / 24:.1f} d ago" if hours >= 48 else f"{hours:.1f} h ago"


def vintage_line(conn, source_id: str) -> str:
    table = VINTAGE_TABLES.get(source_id)
    if table is None:
        return "no core table mapped"
    row = conn.execute(  # table name from our own dict above, not user input
        f"SELECT count(*), max(observed_at) FROM {table} WHERE source_id = %s", (source_id,)
    ).fetchone()
    if row[0] == 0:
        return "no rows published yet"
    newest = row[1].strftime("%Y-%m-%d") if row[1] else "unknown (observed_at is NULL)"
    return f"{row[0]:,} rows, newest observed_at {newest}"


def collect(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT s.source_id, s.name, s.enabled, s.slo_hours, s.attribution_text,
               r.status, coalesce(r.finished_at, r.started_at), r.rows_in,
               r.rows_published, r.rows_rejected, left(r.message, 160), ok.ok_at
        FROM ops.sources s
        LEFT JOIN LATERAL (SELECT * FROM ops.source_runs
                           WHERE source_id = s.source_id ORDER BY started_at DESC LIMIT 1) r ON TRUE
        LEFT JOIN LATERAL (SELECT coalesce(finished_at, started_at) AS ok_at FROM ops.source_runs
                           WHERE source_id = s.source_id AND status = ANY(%s)
                           ORDER BY started_at DESC LIMIT 1) ok ON TRUE
        ORDER BY s.source_id""", (FRESH_STATUSES,)).fetchall()
    now = datetime.now(timezone.utc)
    out = []
    for (sid, name, enabled, slo, attr, status, run_at, n_in, n_pub, n_rej, msg, ok_at) in rows:
        age_h = (now - ok_at).total_seconds() / 3600 if ok_at else None
        if not enabled:
            cls = "off"
        elif age_h is None or age_h > slo:
            cls = "red"
        elif age_h >= 0.75 * slo:
            cls = "yellow"
        else:
            cls = "green"
        num = lambda v: "—" if v is None else f"{v:,}"  # noqa: E731 — tiny formatter
        out.append({
            "source_id": sid, "name": name, "enabled": enabled, "slo_hours": slo,
            "attribution": attr, "status": status, "message": msg,
            "run_at": run_at.strftime("%Y-%m-%d %H:%M UTC") if run_at else None,
            "rows": f"{num(n_in)} / {num(n_pub)} / {num(n_rej)}" if status else "—",
            "age": fmt_age(age_h), "cls": cls, "note": HONEST_NOTES.get(sid, ""),
            "vintage": vintage_line(conn, sid),
        })
    return out


def main() -> int:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        with get_conn() as conn:
            sources = collect(conn)
        error = None
    except Exception as exc:  # DB down: still publish an honest degraded page
        sources, error = [], f"{type(exc).__name__}: {exc}"
    attributions = sorted({s["attribution"] for s in sources if s["attribution"]})
    OUT_PATH.write_text(PAGE.render(
        generated_at=generated, sources=sources, attributions=attributions, error=error))
    print(f"wrote {OUT_PATH} ({len(sources)} sources{', DB ERROR' if error else ''})")
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
