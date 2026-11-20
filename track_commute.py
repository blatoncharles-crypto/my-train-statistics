#!/usr/bin/env python3
"""Log my commute trips and their delays to data/trips.csv using the iRail API.

Stations, usual trains and travel days come from config/route.json. Run it in
the evening (after the last train). For each expected trip of the day it looks
the train up with the iRail vehicle endpoint and records the arrival delay at
that trip's destination (to_station).

    python3 track_commute.py                  # today
    python3 track_commute.py --date 2026-11-18

Corrections made on the dashboard land in data/corrections.csv (append-only, the
latest row per date + direction wins). Every run applies them to trips.csv for
every corrected day from CORRECTION_WINDOW_DAYS ago up to tomorrow:
  - skipped -> status=skipped, source=manual (a delay already measured is kept, so
               an undo can bring it back; skipped trips never count anyway)
  - other   -> the chosen train, status=other, source=manual, delay fetched for that train
  - planned -> back to the usual train, status=planned, source=auto, delay fetched again

Rules for existing rows on a day without a correction:
  - source=manual -> never touched
  - status=skipped -> never touched
  - status=planned -> left alone once it has a delay; filled in if delay_min is empty
  - status=other -> (re)fetched for the train in that row
Running it twice gives the same result and never adds duplicate rows.

Note: iRail only has real-time data for the current day. A trip without real-time
arrival data gets no delay (it is never logged as 0 minutes), so a past day can be
marked "skipped" at any time, but "other train" only gets a delay on the day itself.
"""
import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config" / "route.json"
TRIPS_FILE = ROOT / "data" / "trips.csv"
CORRECTIONS_FILE = ROOT / "data" / "corrections.csv"
FIELDS = ["date", "direction", "train", "from_station", "to_station", "scheduled_departure",
          "scheduled_arrival", "delay_min", "status", "source", "cancelled"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
CORRECTION_WINDOW_DAYS = 14   # same window the dashboard allows corrections in
API = "https://api.irail.be/v1/vehicle"
TZ = ZoneInfo("Europe/Brussels")


def get_vehicle(train, day, attempts=4):
    """Return the iRail vehicle data for `train` (e.g. "IC 506") on `day`, or None if it doesn't run."""
    params = {"id": "BE.NMBS." + train.replace(" ", ""), "date": day.strftime("%d%m%y"), "format": "json", "lang": "en"}
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "train-commute-tracker/1.0"})
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except (urllib.error.URLError, json.JSONDecodeError) as e:  # iRail sometimes returns a 502/504
            err = e
        if i == attempts - 1:
            raise err
        print(f"  request failed ({err}), retrying...", file=sys.stderr)
        time.sleep(5 * (i + 1))


def hhmm(ts):
    return datetime.fromtimestamp(int(ts), TZ).strftime("%H:%M")


def fetch_trip(train, day, from_id, to_id):
    """Look up one trip. Returns (info, None) or (None, reason).

    info always has the scheduled times; it has "delay_min"/"cancelled" only when
    iRail has real-time data for the arrival (or the train was cancelled)."""
    data = get_vehicle(train, day)
    if data is None:
        return None, f"{train} does not run on {day}"
    stops = data["stops"]["stop"]
    ids = [s["stationinfo"]["id"] for s in stops]
    if from_id not in ids or to_id not in ids[ids.index(from_id) + 1:]:
        return None, f"{train} does not run from {from_id} to {to_id}"
    dep = stops[ids.index(from_id)]
    arr = stops[ids.index(to_id, ids.index(from_id) + 1)]
    info = {"scheduled_departure": hhmm(dep["scheduledDepartureTime"]),
            "scheduled_arrival": hhmm(arr["scheduledArrivalTime"])}
    if dep.get("departureCanceled") == "1" or arr.get("arrivalCanceled") == "1":
        info.update(delay_min="", cancelled="yes")
    elif arr.get("arrived") == "1":
        info.update(delay_min=str(round(int(arr["arrivalDelay"]) / 60)), cancelled="no")
    return info, None


def has_result(row):
    return row["delay_min"] != "" or row["cancelled"] == "yes"


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def write_trips(rows):
    rows.sort(key=lambda r: (r["date"], r["scheduled_departure"] or "99:99", r["direction"]))
    TRIPS_FILE.parent.mkdir(exist_ok=True)
    with TRIPS_FILE.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def latest_corrections():
    """{(date, direction): correction} with the last row per key (file order = time order)."""
    latest = {}
    for c in read_csv(CORRECTIONS_FILE):
        if c["direction"] in ("out", "back") and c["status"] in ("planned", "skipped", "other"):
            latest[(c["date"], c["direction"])] = c
    return latest


