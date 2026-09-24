#!/usr/bin/env python3
"""India Fares: on-demand fare check via Scrappa's Google Flights API.

Run by the GitHub workflow when you tap "Check prices now" in the app.
Env: SCRAPPA_KEY (required).  FARE_WATCH_MOCK=file.json for an offline test.
     python tracker.py --probe   saves one raw Scrappa response to docs/probe.json
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
STATE_FILE = ROOT / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"
PROBE_FILE = ROOT / "docs" / "probe.json"
ENDPOINT = "https://scrappa.co/api/flights/round-trip"

CITY = {"MAA": "Chennai", "BLR": "Bengaluru", "BOM": "Mumbai", "COK": "Kochi",
        "TRV": "Thiruvananthapuram", "CCJ": "Kozhikode", "CNN": "Kannur"}

ORIGIN = CFG["origin"]
DESTS = list(CFG["destinations"])
RULES = CFG["rules"]
SEARCH = CFG["search"]
ADULTS = CFG["passengers"]["adults"]
CHILD_AGES = CFG["passengers"]["child_ages"]
ANY_AIRLINE = str(CFG.get("airline_mode", "any")).lower() == "any"
ALLOWED = set(CFG["airlines"])
EXCLUDED = set(CFG.get("exclude_airlines") or [])
HALL_KEEP, HALL_SHOW = 200, 20


class OutOfCredits(Exception):
    pass


class CallCap(Exception):
    pass


class Api:
    def __init__(self) -> None:
        self.key = os.environ.get("SCRAPPA_KEY", "")
        self.mock = os.environ.get("FARE_WATCH_MOCK")
        self.calls = 0
        self.lock = threading.Lock()
        self.first_raw: dict | None = None

    def get(self, params: dict) -> dict:
        with self.lock:
            if self.calls >= SEARCH["max_calls_per_check"]:
                raise CallCap
            self.calls += 1
        if self.mock:
            data = json.loads(Path(self.mock).read_text())
        else:
            data = {}
            for attempt in range(4):
                r = requests.get(ENDPOINT, params=params, headers={"x-api-key": self.key, "Accept": "application/json"}, timeout=90)
                if r.status_code == 402:
                    raise OutOfCredits
                if r.status_code in (429, 503):
                    wait = min(int((r.json() if r.content else {}).get("retry_after", 20)), 60)
                    time.sleep(wait * (attempt + 1) / 2)
                    continue
                if r.status_code == 422:
                    print(f"  validation error: {r.text[:300]}", file=sys.stderr)
                    return {}
                if not r.ok:
                    print(f"  HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
                    return {}
                data = r.json()
                break
        with self.lock:
            if self.first_raw is None and data.get("flights"):
                self.first_raw = data
        return data


# ---------- parsing (tolerant of a few response layouts) ----------

def _ap(v) -> str:
    if isinstance(v, dict):
        return (v.get("id") or v.get("code") or v.get("iata") or "").upper()
    return str(v or "").upper()


def _time(leg: dict, side: str) -> str:
    v = leg.get(f"{side}_time")
    if not v and isinstance(leg.get(f"{side}_airport"), dict):
        v = leg[f"{side}_airport"].get("time")
    return str(v or "").replace("T", " ")[:16]


def _parse_t(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _legs_of(x) -> list | None:
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("legs", "flights", "segments"):
            if isinstance(x.get(k), list):
                return x[k]
    return None


def split_directions(item: dict) -> tuple[list, list, dict, dict] | None:
    """Return (outbound_legs, return_legs, outbound_obj, return_obj) or None if unrecognised."""
    for ok, rk in (("outbound", "return"), ("outbound", "inbound"), ("outbound_flight", "return_flight"),
                   ("departure", "return"), ("outbound_legs", "return_legs"), ("outbound_flights", "return_flights")):
        o, r = _legs_of(item.get(ok)), _legs_of(item.get(rk))
        if o and r:
            return o, r, item.get(ok) if isinstance(item.get(ok), dict) else {}, item.get(rk) if isinstance(item.get(rk), dict) else {}
    legs = _legs_of(item)
    if legs and len(legs) >= 2:
        dests = set(DESTS)
        for i in range(1, len(legs)):
            if _ap(legs[i].get("departure_airport")) in dests and _ap(legs[i - 1].get("arrival_airport")) in dests:
                a, b = _parse_t(_time(legs[i - 1], "arrival")), _parse_t(_time(legs[i], "departure"))
                if a and b and (b - a).total_seconds() > 3 * 86400:   # a stay in India, not a layover
                    return legs[:i], legs[i:], {}, {}
    return None


def normalize(legs: list, obj: dict) -> dict | None:
    segs = []
    for l in legs:
        fn = str(l.get("flight_number") or "").strip()
        code = str(l.get("airline") or "").strip()
        if len(code) != 2:
            code = fn.split(" ")[0] if " " in fn else fn[:2]
        segs.append({
            "from": _ap(l.get("departure_airport")), "to": _ap(l.get("arrival_airport")),
            "depart": _time(l, "departure"), "arrive": _time(l, "arrival"),
            "airline": l.get("airline_name") or l.get("airline"), "code": code.upper(),
            "flight": fn or f"{code} ?", "minutes": l.get("duration_minutes") or l.get("duration"),
            "aircraft": l.get("airplane") or l.get("aircraft"),
        })
    if not segs or not all(s["from"] and s["to"] and s["depart"] and s["arrive"] for s in segs):
        return None
    lays = []
    for a, b in zip(segs, segs[1:]):
        ta, tb = _parse_t(a["arrive"]), _parse_t(b["depart"])
        mins = int((tb - ta).total_seconds() // 60) if ta and tb else None
        lays.append({"airport": a["to"], "name": a["to"], "minutes": mins,
                     "overnight": bool(ta and tb and tb.date() != ta.date()), "change": a["to"] != b["from"]})
    total = obj.get("total_duration_minutes") or obj.get("duration_minutes")
    if not total:
        total = sum(s["minutes"] or 0 for s in segs) + sum(l["minutes"] or 0 for l in lays)
    return {"from": segs[0]["from"], "to": segs[-1]["to"], "depart": segs[0]["depart"], "arrive": segs[-1]["arrive"],
            "minutes": int(total), "stops": len(lays), "segments": segs, "layovers": lays}


def leg_problem(leg: dict) -> str | None:
    lays = leg["layovers"]
    if leg["stops"] > RULES["max_stops"]:
        return "too many stops"
    if leg["minutes"] > RULES["max_leg_minutes"]:
        return "over 35 h"
    if any(l["minutes"] is None for l in lays):
        return "unknown layover"
    if leg["stops"] == 1 and lays[0]["minutes"] < RULES["one_stop_min_layover"]:
        return "1-stop layover under 6 h"
    if leg["stops"] == 2 and any(l["minutes"] < RULES["two_stop_min_layover"] for l in lays):
        return "2-stop layover under 4 h"
    if any(l["change"] for l in lays):
        return "airport change"
    codes = [s["code"] for s in leg["segments"]]
    if ANY_AIRLINE and any(c in EXCLUDED for c in codes):
        return "excluded airline"
    if not ANY_AIRLINE and any(c not in ALLOWED for c in codes):
        return "airline not on list"
    return None


def signature(leg: dict) -> str:
    return "+".join(s["flight"].replace(" ", "") for s in leg["segments"])


# ---------- search ----------

def params_for(dep: dt.date, ret: dt.date, dest: str, adults: int, children: int) -> dict:
    p = {"origin": ORIGIN, "destination": dest, "departure_date": dep.isoformat(), "return_date": ret.isoformat(),
         "adults": adults, "children": children, "max_stops": "two_or_fewer", "sort_by": "cheapest",
         "currency": SEARCH["currency"], "gl": SEARCH["country"], "hl": "en",
         "max_duration_minutes": RULES["max_leg_minutes"]}
    if not ANY_AIRLINE:
        p["airlines"] = ",".join(CFG["airlines"])
    return p


def links(dest: str, dep: str, ret: str) -> tuple[str, str]:
    q = urllib.parse.quote(f"Flights from {ORIGIN} to {dest} on {dep} returning {ret} "
                           f"{ADULTS} adults {len(CHILD_AGES)} children economy")
    google = f"https://www.google.com/travel/flights?q={q}&curr={SEARCH['currency']}"
    d = dt.date.fromisoformat(dep).strftime("%y%m%d")
    r = dt.date.fromisoformat(ret).strftime("%y%m%d")
    kids = "%7C".join(str(a) for a in CHILD_AGES)
    sky = (f"https://www.skyscanner.ca/transport/flights/{ORIGIN.lower()}/{dest.lower()}/{d}/{r}/"
           f"?adultsv2={ADULTS}&childrenv2={kids}&cabinclass=economy")
    return google, sky


def parse_options(resp: dict) -> tuple[list[dict], int]:
    """Valid itineraries from one response, plus how many items couldn't be understood."""
    out, unknown = [], 0
    for item in resp.get("flights") or []:
        price = item.get("price") or item.get("total_price")
        split = split_directions(item)
        if not split or not price:
            unknown += 1
            continue
        o_legs, r_legs, o_obj, r_obj = split
        ob, rt = normalize(o_legs, o_obj), normalize(r_legs, r_obj)
        if not ob or not rt:
            unknown += 1
            continue
        if leg_problem(ob) or leg_problem(rt):
            continue
        out.append({"out": ob, "ret": rt, "total": round(float(price))})
    return out, unknown


