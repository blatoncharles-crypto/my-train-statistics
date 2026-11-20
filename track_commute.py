#!/usr/bin/env python3
"""Log my commute trips and their delays to data/trips.csv using the iRail API.

Stations, usual trains and travel days come from config/route.json. Run it in
the evening (after the last train). For each expected trip of the day it looks
the train up with the iRail vehicle endpoint and records the arrival delay at
that trip's destination (to_station).

    python3 track_commute.py                  # today
    python3 track_commute.py --date 2026-11-18

Rules for rows that already exist (one row per date + direction):
  - source=manual         -> never touched
  - status=skipped        -> never touched
  - status=planned        -> left alone once it has a delay; filled in if delay_min is empty
  - status=other          -> (re)fetched for the train in that row instead of the usual train
Running it twice for the same day never adds duplicate rows.

Note: iRail only has real-time data for the current day. A trip without real-time
arrival data is skipped (never logged as 0 minutes), so --date only helps for today
or for a day whose data iRail still has.
"""
import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config" / "route.json"
TRIPS_FILE = ROOT / "data" / "trips.csv"
FIELDS = ["date", "direction", "train", "from_station", "to_station", "scheduled_departure",
          "scheduled_arrival", "delay_min", "status", "source", "cancelled"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
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
    """Look up one trip. Returns (fields dict, None) or (None, reason it was skipped)."""
    data = get_vehicle(train, day)
    if data is None:
        return None, f"{train} does not run on {day}"
    stops = data["stops"]["stop"]
    ids = [s["stationinfo"]["id"] for s in stops]
    if from_id not in ids or to_id not in ids[ids.index(from_id) + 1:]:
        return None, f"{train} does not run from {from_id} to {to_id}"
    dep = stops[ids.index(from_id)]
    arr = stops[ids.index(to_id, ids.index(from_id) + 1)]
    cancelled = dep.get("departureCanceled") == "1" or arr.get("arrivalCanceled") == "1"
    if not cancelled and arr.get("arrived") != "1":
        return None, "no real-time arrival data yet (train not arrived, or the date is too far back)"
    return {
        "scheduled_departure": hhmm(dep["scheduledDepartureTime"]),
        "scheduled_arrival": hhmm(arr["scheduledArrivalTime"]),
        "delay_min": "" if cancelled else round(int(arr["arrivalDelay"]) / 60),
        "cancelled": "yes" if cancelled else "no",
    }, None


def read_trips():
    if not TRIPS_FILE.exists():
        return []
    with TRIPS_FILE.open(newline="") as f:
        return list(csv.DictReader(f))


def write_trips(rows):
    rows.sort(key=lambda r: (r["date"], r["scheduled_departure"]))
    TRIPS_FILE.parent.mkdir(exist_ok=True)
    with TRIPS_FILE.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(TZ).date())
    day = parser.parse_args().date
    config = json.loads(CONFIG_FILE.read_text())

    if DAYS[day.weekday()] not in config["travel_days"]:
        print(f"{day} is not a travel day, nothing to do.")
        return

    home, work = config["stations"]["home"]["id"], config["stations"]["work"]["id"]
    legs = {"out": (home, work), "back": (work, home)}
    rows = read_trips()
    by_key = {(r["date"], r["direction"]): r for r in rows}
    changed = False

    for direction, usual in config["trains"].items():
        row = by_key.get((day.isoformat(), direction))
        label = f"{day} {direction}"
        if row:
            if row["source"] == "manual":
                print(f"{label}: manual row, kept as is")
                continue
            if row["status"] == "skipped":
                print(f"{label}: skipped, nothing to fetch")
                continue
            if row["status"] == "planned" and (row["delay_min"] != "" or row["cancelled"] == "yes"):
                print(f"{label}: already logged ({row['train']}, {row['delay_min'] or 'cancelled'})")
                continue
            train = row["train"] if row["status"] == "other" else usual["train"]
            from_id, to_id = row["from_station"], row["to_station"]
        else:
            train = usual["train"]
            from_id, to_id = legs[direction]

        fields, reason = fetch_trip(train, day, from_id, to_id)
        if fields is None:
            print(f"{label}: {train} skipped - {reason}", file=sys.stderr)
            continue
        if not row:
            row = {"date": day.isoformat(), "direction": direction, "from_station": from_id,
                   "to_station": to_id, "status": "planned", "source": "auto"}
            rows.append(row)
            by_key[(day.isoformat(), direction)] = row
            if fields["scheduled_departure"] != usual["departure"]:
                print(f"{label}: note - {train} now departs at {fields['scheduled_departure']}, "
                      f"config says {usual['departure']}", file=sys.stderr)
        row.update(fields, train=train)
        changed = True
        print(f"{label}: {train} {fields['scheduled_departure']}, "
              f"{'cancelled' if fields['cancelled'] == 'yes' else str(fields['delay_min']) + ' min late'}")

    if changed:
        write_trips(rows)


if __name__ == "__main__":
    main()