class Day:
    def __init__(self, day, config, rows, by_key):
        self.day, self.config, self.rows, self.by_key = day, config, rows, by_key
        home, work = config["stations"]["home"]["id"], config["stations"]["work"]["id"]
        self.legs = {"out": (home, work), "back": (work, home)}

    def new_row(self, direction):
        from_id, to_id = self.legs[direction]
        row = {"date": self.day.isoformat(), "direction": direction, "train": self.config["trains"][direction]["train"],
               "from_station": from_id, "to_station": to_id,
               "scheduled_departure": self.config["trains"][direction]["departure"], "scheduled_arrival": "",
               "delay_min": "", "status": "planned", "source": "auto", "cancelled": "no"}
        self.rows.append(row)
        self.by_key[(row["date"], direction)] = row
        return row

    def lookup(self, label, train, from_id, to_id):
        info, reason = fetch_trip(train, self.day, from_id, to_id)
        if info is None:
            print(f"{label}: {train} - {reason}", file=sys.stderr)
        elif "delay_min" not in info:
            print(f"{label}: {train} - no real-time arrival data (not arrived yet, or the date is too far back)")
        return info

    def apply_correction(self, direction, corr):
        """Bring the trips.csv row in line with the latest correction for this trip."""
        key, label = (self.day.isoformat(), direction), f"{self.day} {direction}"
        usual = self.config["trains"][direction]["train"]
        row = self.by_key.get(key)
        status = corr["status"]

        if status == "skipped":
            if row and row["status"] == "skipped" and row["source"] == "manual":
                return
            row = row or self.new_row(direction)
            row.update(status="skipped", source="manual")
            print(f"{label}: marked as not travelled")
            return

        if status == "other":
            train = corr["train"].strip()
            if row and row["status"] == "other" and row["source"] == "manual" and row["train"] == train and has_result(row):
                return
            row = row or self.new_row(direction)
            row.update(train=train, status="other", source="manual", delay_min="", cancelled="no")
            info = self.lookup(label, train, row["from_station"], row["to_station"])
            if info:
                row.update(info)
            print(f"{label}: other train {train}, delay {row['delay_min'] or ('cancelled' if row['cancelled'] == 'yes' else 'unknown')}")
            return

        # planned: undo any correction
        if row is None or (row["source"] == "auto" and row["status"] == "planned" and row["train"] == usual):
            return self.auto_update(direction)          # nothing to undo, normal logging
        keep = row["train"] == usual and has_result(row)  # e.g. undoing a "skipped" whose delay was kept
        old_delay, old_cancelled = row["delay_min"], row["cancelled"]
        row.update(train=usual, status="planned", source="auto", delay_min="", cancelled="no")
        info = self.lookup(label, usual, row["from_station"], row["to_station"])
        if info:
            row.update(info)
        if not has_result(row) and keep:
            row.update(delay_min=old_delay, cancelled=old_cancelled)
        print(f"{label}: back to the usual train {usual}, delay {row['delay_min'] or ('cancelled' if row['cancelled'] == 'yes' else 'unknown')}")

    def auto_update(self, direction):
        """Normal logging for a day without a correction (only for the day being logged)."""
        key, label = (self.day.isoformat(), direction), f"{self.day} {direction}"
        usual = self.config["trains"][direction]
        row = self.by_key.get(key)
        if row:
            if row["source"] == "manual":
                print(f"{label}: manual row, kept as is")
                return
            if row["status"] == "skipped":
                print(f"{label}: skipped, nothing to fetch")
                return
            if row["status"] == "planned" and has_result(row):
                print(f"{label}: already logged ({row['train']}, {row['delay_min'] or 'cancelled'})")
                return
            train = row["train"] if row["status"] == "other" else usual["train"]
            from_id, to_id = row["from_station"], row["to_station"]
        else:
            train = usual["train"]
            from_id, to_id = self.legs[direction]

        info = self.lookup(label, train, from_id, to_id)
        if not info or "delay_min" not in info:
            return                                     # never log a trip without a real delay
        row = row or self.new_row(direction)
        row.update(info, train=train)
        if info["scheduled_departure"] != usual["departure"] and row["status"] == "planned":
            print(f"{label}: note - {train} now departs at {info['scheduled_departure']}, "
                  f"config says {usual['departure']}", file=sys.stderr)
        print(f"{label}: {train} {info['scheduled_departure']}, "
              f"{'cancelled' if info['cancelled'] == 'yes' else info['delay_min'] + ' min late'}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(TZ).date())
    run_day = parser.parse_args().date
    config = json.loads(CONFIG_FILE.read_text())
    today = datetime.now(TZ).date()

    rows = read_csv(TRIPS_FILE)
    before = json.dumps(rows, sort_keys=True)
    by_key = {(r["date"], r["direction"]): r for r in rows}
    corrections = latest_corrections()

    first, last = today - timedelta(days=CORRECTION_WINDOW_DAYS), today + timedelta(days=1)
    days = {run_day} | {date.fromisoformat(d) for d, _ in corrections if first.isoformat() <= d <= last.isoformat()}

    for day in sorted(days):
        if DAYS[day.weekday()] not in config["travel_days"]:
            if day == run_day:
                print(f"{day} is not a travel day, nothing to do.")
            continue
        d = Day(day, config, rows, by_key)
        for direction in config["trains"]:
            corr = corrections.get((day.isoformat(), direction))
            if corr:
                d.apply_correction(direction, corr)
            elif day == run_day:
                d.auto_update(direction)

    if json.dumps(rows, sort_keys=True) != before:
        write_trips(rows)
        print("data/trips.csv updated")


if __name__ == "__main__":
    main()