def search_one(api: Api, dep: dt.date, ret: dt.date, dest: str) -> tuple[list[dict], int, int]:
    resp = api.get(params_for(dep, ret, dest, ADULTS, len(CHILD_AGES)))
    opts, unknown = parse_options(resp)
    found = []
    for o in sorted(opts, key=lambda x: x["total"])[: SEARCH["options_per_search"]]:
        g, s = links(o["out"]["to"], dep.isoformat(), ret.isoformat())
        found.append({
            "id": f"{dep}|{ret}|{signature(o['out'])}|{signature(o['ret'])}",
            "depart_date": dep.isoformat(), "return_date": ret.isoformat(), "dest": o["out"]["to"],
            "total": o["total"], "currency": SEARCH["currency"], "out": o["out"], "ret": o["ret"],
            "airlines": sorted({x["airline"] for x in o["out"]["segments"] + o["ret"]["segments"] if x["airline"]}),
            "google_url": g, "skyscanner_url": s,
        })
    return found, unknown, len(resp.get("flights") or [])


def real_breakdown(api: Api, it: dict) -> dict | None:
    """Re-price the identical flights for 1 adult; child fare = rest of the family total."""
    dep, ret = dt.date.fromisoformat(it["depart_date"]), dt.date.fromisoformat(it["return_date"])
    opts, _ = parse_options(api.get(params_for(dep, ret, it["dest"], 1, 0)))
    want = (signature(it["out"]), signature(it["ret"]))
    match = next((o for o in opts if (signature(o["out"]), signature(o["ret"])) == want), None)
    if not match:
        return None
    adult, kids = match["total"], len(CHILD_AGES)
    child = (it["total"] - ADULTS * adult) / kids if kids else 0
    if kids and not (0.3 * adult <= child <= 1.1 * adult):
        return None
    return {"method": "measured", "adult": round(adult), "child": round(child)}


