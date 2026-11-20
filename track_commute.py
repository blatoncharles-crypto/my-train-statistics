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

Check-ins (data/checkins/*.csv, one line "ISO time,home|work,arrive|leave" per file,
written by an iPhone location automation) tell which train I actually took on a trip
without a correction:
  - one train fits, or several that all give the same reason (choice/disruption) and
    the same side of the 30-minute line -> the most likely one is used, certainty=located (📍)
  - otherwise -> the most likely one is used provisionally, certainty=uncertain (?) and up
    to 3 candidates are stored; if one of them might be a compensation case (journey delay
    >= 30) the run asks for a GitHub issue "Confirm train: <date> <direction>"
A correction always wins and clears the certainty.

Note: iRail only has real-time data for the current day, so a delay is only logged
when the script runs on the day itself (after the arrival). A trip without real-time
data is never logged as 0 minutes. A past day can be marked "skipped" at any time,
but "other train" only gets a delay on the day itself.
"""
import argparse
import csv
import json
import os
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
CHECKINS_DIR = ROOT / "data" / "checkins"
FIELDS = ["date", "direction", "train", "from_station", "to_station", "scheduled_departure",
          "scheduled_arrival", "planned_arrival", "actual_arrival", "train_delay_min",
          "journey_delay_min", "status", "reason", "source", "cancelled", "certainty", "candidates"]
REASONS = ("choice", "disruption")
RESULT_FIELDS = ["planned_arrival", "actual_arrival", "train_delay_min", "journey_delay_min"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
CORRECTION_WINDOW_DAYS = 14   # same window the dashboard allows corrections in
API = "https://api.irail.be/v1/vehicle"
CONNECTIONS_API = "https://api.irail.be/v1/connections"
COMPENSATION_MIN = 30         # journey delay that counts for NMBS repeated-delay compensation
MAX_CANDIDATES = 3
# How check-in events relate to the train (minutes). A location automation fires when the
# phone crosses the station geofence, sometimes a few minutes late.
TRIP_WINDOW = (-180, 180)     # check-in events this far around the usual departure belong to the trip
BOARD_AFTER_ARRIVE = (-1, 60) # departure: from just before "arrive" at the station up to an hour later
DEPART_BY_LEAVE = (-10, 2)    # departure: up to 10 min before "leave" (geofence left on the train)
ARRIVE_BY_ARRIVE = (-10, 3)   # arrival at to_station vs. its "arrive" event
ARRIVE_BY_LEAVE = (-30, 0)    # arrival at to_station vs. its "leave" event (only when there's no "arrive")
NO_CHECKIN = (-60, 120)       # departure window around the usual train without any check-in
TZ = ZoneInfo("Europe/Brussels")


def get_vehicle(train, day, attempts=4):
    """Return the iRail vehicle data for `train` (e.g. "IC 506") on `day`, or None if it doesn't run."""
    params = {"id": "BE.NMBS." + train.replace(" ", ""), "date": day.strftime("%d%m%y"), "format": "json", "lang": "en"}
    return get_json(API, params, attempts)


def get_json(url, params, attempts=4):
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
            "dep_ts": int(dep["scheduledDepartureTime"]), "arr_ts": int(arr["scheduledArrivalTime"]),
            "dep_actual_ts": int(dep["scheduledDepartureTime"]) + int(dep.get("departureDelay") or 0),
            "cancelled": cancelled}
    # iRail only serves real-time delays for the current day (older days come back as 0).
    actual_ts = info["arr_ts"] + int(arr.get("arrivalDelay") or 0)
    if not cancelled and day == datetime.now(TZ).date() and (arr.get("arrived") == "1" or actual_ts <= time.time()):
        info.update(actual_ts=actual_ts, train_delay_min=round((actual_ts - info["arr_ts"]) / 60))
    return info, None


def minute_of(ts):
    t = datetime.fromtimestamp(int(ts), TZ)
    return t.hour * 60 + t.minute


def get_connections(from_id, to_id, day, start, end):
    """Direct trains from_id -> to_id departing (scheduled) between minute `start` and `end` of `day`."""
    found, t = {}, max(0, start)
    for _ in range(10):
        if t > end:
            break
        data = get_json(CONNECTIONS_API, {"from": from_id, "to": to_id, "date": day.strftime("%d%m%y"),
                                          "time": f"{t // 60:02d}{t % 60:02d}", "timesel": "departure",
                                          "format": "json", "lang": "en", "results": 6})
        conns = (data or {}).get("connection", [])
        if not conns:
            break
        latest = t
        for c in conns:
            dep, arr = c["departure"], c["arrival"]
            m = minute_of(dep["time"])
            latest = max(latest, m)
            if int((c.get("vias") or {}).get("number", 0)) or not (start <= m <= end):
                continue
            found[dep["vehicleinfo"]["shortname"]] = {
                "train": dep["vehicleinfo"]["shortname"],
                "dep": m, "dep_actual": m + round(int(dep.get("delay") or 0) / 60),
                "arr": minute_of(arr["time"]), "arr_actual": minute_of(arr["time"]) + round(int(arr.get("delay") or 0) / 60),
                "cancelled": "1" in (dep.get("canceled"), arr.get("canceled"))}
        if latest < t:
            break
        t = latest + 1
    return sorted(found.values(), key=lambda c: c["dep"])


def read_checkins():
    """[(datetime in Belgian time, "home"|"work", "arrive"|"leave")] from data/checkins/*.csv."""
    events = []
    for f in sorted(CHECKINS_DIR.glob("*.csv")) if CHECKINS_DIR.exists() else []:
        for line in f.read_text().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3 or parts[1] not in ("home", "work") or parts[2] not in ("arrive", "leave"):
                continue
            try:
                when = datetime.fromisoformat(parts[0].replace("Z", "+00:00"))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=TZ)
            events.append((when.astimezone(TZ), parts[1], parts[2]))
    return events


def parse_candidates(text):
    """'IC 506/08:08/planned/7|IC 3607/08:22/disruption/19' -> list of dicts."""
    out = []
    for part in filter(None, (text or "").split("|")):
        train, dep, kind, journey = (part.split("/") + ["", "", "", ""])[:4]
        out.append({"train": train, "dep": dep, "kind": kind, "journey": journey})
    return out


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
    def __init__(self, day, config, rows, by_key, checkins=()):
        self.day, self.config, self.rows, self.by_key = day, config, rows, by_key
        self.checkins = [e for e in checkins if e[0].date() == day]
        home, work = config["stations"]["home"]["id"], config["stations"]["work"]["id"]
        self.legs = {"out": (home, work), "back": (work, home)}

    def new_row(self, direction):
        from_id, to_id = self.legs[direction]
        usual = self.config["trains"][direction]
        row = {"date": self.day.isoformat(), "direction": direction, "train": usual["train"],
               "from_station": from_id, "to_station": to_id, "scheduled_departure": usual["departure"],
               "scheduled_arrival": "", "planned_arrival": "", "actual_arrival": "", "train_delay_min": "",
               "journey_delay_min": "", "status": "planned", "reason": "", "source": "auto", "cancelled": "false",
               "certainty": "", "candidates": ""}
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
        self._apply_correction(direction, corr)
        row = self.by_key.get((self.day.isoformat(), direction))
        if row and (row["certainty"] or row["candidates"]):
            row.update(certainty="", candidates="")      # I said which train it was

    def _apply_correction(self, direction, corr):
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
            return self.auto_update(direction, infer=False)   # nothing to undo, normal logging
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

    def infer(self, label, direction):
        """Which train did I take, according to the check-ins? None if there are none for this trip.

        Returns {"best": candidate, "candidates": [...], "certainty": "located"|"uncertain"}; each
        candidate has train, dep, kind ("planned"/"choice"/"disruption") and journey (minutes)."""
        usual = self.config["trains"][direction]
        from_id, to_id = self.legs[direction]
        home = self.config["stations"]["home"]["id"]
        from_role, to_role = ("home", "work") if from_id == home else ("work", "home")
        dep0 = sum(x * y for x, y in zip(map(int, usual["departure"].split(":")), (60, 1)))
        mins = lambda e: e[0].hour * 60 + e[0].minute
        near = [e for e in self.checkins if dep0 + TRIP_WINDOW[0] <= mins(e) <= dep0 + TRIP_WINDOW[1]]
        at = lambda role, kind, after=-1: sorted(mins(e) for e in near if e[1] == role and e[2] == kind and mins(e) >= after)
        cin_arrive = (at(from_role, "arrive") or [None])[-1]
        cin_leave = (at(from_role, "leave", cin_arrive if cin_arrive is not None else -1) or [None])[0]
        start = cin_leave if cin_leave is not None else (cin_arrive if cin_arrive is not None else -1)
        out_arrive = (at(to_role, "arrive", start) or [None])[0]
        out_leave = (at(to_role, "leave", out_arrive if out_arrive is not None else start) or [None])[0]
        if cin_arrive is None and cin_leave is None and out_arrive is None and out_leave is None:
            return None

        usual_info = self.fetch(label, usual["train"], from_id, to_id)
        usual_cancelled = bool(usual_info and usual_info["cancelled"])
        usual_dep_actual = minute_of(usual_info["dep_actual_ts"]) if usual_info else dep0
        usual_arr = minute_of(usual_info["arr_ts"]) if usual_info else None

        conns = get_connections(from_id, to_id, self.day, dep0 + TRIP_WINDOW[0], dep0 + TRIP_WINDOW[1])
        fits = []
        for c in conns:
            if c["cancelled"]:
                continue
            d, a = c["dep_actual"], c["arr_actual"]
            ok = True
            if cin_arrive is not None:
                ok &= cin_arrive + BOARD_AFTER_ARRIVE[0] <= d and (cin_leave is not None or d <= cin_arrive + BOARD_AFTER_ARRIVE[1])
            if cin_leave is not None:
                ok &= cin_leave + DEPART_BY_LEAVE[0] <= d <= cin_leave + DEPART_BY_LEAVE[1]
            if cin_arrive is None and cin_leave is None:
                ok &= dep0 + NO_CHECKIN[0] <= c["dep"] <= dep0 + NO_CHECKIN[1]
            if out_arrive is not None:
                ok &= out_arrive + ARRIVE_BY_ARRIVE[0] <= a <= out_arrive + ARRIVE_BY_ARRIVE[1]
            elif out_leave is not None:
                ok &= out_leave + ARRIVE_BY_LEAVE[0] <= a <= out_leave + ARRIVE_BY_LEAVE[1]
            if not ok:
                continue
            score = -0.5 if c["train"] == usual["train"] else 0
            if out_arrive is not None:
                score += abs(a - (out_arrive - 1))
            elif out_leave is not None:
                score += abs(a - (out_leave - 5))
            if cin_leave is not None:
                score += abs(d - cin_leave)
            elif cin_arrive is not None:
                score += (d - cin_arrive) * 0.5          # the first train after I got there is the likeliest
            if c["train"] == usual["train"]:
                kind, journey = "planned", a - c["arr"]
            else:
                if usual_cancelled:
                    kind = "disruption"
                elif c["dep"] < dep0:
                    kind = "choice"                       # an earlier train than usual
                elif cin_arrive is not None:
                    kind = "disruption" if cin_arrive <= usual_dep_actual else "choice"
                else:
                    kind = "disruption" if c["dep"] <= usual_dep_actual else "choice"
                journey = (a - c["arr"]) if kind == "choice" else (a - usual_arr if usual_arr is not None else a - c["arr"])
            fits.append({**c, "score": score, "kind": kind, "journey": journey})
        if not fits:
            print(f"{label}: check-ins found, but no train fits them - using the usual train", file=sys.stderr)
            return None
        fits.sort(key=lambda c: (c["score"], c["dep"]))
        cands = fits[:MAX_CANDIDATES]
        # Only ask when the choice matters: own choice vs. measured against the usual train (the usual
        # train itself or a disruption), or which side of the compensation line the journey delay falls.
        buckets = {(c["kind"] == "choice", c["journey"] >= COMPENSATION_MIN) for c in cands}
        return {"best": cands[0], "candidates": cands, "certainty": "located" if len(buckets) == 1 else "uncertain"}

    def auto_update(self, direction, infer=True):
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
            if is_complete(row) and (row["status"] == "planned" or row["certainty"]):
                print(f"{label}: already logged ({row['train']}, {self.describe(row)})")
                return
            train = row["train"] if row["status"] == "other" else usual["train"]
            from_id, to_id = row["from_station"], row["to_station"]
            reason = (row["reason"] or "choice") if row["status"] == "other" else "disruption"
        else:
            train = usual["train"]
            from_id, to_id = self.legs[direction]
            reason = "disruption"

        found = self.infer(label, direction) if infer and self.day == datetime.now(TZ).date() else None
        if found:
            best = found["best"]
            train = best["train"]
            reason = "disruption" if best["kind"] == "planned" else best["kind"]
        fields = self.measure(label, direction, train, from_id, to_id, reason)
        if not fields or (fields["journey_delay_min"] == "" and fields.get("cancelled") != "true"):
            return                                       # never log a trip without a real result
        row = row or self.new_row(direction)
        row.update(fields, train=train)
        if found:
            other = train != usual["train"]
            row.update(status="other" if other else "planned", reason=reason if other else "",
                       certainty=found["certainty"],
                       candidates="" if found["certainty"] == "located" else "|".join(
                           f"{c['train']}/{c['dep'] // 60:02d}:{c['dep'] % 60:02d}/{c['kind']}/{c['journey']}"
                           for c in found["candidates"]))
            print(f"{label}: check-ins -> {train} ({'📍 located' if found['certainty'] == 'located' else '? uncertain: ' + row['candidates']})")
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

    rows = [{**dict.fromkeys(FIELDS, ""), **r} for r in read_csv(TRIPS_FILE)]   # older files lack newer columns
    before = json.dumps(rows, sort_keys=True)
    by_key = {(r["date"], r["direction"]): r for r in rows}
    corrections = latest_corrections()
    checkins = read_checkins()
    was_asking = {(r["date"], r["direction"]) for r in rows if needs_confirmation(r)}

    first, last = today - timedelta(days=CORRECTION_WINDOW_DAYS), today + timedelta(days=1)
    days = {run_day} | {date.fromisoformat(d) for d, _ in corrections if first.isoformat() <= d <= last.isoformat()}

    for day in sorted(days):
        if DAYS[day.weekday()] not in config["travel_days"]:
            if day == run_day:
                print(f"{day} is not a travel day, nothing to do.")
            continue
        d = Day(day, config, rows, by_key, checkins)
        for direction in config["trains"]:
            corr = corrections.get((day.isoformat(), direction))
            if corr:
                d.apply_correction(direction, corr)
            elif day == run_day:
                d.auto_update(direction)

    if json.dumps(rows, sort_keys=True) != before:
        write_trips(rows)
        print("data/trips.csv updated")
    write_confirmations(config, rows, was_asking)


def needs_confirmation(row):
    """An uncertain trip where one of the candidate trains would be a compensation case."""
    return row.get("certainty") == "uncertain" and any(
        c["journey"].lstrip("-").isdigit() and int(c["journey"]) >= COMPENSATION_MIN
        for c in parse_candidates(row.get("candidates")))


def write_confirmations(config, rows, was_asking):
    """Tell the workflow which "Confirm train" issues to open or close (tab-separated lines)."""
    names = {s["id"]: s["name"] for s in config["stations"].values()}
    lines = []
    for r in rows:
        key, title = (r["date"], r["direction"]), f"Confirm train: {r['date']} {r['direction']}"
        if needs_confirmation(r):
            label = {"planned": "the usual train", "choice": "own choice", "disruption": "due to delay"}
            body = (f"The evening run couldn't tell which train you took on {r['date']} "
                    f"({names.get(r['from_station'], r['from_station'])} → {names.get(r['to_station'], r['to_station'])}), "
                    f"and it might be a compensation case (journey delay ≥ {COMPENSATION_MIN} min):\\n\\n" +
                    "".join(f"- {c['dep']} · {c['train']} ({label.get(c['kind'], c['kind'])}): journey delay "
                            f"{'+' if int(c['journey']) > 0 else ''}{c['journey']} min\\n" for c in parse_candidates(r["candidates"])) +
                    "\\nOpen the dashboard and tap the right train on that trip.")
            lines.append(f"open\t{title}\t{body}")
        elif key in was_asking:
            lines.append(f"close\t{title}\t")
    for line in lines:
        print("confirm:", line.split("\t")[0], line.split("\t")[1])
    if os.environ.get("CONFIRM_FILE"):
        Path(os.environ["CONFIRM_FILE"]).write_text("".join(l + "\n" for l in lines))


if __name__ == "__main__":
    main()
