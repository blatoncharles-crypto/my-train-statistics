#!/usr/bin/env python3
"""Log my commute trips and their delays to data/trips.csv using the iRail API.

Stations, usual trains and travel days come from config/route.json. Run it in
the evening (after the last train). For each expected trip of the day it looks
the trains up with the iRail vehicle endpoint.

    python3 track_commute.py                  # today
    python3 track_commute.py --date 2026-11-18

Two delays are stored per trip:
  - train_delay_min:   delay of the train actually taken, at to_station
  - journey_delay_min: actual arrival at to_station minus planned_arrival - the NMBS
                       definition used for compensation. planned_arrival is the scheduled
                       arrival of the train I planned to take:
                         planned trip        -> the usual train (both delays are the same)
                         other, reason=choice     -> the chosen train itself (I left later/earlier
                                                     by choice, so only its own delay counts)
                         other, reason=disruption -> the usual train (it was late or cancelled)
`cancelled` says whether the usual train was cancelled (departure at from_station,
arrival at to_station, or every stop of the train).

Corrections made on the dashboard land in data/corrections.csv (append-only, the
latest row per date + direction wins). Every run applies them to trips.csv for
every corrected day from CORRECTION_WINDOW_DAYS ago up to tomorrow:
  - skipped -> status=skipped, source=manual, both delays empty (the arrival times
               are kept, so an undo can bring the delays back)
  - other   -> the chosen train, status=other, source=manual, the correction's reason
               (choice/disruption; missing = choice), delays fetched for it
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
CONNECTIONS_FILE = ROOT / "data" / "connections.json"   # timetable for the dashboard's "Other train" picker
FIELDS = ["date", "direction", "train", "from_station", "to_station", "scheduled_departure",
          "scheduled_arrival", "planned_arrival", "actual_arrival", "train_delay_min",
          "journey_delay_min", "status", "reason", "source", "cancelled"]
REASONS = ("choice", "disruption")
RESULT_FIELDS = ["planned_arrival", "actual_arrival", "train_delay_min", "journey_delay_min"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
CORRECTION_WINDOW_DAYS = 14   # same window the dashboard allows corrections in
API = "https://api.irail.be/v1/vehicle"
CONNECTIONS_API = "https://api.irail.be/v1/connections"
PICKER_WINDOW = (-60, 120)    # the picker offers trains from 1 h before to 2 h after the usual departure
TZ = ZoneInfo("Europe/Brussels")


def get_vehicle(train, day, attempts=4):
    """Return the iRail vehicle data for `train` (e.g. "IC 506") on `day`, or None if it doesn't run."""
    params = {"id": "BE.NMBS." + train.replace(" ", ""), "date": day.strftime("%d%m%y"), "format": "json", "lang": "en"}
    return get_json(API, params, attempts)


def get_json(url, params, attempts=4):
    """GET an iRail endpoint, retrying the 502/504s it regularly returns. None on a 404."""
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
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


def scheduled_trains(from_id, to_id, day, start, end):
    """[[train, departure, arrival]] of the direct trains from_id -> to_id leaving between minute
    `start` and `end` of `day` (scheduled times, HH:MM)."""
    found, t = {}, max(0, start)
    for _ in range(10):                        # 6 connections per page
        if t > end:
            break
        data = get_json(CONNECTIONS_API, {"from": from_id, "to": to_id, "date": day.strftime("%d%m%y"),
                                          "time": f"{t // 60:02d}{t % 60:02d}", "timesel": "departure",
                                          "format": "json", "lang": "en", "results": 6})
        conns = (data or {}).get("connection", [])
        latest = t
        for c in conns:
            dep, arr = hhmm(c["departure"]["time"]), hhmm(c["arrival"]["time"])
            m = int(dep[:2]) * 60 + int(dep[3:])
            latest = max(latest, m)
            if datetime.fromtimestamp(int(c["departure"]["time"]), TZ).date() != day or not start <= m <= end:
                continue
            if int((c.get("vias") or {}).get("number", 0)) == 0:
                found[c["departure"]["vehicleinfo"]["shortname"]] = [c["departure"]["vehicleinfo"]["shortname"], dep, arr]
        if not conns or latest < t:
            break
        t = latest + 1
    return sorted(found.values(), key=lambda c: c[1])


