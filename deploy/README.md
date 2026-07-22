# deploy/ — systemd USER units for truck-intel

Per MASTER_PLAN §3.1-9: Python workers and timers run as systemd **user** units
on the host (PostGIS runs in Docker via `scripts/db_up.sh`).

| Unit | What | Cadence |
|---|---|---|
| `truckintel-tick.service` + `.timer` | registry sync + enqueue due jobs | every 1 min |
| `truckintel-worker.service` | queue worker: fetch → validate → publish | long-running |
| `truckintel-freshness.service` + `.timer` | freshness SLO check, then regenerate `status.html` | every 10 min |
| `truckintel-quality.service` + `.timer` | nightly quality ladder (gates 4-5 + confidence rescore) | daily 03:30 |
| `truckintel-businesses.service` + `.timer` | Overture + FSQ-mirror pull → conflate rebuild of `core.businesses` | monthly, 1st @ 04:30 |
| `truckintel-weekly-digest.service` + `.timer` | 7-day ops rollup → `status_weekly.md` (+ Telegram/ntfy) | Mondays 08:00 |
| `truckintel-api.service` | uvicorn `api.main:app` on 127.0.0.1:8000 | long-running |

## Assumptions baked into the unit files

- Repo lives at `~/Documents/truck-intel` (`WorkingDirectory=%h/Documents/truck-intel`).
- `uv` is at `~/.local/bin/uv`. Edit both paths in every unit if your layout differs.
- The PostGIS container is already running (`make db-up`); units only order
  `After=docker.service`, they do not start the container.
- `.env` in the repo root is read by the app itself (no `EnvironmentFile=` needed).

## Install

```bash
mkdir -p ~/.config/systemd/user
cp deploy/truckintel-*.service deploy/truckintel-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload

# timers (they pull in their .service on each firing)
systemctl --user enable --now truckintel-tick.timer truckintel-freshness.timer \
    truckintel-quality.timer truckintel-businesses.timer truckintel-weekly-digest.timer
# long-running services
systemctl --user enable --now truckintel-worker.service truckintel-api.service
```

To keep everything running after logout / across reboots without a login session:

```bash
loginctl enable-linger "$USER"
```

## Observe

```bash
systemctl --user list-timers 'truckintel-*'
systemctl --user status truckintel-worker
journalctl --user -u truckintel-worker -f       # live worker log
journalctl --user -u truckintel-freshness -e    # last freshness run (violations included)
```

`status.html` is written to the repo root every 10 minutes; open it in a browser.
Note: `truckintel-freshness.service` runs `freshness_check.py` with a leading
`-` on its `ExecStart` line — an SLO violation (exit 1) is logged but does not
stop `status_gen.py`, so the page stays current even while a source is late.

## Uninstall

```bash
systemctl --user disable --now truckintel-tick.timer truckintel-freshness.timer \
    truckintel-worker.service truckintel-api.service
rm ~/.config/systemd/user/truckintel-*.{service,timer}
systemctl --user daemon-reload
```
