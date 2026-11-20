#!/usr/bin/env python3
"""Log my commute trips and their delays to data/trips.csv using the iRail API.

Stations, usual trains and travel days come from config/route.json. Run it in
the evening (after the last train). For each expected trip of the day it looks
the trains up with the iRail vehicle endpoint.

    python3 track_commute.py                  # today
    python3 track_commute.py --date 2026-11-18

Two delays are stored per trip:
  - train_delay_min:   delay of the train actually taken, at to_station
  - journey_delay_min: actual arrival at to_station minus the scheduled arrival of the
                       usual train that day (planned_arrival) - the NMBS definition used
                       for compensation. For a planned trip both are the same.
`cancelled` says whether the usual train was cancelled (departure at from_station,
arrival at to_station, or every stop of the train).

Corrections made on the dashboard land in data/corrections.csv (append-only, the
latest row per date + direction wins). Every run applies them to trips.csv for
every corrected day from CORRECTION_WINDOW_DAYS ago up to tomorrow:
  - skipped -> status=skipped, source=manual, both delays empty (the arrival times
               are kept, so an undo can bring the delays back)
  - other   -> the chosen train, status=other, source=manual, delays fetched for it
  - planned -> back to the usual train, status=planned, source=auto, delays fetched again

Rules for existing rows on a day without a correction:
  - source=manual -> never touched
  - status=skipped -> never touched
  - status=planned -> left alone once it has a journey delay or the usual train was
                      cancelled (then journey_delay_min stays empty until a correction
                      says which train was taken)
  - status=other -> (re)fetched for the train in that row
Running it twice gives the same result and never adds duplicate rows.

Note: iRail only has real-time data for the current day, so a delay is only logged
when the script runs on the day itself (after the arrival). A trip without real-time
data is never logged as 0 minutes. A past day can be marked "skipped" at any time,
but "other train" only gets a delay on the day itself.
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
          "scheduled_arrival", "planned_arrival", "actual_arrival", "train_delay_min",
          "journey_delay_min", "status", "source", "cancelled"]
RESULT_FIELDS = ["planned_arrival", "actual_arrival", "train_delay_min", "journey_delay_min"]
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


def minutes_between(later, earlier):
    """Whole minutes from HH:MM `earlier` to HH:MM `later` (same day)."""
    (h1, m1), (h2, m2) = (map(int, later.split(":")), map(int, earlier.split(":")))
    return (h1 * 60 + m1) - (h2 * 60 + m2)


def lookup(train, day, from_id, to_id):
    """Look up one train between two stations. Returns (info, None) or (None, reason).

    info: scheduled_departure, scheduled_arrival, arr_ts (scheduled arrival, unix),
    cancelled (bool) and - only when iRail has real-time data for it - actual_ts and
    train_delay_min."""
    data = get_vehicle(train, day)
    if data is None:
        return None, f"{train} does not run on {day}"
    stops = data["stops"]["stop"]
    ids = [s["stationinfo"]["id"] for s in stops]
    if from_id not in ids or to_id not in ids[ids.index(from_id) + 1:]:
        return None, f"{train} does not run from {from_id} to {to_id}"
    dep = stops[ids.index(from_id)]
    arr = stops[ids.index(to_id, ids.index(from_id) + 1)]
    cancelled = ("1" in (dep.get("departureCanceled"), dep.get("canceled"), arr.get("arrivalCanceled"))
                 or all(s.get("canceled") == "1" for s in stops))
    info = {"scheduled_departure": hhmm(dep["scheduledDepartureTime"]),
            "scheduled_arrival": hhmm(arr["scheduledArrivalTime"]),
            "arr_ts": int(arr["scheduledArrivalTime"]), "cancelled": cancelled}
    # iRail only serves real-time delays for the current day (older days come back as 0).
    actual_ts = info["arr_ts"] + int(arr.get("arrivalDelay") or 0)
    if not cancelled and day == datetime.now(TZ).date() and (arr.get("arrived") == "1" or actual_ts <= time.time()):
        info.update(actual_ts=actual_ts, train_delay_min=round((actual_ts - info["arr_ts"]) / 60))
    return info, None


def is_complete(row):
    """A row the evening run doesn't need to look at again."""
    return row["journey_delay_min"] != "" or (row["status"] == "planned" and row["cancelled"] == "true")


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
        usual = self.config["trains"][direction]
        row = {"date": self.day.isoformat(), "direction": direction, "train": usual["train"],
               "from_station": from_id, "to_station": to_id, "scheduled_departure": usual["departure"],
               "scheduled_arrival": "", "planned_arrival": "", "actual_arrival": "", "train_delay_min": "",
               "journey_delay_min": "", "status": "planned", "source": "auto", "cancelled": "false"}
        self.rows.append(row)
        self.by_key[(row["date"], direction)] = row
        return row

    def fetch(self, label, train, from_id, to_id):
        info, reason = lookup(train, self.day, from_id, to_id)
        if info is None:
            print(f"{label}: {train} - {reason}", file=sys.stderr)
        elif "actual_ts" not in info and not info["cancelled"]:
            print(f"{label}: {train} - no real-time arrival data (not arrived yet, or not today)")
        return info

    def measure(self, label, direction, train, from_id, to_id):
        """Fields for a trip on `train` (the usual one or another), measured against the usual train.
        Returns None if the usual train can't be found at all."""
        usual_name = self.config["trains"][direction]["train"]
        usual = self.fetch(label, usual_name, from_id, to_id)
        taken = usual if train == usual_name else self.fetch(label, train, from_id, to_id)
        fields = {k: "" for k in RESULT_FIELDS}
        if usual:
            fields.update(planned_arrival=usual["scheduled_arrival"], cancelled="true" if usual["cancelled"] else "false")
        if taken:
            fields.update(scheduled_departure=taken["scheduled_departure"], scheduled_arrival=taken["scheduled_arrival"])
            if "actual_ts" in taken:
                fields.update(actual_arrival=hhmm(taken["actual_ts"]), train_delay_min=str(taken["train_delay_min"]))
                if usual:
                    fields["journey_delay_min"] = str(round((taken["actual_ts"] - usual["arr_ts"]) / 60))
        return fields if usual or taken else None

    def describe(self, row):
        if row["journey_delay_min"] != "":
            extra = f" (train {row['train_delay_min']})" if row["status"] == "other" else ""
            return f"journey delay {row['journey_delay_min']} min{extra}"
        return "usual train cancelled, journey delay unknown" if row["cancelled"] == "true" else "delay unknown"

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
            row.update(status="skipped", source="manual", train_delay_min="", journey_delay_min="")
            print(f"{label}: marked as not travelled")
            return

        if status == "other":
            train = corr["train"].strip()
            if row and row["status"] == "other" and row["source"] == "manual" and row["train"] == train and is_complete(row):
                return
            row = row or self.new_row(direction)
            fields = self.measure(label, direction, train, row["from_station"], row["to_station"])
            row.update(train=train, status="other", source="manual", **{k: "" for k in RESULT_FIELDS})
            if fields:
                row.update(fields)
            print(f"{label}: other train {train}, {self.describe(row)}")
            return

        # planned: undo any correction
        if row is None or (row["source"] == "auto" and row["status"] == "planned" and row["train"] == usual):
            return self.auto_update(direction)            # nothing to undo, normal logging
        # undoing "skipped" on the usual train: its arrival times are still there
        keep = row["train"] == usual and row["actual_arrival"] and row["planned_arrival"]
        old = {k: row[k] for k in ("planned_arrival", "actual_arrival")}
        fields = self.measure(label, direction, usual, row["from_station"], row["to_station"])
        row.update(train=usual, status="planned", source="auto", **{k: "" for k in RESULT_FIELDS})
        if fields:
            row.update(fields)
        if row["journey_delay_min"] == "" and row["cancelled"] != "true" and keep:
            delay = str(minutes_between(old["actual_arrival"], old["planned_arrival"]))
            row.update(old, train_delay_min=delay, journey_delay_min=delay)
        print(f"{label}: back to the usual train {usual}, {self.describe(row)}")

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
            if row["status"] == "planned" and is_complete(row):
                print(f"{label}: already logged ({row['train']}, {self.describe(row)})")
                return
            train = row["train"] if row["status"] == "other" else usual["train"]
            from_id, to_id = row["from_station"], row["to_station"]
        else:
            train = usual["train"]
            from_id, to_id = self.legs[direction]

        fields = self.measure(label, direction, train, from_id, to_id)
        if not fields or (fields["journey_delay_min"] == "" and fields.get("cancelled") != "true"):
            return                                       # never log a trip without a real result
        row = row or self.new_row(direction)
        row.update(fields, train=train)
        if row["scheduled_departure"] != usual["departure"] and row["status"] == "planned":
            print(f"{label}: note - {train} now departs at {row['scheduled_departure']}, "
                  f"config says {usual['departure']}", file=sys.stderr)
        print(f"{label}: {train} {row['scheduled_departure']}, {self.describe(row)}")


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