def estimated_breakdown(total: float) -> dict:
    ratio = 0.8
    adult = total / (ADULTS + len(CHILD_AGES) * ratio)
    return {"method": "estimated", "adult": round(adult), "child": round(adult * ratio)}


# ---------- dates, state ----------

def daterange(start: dt.date, end: dt.date, step: int, offset: int):
    d = start + dt.timedelta(days=offset % step)
    while d <= end:
        yield d
        d += dt.timedelta(days=step)


def as_date(v) -> dt.date:
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_state() -> dict:
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text())
        s.setdefault("sweeps", s.get("runs", 0))
        s.setdefault("hall", {})
        return s
    return {"sweeps": 0, "results": {}, "lows": {}, "history": [], "breakdowns": {}, "hall": {}}


def write_data(state: dict, fresh: list[dict], pairs, calls: int, stamp: str, prev_low, error: str | None = None) -> None:
    hall = state["hall"]
    DATA_FILE.write_text(json.dumps({
        "updated": stamp, "runs": state["sweeps"], "sweeps": state["sweeps"], "calls_last_run": calls,
        "provider": "scrappa", "error": error,
        "origin": ORIGIN, "cities": CITY, "passengers": {"adults": ADULTS, "child_ages": CHILD_AGES},
        "airline_mode": "any" if ANY_AIRLINE else "list", "airlines": CFG["airlines"], "excluded": sorted(EXCLUDED),
        "rules": RULES, "date_step_days": CFG["date_step_days"],
        "windows": {"depart": [str(CFG["depart_window"]["start"]), str(CFG["depart_window"]["end"])],
                    "return": [str(CFG["return_window"]["start"]), str(CFG["return_window"]["end"])]},
        "all_time": [{**h["it"], "low_total": h["low_total"], "low_at": h["low_at"], "first_seen": h["first_seen"],
                      "last_total": h["last_total"], "last_seen": h["last_seen"], "times_seen": h["times_seen"]}
                     for h in sorted(hall.values(), key=lambda h: h["low_total"])[:HALL_SHOW]],
        "lows": state["lows"], "prev_low": prev_low, "history": state["history"][-120:],
        "checked_pairs": [[str(d), str(r)] for d, r in pairs],
        "results": sorted(fresh, key=lambda x: x["total"])[:80],
    }, indent=1))