def update_connections(config, today):
    """Write data/connections.json: the direct trains around the usual departure, per travel day and
    direction, from CORRECTION_WINDOW_DAYS ago up to the next travel day. The dashboard's picker reads
    this file instead of calling iRail from the browser (iRail answers ~15% of requests with a 504).
    Past days already in the file are kept (their timetable doesn't change); today and the next travel
    day are fetched again. Days outside the window are dropped, so the file stays small."""
    try:
        old = json.loads(CONNECTIONS_FILE.read_text()).get("days", {})
    except (OSError, ValueError):
        old = {}
    travel = [today - timedelta(days=n) for n in range(CORRECTION_WINDOW_DAYS, -1, -1)]
    nxt = today + timedelta(days=1)
    while DAYS[nxt.weekday()] not in config["travel_days"]:
        nxt += timedelta(days=1)
    travel = [d for d in travel + [nxt] if DAYS[d.weekday()] in config["travel_days"]]
    home, work = config["stations"]["home"]["id"], config["stations"]["work"]["id"]
    legs = {"out": (home, work), "back": (work, home)}
    days, fetched = {}, 0
    for day in travel:
        key = day.isoformat()
        if day < today and set(old.get(key, {})) >= set(config["trains"]):
            days[key] = old[key]
            continue
        try:
            entry = {}
            for direction, usual in config["trains"].items():
                dep = int(usual["departure"][:2]) * 60 + int(usual["departure"][3:])
                entry[direction] = scheduled_trains(*legs[direction], day, dep + PICKER_WINDOW[0], dep + PICKER_WINDOW[1])
                if not entry[direction]:               # never store an empty timetable: it would never be re-fetched
                    raise ValueError(f"no {direction} trains returned")
            days[key] = entry
            fetched += 1
        except Exception as e:                     # never let the picker data break the evening run
            print(f"connections for {key}: {e} - keeping what was there", file=sys.stderr)
            if key in old:
                days[key] = old[key]
    lines = ",\n".join(f'  "{k}": {json.dumps(v, separators=(",", ":"))}' for k, v in sorted(days.items()))
    text = '{"window_minutes": %s, "days": {\n%s\n}}\n' % (json.dumps(list(PICKER_WINDOW)), lines)
    try:
        if CONNECTIONS_FILE.read_text() == text:
            return
    except OSError:
        pass
    CONNECTIONS_FILE.write_text(text)
    print(f"data/connections.json updated ({len(days)} days, {fetched} fetched, {len(text)} bytes)")


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
            if c["status"] == "other" and c.get("reason") not in REASONS:
                c["reason"] = "choice"
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
               "journey_delay_min": "", "status": "planned", "reason": "", "source": "auto", "cancelled": "false"}
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

    def measure(self, label, direction, train, from_id, to_id, reason="disruption", known=None):
        """Fields for a trip on `train` (the usual one or another).

        The journey delay is measured against the usual train, except for reason=choice, where
        the chosen train is the planned one. `known` is an existing row for the same train: if
        iRail has no real-time data any more, its actual arrival is reused (e.g. when only the
        reason changes after the day). Returns None if no train can be found at all."""
        usual_name = self.config["trains"][direction]["train"]
        against_usual = train == usual_name or reason != "choice"
        usual = self.fetch(label, usual_name, from_id, to_id) if against_usual else None
        taken = usual if train == usual_name else self.fetch(label, train, from_id, to_id)
        planned = usual if against_usual else taken
        fields = {k: "" for k in RESULT_FIELDS}
        if usual:
            fields["cancelled"] = "true" if usual["cancelled"] else "false"
        if planned:
            fields["planned_arrival"] = planned["scheduled_arrival"]
        if taken:
            fields.update(scheduled_departure=taken["scheduled_departure"], scheduled_arrival=taken["scheduled_arrival"])
            if "actual_ts" in taken:
                fields.update(actual_arrival=hhmm(taken["actual_ts"]), train_delay_min=str(taken["train_delay_min"]))
            elif known and known.get("actual_arrival") and known.get("train_delay_min") != "":
                fields.update(actual_arrival=known["actual_arrival"], train_delay_min=known["train_delay_min"])
        if fields["train_delay_min"] != "" and planned is taken:
            fields["journey_delay_min"] = fields["train_delay_min"]          # planned train = train taken
        elif taken and "actual_ts" in taken and planned:
            fields["journey_delay_min"] = str(round((taken["actual_ts"] - planned["arr_ts"]) / 60))
        elif fields["actual_arrival"] and fields["planned_arrival"]:
            fields["journey_delay_min"] = str(minutes_between(fields["actual_arrival"], fields["planned_arrival"]))
        return fields if usual or taken else None

    def describe(self, row):
        if row["journey_delay_min"] != "":
            extra = f" (train {row['train_delay_min']}, {row['reason']})" if row["status"] == "other" else ""
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
            row.update(status="skipped", reason="", source="manual", train_delay_min="", journey_delay_min="")
            print(f"{label}: marked as not travelled")
            return

        if status == "other":
            train, reason = corr["train"].strip(), corr["reason"]
            if (row and row["status"] == "other" and row["source"] == "manual" and row["train"] == train
                    and row["reason"] == reason and is_complete(row)):
                return
            row = row or self.new_row(direction)
            known = dict(row) if row["train"] == train else None
            fields = self.measure(label, direction, train, row["from_station"], row["to_station"], reason, known)
            row.update(train=train, status="other", reason=reason, source="manual", **{k: "" for k in RESULT_FIELDS})
            if fields:
                row.update(fields)
            print(f"{label}: other train {train} ({reason}), {self.describe(row)}")
            return

        # planned: undo any correction
        if row is None or (row["source"] == "auto" and row["status"] == "planned" and row["train"] == usual):
            return self.auto_update(direction)            # nothing to undo, normal logging
        # undoing "skipped" on the usual train: its arrival times are still there
        keep = row["train"] == usual and row["actual_arrival"] and row["planned_arrival"]
        old = {k: row[k] for k in ("planned_arrival", "actual_arrival")}
        fields = self.measure(label, direction, usual, row["from_station"], row["to_station"])
        row.update(train=usual, status="planned", reason="", source="auto", **{k: "" for k in RESULT_FIELDS})
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
            reason = (row["reason"] or "choice") if row["status"] == "other" else "disruption"
        else:
            train = usual["train"]
            from_id, to_id = self.legs[direction]
            reason = "disruption"

        fields = self.measure(label, direction, train, from_id, to_id, reason)
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

    update_connections(config, today)

    if json.dumps(rows, sort_keys=True) != before:
        write_trips(rows)
        print("data/trips.csv updated")


if __name__ == "__main__":
    main()
