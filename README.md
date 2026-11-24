# my-train-statistics

Logs the delays of my daily train commute using the [iRail API](https://docs.irail.be/), and shows them on a dashboard.

## Files

- `config/route.json` – the route: home and work station (iRail ID + display name), the usual train per direction (`out` = home → work, `back` = work → home) and the travel days. Change trains or stations here.
- `data/trips.csv` – one row per trip:

  | column | meaning |
  |---|---|
  | `date` | travel day (YYYY-MM-DD) |
  | `direction` | `out` or `back` |
  | `train` | the train actually taken, e.g. `IC 506` |
  | `from_station`, `to_station` | iRail station IDs (names are in the config) |
  | `scheduled_departure`, `scheduled_arrival` | timetable of the train taken, HH:MM Belgian time |
  | `planned_arrival` | scheduled arrival at `to_station` of the train I planned to take: the usual train, or for `other` with reason `choice` the chosen train itself |
  | `actual_arrival` | actual arrival at `to_station` of the train taken |
  | `train_delay_min` | delay of the train taken, at `to_station` |
  | `journey_delay_min` | `actual_arrival` − `planned_arrival` in whole minutes: the NMBS journey delay, used for compensation (negative = earlier train; counted as 0) |
  | `status` | `planned` (usual train), `skipped` (didn't travel; both delays empty) or `other` (took another train) |
  | `reason` | for `other` only: `choice` (I chose another train; the journey delay is that train's own delay) or `disruption` (the usual train was late or cancelled; measured against the usual train). Empty otherwise |
  | `source` | `auto` (filled by the script) or `manual` (set by a dashboard correction or by hand; only a newer correction changes it) |
  | `cancelled` | `true` / `false`: was the usual train cancelled (departure at `from_station`, arrival at `to_station`, or the whole train)? If so and there's no correction, `journey_delay_min` stays empty until you say which train you took |

- `data/corrections.csv` – corrections made on the dashboard: `date, direction, status, train, reason, created_at` (`reason` = `choice` or `disruption` for `other`; a missing reason counts as `choice`). Append-only; the latest row per date + direction wins (choosing "As planned" again undoes a correction). The dashboard only ever writes to this file, never to `trips.csv`.
- `data/connections.json` – timetable for the dashboard's *Other train* picker: per travel day (last 14 days up to the next travel day) and direction, the direct trains from 1 h before to 2 h after the usual departure, as `[train, departure, arrival]`. Written by the evening run (past days are kept, today and the next travel day are refreshed, older days dropped), so the browser never calls iRail itself: iRail answers about 1 in 7 requests with a 504, and the picker needed 6–8 in a row.
- `track_commute.py` – creates the day's expected trips from the config and fills in the delay via the iRail vehicle endpoint. Python standard library only. On every run it also applies the latest correction per trip (last 14 days up to tomorrow) to `trips.csv`: `skipped` → status skipped with both delays empty, `other` → that train with its own delay; the journey delay is that train's own delay for reason `choice`, or measured against the usual train for `disruption`, `planned` → back to the usual train. Re-running never changes the result or adds duplicates. The arrival times are kept when a trip is marked skipped, so undoing it brings the delays back.
- `.github/workflows/track-commute.yml` – runs the script every weekday evening (19:30 UTC) and commits `data/trips.csv`. Can also be started by hand from the Actions tab.
- `index.html` + `manifest.json` – the dashboard (plain HTML + JavaScript), installable on a phone's home screen. Only trips with status `planned` or `other` count. The compensation counter, the monthly/yearly totals and the worst trip use `journey_delay_min`; "average delay per train" uses `train_delay_min` of the usual trains on days they were taken.

iRail only keeps real-time data for the current day, so mark an "other train" on the day itself to get its delay; "didn't travel" can be marked any time.

To log a specific day: `python3 track_commute.py --date 2026-11-20`. iRail only keeps real-time data for the current day, so this only works for today (or a day iRail still has data for); a trip without real-time data is skipped, never logged as 0 minutes.

## Dashboard

Served with GitHub Pages from `main` / root, so it updates every time the workflow commits new data. To view it locally: `python3 -m http.server` and open http://localhost:8000.

## Correcting trips

On the dashboard, the gear icon opens the settings. Once a GitHub token is saved there, you can tap any trip from the last 14 days, today or tomorrow and mark it *As planned*, *Didn't travel* or *Other train*. Without a token the page is read-only, and visitors never see the correction buttons.

The token is kept only in that browser's local storage. It is never committed or sent anywhere except to the GitHub API. Note that on an iPhone the home-screen app and Safari have separate storage, so enter the token in the app you actually use.

### Creating the token

1. On GitHub: profile picture → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token** (direct link: https://github.com/settings/personal-access-tokens/new).
2. **Token name**: e.g. `commute dashboard`. **Expiration**: your choice (e.g. 1 year); when it expires the dashboard tells you and you paste a new one.
3. **Resource owner**: your own account. **Repository access**: **Only select repositories** → `my-train-statistics`.
4. **Permissions** → **Repository permissions** → **Contents**: **Read and write**. Leave everything else on *No access* (GitHub adds *Metadata: Read-only* by itself; that's required and fine).
5. **Generate token** and copy it (it starts with `github_pat_`). Paste it under the gear icon → **Save**. To revoke it later, delete it on the same GitHub page and tap **Remove token** on the dashboard.