# ---------- main ----------

def probe() -> int:
    api = Api()
    dep = as_date(CFG["depart_window"]["start"]) + dt.timedelta(days=3)
    ret = as_date(CFG["return_window"]["start"]) + dt.timedelta(days=7)
    resp = api.get(params_for(dep, ret, DESTS[0], ADULTS, len(CHILD_AGES)))
    PROBE_FILE.write_text(json.dumps(resp, indent=1))
    opts, unknown = parse_options(resp)
    print(f"probe: {len(resp.get('flights') or [])} items, {len(opts)} valid, {unknown} not understood")
    return 0


def main() -> int:
    if "--probe" in sys.argv:
        return probe()
    if not os.environ.get("SCRAPPA_KEY") and not os.environ.get("FARE_WATCH_MOCK"):
        print("Set SCRAPPA_KEY", file=sys.stderr)
        return 1
    state, api = load_state(), Api()
    step, n = CFG["date_step_days"], state["sweeps"]
    deps = daterange(as_date(CFG["depart_window"]["start"]), as_date(CFG["depart_window"]["end"]), step, n % step)
    rets = list(daterange(as_date(CFG["return_window"]["start"]), as_date(CFG["return_window"]["end"]), step, (n // step) % step))
    pairs = [(d, r) for d in deps for r in rets]
    jobs = [(d, r, dest) for d, r in pairs for dest in DESTS]
    print(f"check {n + 1}: {len(pairs)} date pairs x {len(DESTS)} cities = {len(jobs)} searches")

    fresh: list[dict] = []
    unknown_total = items_total = 0
    error = None

    def run(job):
        try:
            return search_one(api, *job)
        except CallCap:
            return [], 0, 0

    try:
        # first search alone: if its format isn't understood, stop before spending more credits
        for i, job in enumerate(jobs):
            found, unk, items = run(job)
            fresh.extend(found); unknown_total += unk; items_total += items
            if items:
                break
        rest = jobs[i + 1:] if not (items_total and unknown_total == items_total) else []
        with ThreadPoolExecutor(max_workers=SEARCH["parallel"]) as pool:
            for found, unk, items in pool.map(run, rest):
                fresh.extend(found)
                unknown_total += unk
                items_total += items
    except OutOfCredits:
        error = "Scrappa says you're out of credits. Buy a credit pack or wait for next month's free 500."
    print(f"  {items_total} options returned, {len(fresh)} kept, {unknown_total} not understood, {api.calls} calls")

    if items_total and unknown_total == items_total:
        PROBE_FILE.write_text(json.dumps(api.first_raw or {}, indent=1))
        error = ("Scrappa's result format wasn't recognised, so no fares were saved. "
                 "A sample was saved as docs/probe.json; send it to get the parser fixed.")

    stamp = now_iso()
    for it in fresh:
        it["seen"] = stamp
        prev = state["results"].get(it["id"])
        it["first_seen"] = prev["first_seen"] if prev else stamp
        it["prev_total"] = prev["total"] if prev else None
        state["results"][it["id"]] = it
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    state["results"] = {k: v for k, v in state["results"].items() if dt.datetime.fromisoformat(v["seen"]) >= cutoff}

    # real adult/child split for the cheapest few
    try:
        for it in sorted(fresh, key=lambda x: x["total"])[: SEARCH["breakdown_top_n"]]:
            c = state["breakdowns"].get(it["id"])
            if c and c.get("total") == it["total"]:
                continue
            bd = real_breakdown(api, it)
            if bd:
                state["breakdowns"][it["id"]] = {**bd, "total": it["total"]}
    except (CallCap, OutOfCredits):
        pass
    state["breakdowns"] = {k: v for k, v in state["breakdowns"].items() if k in state["results"]}
    for it in fresh:
        bd = state["breakdowns"].get(it["id"])
        it["breakdown"] = bd if bd and bd.get("total") == it["total"] else estimated_breakdown(it["total"])

    # all-time lows
    hall = state["hall"]
    for it in fresh:
        h = hall.get(it["id"])
        if h is None or it["total"] < h["low_total"]:
            snap = {k: v for k, v in it.items() if k not in ("prev_total", "new_low", "drop", "seen")}
            hall[it["id"]] = h = {"it": snap, "low_total": it["total"], "low_at": stamp, "first_seen": h["first_seen"] if h else stamp}
        h["last_total"], h["last_seen"] = it["total"], stamp
        h["times_seen"] = h.get("times_seen", 0) + 1
    state["hall"] = {k: hall[k] for k in sorted(hall, key=lambda k: hall[k]["low_total"])[:HALL_KEEP]}

    # highlights
    prev_low = (state["lows"].get("ALL") or {}).get("total")
    for it in fresh:
        it["new_low"] = prev_low is not None and it["total"] < prev_low
        it["drop"] = (it["prev_total"] - it["total"]) if it.get("prev_total") and it["prev_total"] > it["total"] else 0
    if fresh:
        best = min(fresh, key=lambda x: x["total"])
        if prev_low is None or best["total"] < prev_low:
            state["lows"]["ALL"] = {"total": best["total"], "id": best["id"], "at": stamp}

    if fresh or not error:
        state["sweeps"] += 1
    state["history"].append({"at": stamp, "min": min((x["total"] for x in fresh), default=None), "calls": api.calls})
    state["history"] = state["history"][-400:]
    STATE_FILE.write_text(json.dumps(state, indent=1))
    write_data(state, fresh, pairs, api.calls, stamp, prev_low, error)
    print(f"done: {len(fresh)} fares, {api.calls} credits used" + (f"  ERROR: {error}" if error else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
