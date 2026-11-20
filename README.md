# my-train-statistics

Logs the delays of my daily train commute using the [iRail API](https://docs.irail.be/), and shows them on a dashboard.

## Files

- `config/route.json` – the route: home and work station (iRail ID + display name), the usual train per direction (`out` = home → work, `back` = work → home) and the travel days. Change trains or stations here.
- `data/trips.csv` – one row per trip:

  | column | meaning |
  |---|---|
  | `date` | travel day (YYYY-MM-DD) |
  | `direction` | `out` or `back` |
  | `train` | e.g. `IC 506` |
  | `from_station`, `to_station` | iRail station IDs (names are in the config) |
  | `scheduled_departure`, `scheduled_arrival` | HH:MM, Belgian time |
  | `delay_min` | delay on arrival at `to_station` (negative = early; empty if cancelled) |
  | `status` | `planned` (usual train), `skipped` (didn't travel) or `other` (took another train) |
  | `source` | `auto` (filled by the script) or `manual` (hand correction, never overwritten) |
  | `cancelled` | `yes` / `no` |

- `track_commute.py` – creates the day's expected trips from the config and fills in the delay via the iRail vehicle endpoint. Python standard library only. Re-running never adds duplicates. Rows with `source=manual` or `status=skipped` are left alone; for `status=other` it fetches the train named in that row.
- `.github/workflows/track-commute.yml` – runs the script every weekday evening (19:30 UTC) and commits `data/trips.csv`. Can also be started by hand from the Actions tab.
- `index.html` + `manifest.json` – the dashboard (plain HTML + JavaScript), installable on a phone's home screen. Only trips with status `planned` or `other` count.

To log a specific day: `python3 track_commute.py --date 2026-11-20`. iRail only keeps real-time data for the current day, so this only works for today (or a day iRail still has data for); a trip without real-time data is skipped, never logged as 0 minutes.

## Dashboard

Served with GitHub Pages from `main` / root, so it updates every time the workflow commits new data. To view it locally: `python3 -m http.server` and open http://localhost:8000.
