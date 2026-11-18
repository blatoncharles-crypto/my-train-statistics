#!/usr/bin/env python3
"""Log the delay of my daily commute trains to commute.csv using the iRail API.

Run it in the evening (after the last train). It looks up today's trips and
appends one row per trip. Running it twice on the same day adds nothing new.

    python3 track_commute.py                  # today
    python3 track_commute.py --date 2026-11-18
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

# (from station, to station, scheduled departure HH:MM) -- names as iRail knows them
TRIPS = [
    ("Brussels-North", "Leuven", "08:08"),
    ("Leuven", "Brussels-North", "17:21"),
]

CSV_FILE = Path(__file__).with_name("commute.csv")
FIELDS = ["date", "train", "from", "to", "scheduled_time", "delay_min", "cancelled"]
API = "https://api.irail.be/v1/connections"
TZ = ZoneInfo("Europe/Brussels")


def get_json(params, attempts=4):
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "train-commute-tracker/1.0"})
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except (urllib.error.URLError, json.JSONDecodeError) as e:  # iRail sometimes returns a 502
            if i == attempts - 1:
                raise
            print(f"  request failed ({e}), retrying...", file=sys.stderr)
            time.sleep(5 * (i + 1))


def find_trip(day, origin, dest, hhmm):
    """Return the direct connection leaving `origin` at exactly `hhmm` on `day`, or None."""
    data = get_json({
        "from": origin, "to": dest,
        "date": day.strftime("%d%m%y"), "time": hhmm.replace(":", ""),
        "timesel": "departure", "format": "json", "lang": "en", "results": 4,
    })
    for conn in data.get("connection", []):
        dep = conn["departure"]
        dep_time = datetime.fromtimestamp(int(dep["time"]), TZ)
        if dep_time.date() == day and dep_time.strftime("%H:%M") == hhmm and int(conn.get("vias", {}).get("number", 0)) == 0:
            return conn
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(TZ).date())
    day = parser.parse_args().date

    if day.weekday() >= 5:
        print(f"{day} is a weekend day, nothing to do.")
        return

    existing = set()
    if CSV_FILE.exists():
        with CSV_FILE.open(newline="") as f:
            existing = {(r["date"], r["from"], r["scheduled_time"]) for r in csv.DictReader(f)}

    rows = []
    for origin, dest, hhmm in TRIPS:
        if (day.isoformat(), origin, hhmm) in existing:
            print(f"{day} {hhmm} {origin} -> {dest}: already logged")
            continue
        conn = find_trip(day, origin, dest, hhmm)
        if conn is None:
            print(f"{day} {hhmm} {origin} -> {dest}: no direct train found at that time, skipped", file=sys.stderr)
            continue
        dep, arr = conn["departure"], conn["arrival"]
        cancelled = dep["canceled"] != "0" or arr["canceled"] != "0"
        if not cancelled and int(arr["time"]) + int(arr["delay"]) > time.time():
            print(f"{day} {hhmm} {origin} -> {dest}: train has not arrived yet, skipped (run again later)")
            continue
        row = {
            "date": day.isoformat(),
            "train": dep["vehicleinfo"]["shortname"],
            "from": origin,
            "to": dest,
            "scheduled_time": hhmm,
            "delay_min": round(int(arr["delay"]) / 60),  # delay on arrival at destination
            "cancelled": "yes" if cancelled else "no",
        }
        print(f"{day} {hhmm} {origin} -> {dest}: {row['train']}, {row['delay_min']} min late, cancelled={row['cancelled']}")
        rows.append(row)

    if rows:
        new_file = not CSV_FILE.exists()
        with CSV_FILE.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    main()
