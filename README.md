# my-train-statistics

Logs the delay of my daily commute (Brussel-Noord → Leuven 08:08, Leuven → Brussel-Noord 17:21, Mon–Fri) to [`commute.csv`](commute.csv), using the [iRail API](https://docs.irail.be/).

- `track_commute.py` – looks up today's trips and appends one row per trip. Python standard library only.
- `.github/workflows/track-commute.yml` – runs the script every weekday evening (19:30 UTC) and commits the CSV. Can also be started by hand from the Actions tab.

`delay_min` is the delay on arrival at the destination (negative = early). Trips that haven't arrived yet are skipped, and re-running never adds duplicates. To change the trains, edit `TRIPS` at the top of the script. To backfill a day: `python3 track_commute.py --date 2026-11-17`.
