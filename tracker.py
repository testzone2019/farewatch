#!/usr/bin/env python3
"""Fare Watch: polls Google Flights (via SerpApi) for the family trip, applies the
layover/duration rules, writes docs/data.json for the website, and sends an ntfy
push to Android when a new lowest fare appears.

Env vars:  SERPAPI_KEY (required)   NTFY_TOPIC (optional, enables push)
           FARE_WATCH_MOCK=path.json (optional, offline test with a canned response)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
STATE_FILE = ROOT / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"
SERPAPI = "https://serpapi.com/search.json"

CITY = {"MAA": "Chennai", "BLR": "Bengaluru", "BOM": "Mumbai", "COK": "Kochi",
        "TRV": "Thiruvananthapuram", "CCJ": "Kozhikode", "CNN": "Kannur"}

RULES = CFG["rules"]
SEARCH = CFG["search"]
HALL_KEEP = 200     # itineraries remembered for the all-time lows tab
HALL_SHOW = 20
QUICK = CFG.get("quick_check") or {"every_minutes": 15, "watch_top_pairs": 3, "pairs_per_run": 1, "outbound_candidates": 1}
ADULTS = CFG["passengers"]["adults"]
CHILD_AGES = CFG["passengers"]["child_ages"]
ANY_AIRLINE = str(CFG.get("airline_mode", "list")).lower() == "any"
ALLOWED = set(CFG["airlines"])
EXCLUDED = set(CFG.get("exclude_airlines") or [])


class QuotaReached(Exception):
    pass


class Api:
    def __init__(self) -> None:
        self.key = os.environ.get("SERPAPI_KEY", "")
        self.mock = os.environ.get("FARE_WATCH_MOCK")
        self.calls = 0

    def get(self, params: dict) -> dict:
        if self.calls >= SEARCH["max_calls_per_run"]:
            raise QuotaReached
        self.calls += 1
        if self.mock:
            return json.loads(Path(self.mock).read_text())
        r = requests.get(SERPAPI, params={**params, "api_key": self.key}, timeout=90)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            # "no results" is normal for some date pairs; anything else is worth logging
            if "hasn't returned any results" not in data["error"]:
                print(f"  API error: {data['error']}", file=sys.stderr)
            return {}
        return data


# ---------- dates ----------

def daterange(start: dt.date, end: dt.date, step: int, offset: int):
    d = start + dt.timedelta(days=offset % step)
    while d <= end:
        yield d
        d += dt.timedelta(days=step)


def as_date(v) -> dt.date:
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))


# ---------- rules ----------

def carrier(seg: dict) -> str:
    return (seg.get("flight_number") or "").split(" ")[0].upper()


def leg_problem(leg: dict) -> str | None:
    """Return why a one-direction option fails the family's rules, or None if it passes."""
    lays = leg.get("layovers") or []
    stops = len(lays)
    if stops > RULES["max_stops"]:
        return "too many stops"
    if leg.get("total_duration", 10**9) > RULES["max_leg_minutes"]:
        return "over 35 h"
    if stops == 1 and lays[0].get("duration", 0) < RULES["one_stop_min_layover"]:
        return "1-stop layover under 6 h"
    if stops == 2 and any(l.get("duration", 0) < RULES["two_stop_min_layover"] for l in lays):
        return "2-stop layover under 4 h"
    segs = leg.get("flights") or []
    if not segs:
        return "no segments"
    codes = [carrier(s) for s in segs]
    if ANY_AIRLINE:
        bad = [c for c in codes if c in EXCLUDED]
        if bad:
            return f"excluded carrier {','.join(bad)}"
    else:
        foreign = [c for c in codes if c not in ALLOWED]
        if foreign:
            return f"carrier {','.join(foreign)} not on list"
    ext = " ".join(str(e) for e in (leg.get("extensions") or [])).lower()
    if "separate ticket" in ext or "self transfer" in ext or "self-transfer" in ext:
        return "separate tickets"
    # airport change mid-journey (e.g. LHR -> LGW) is a self-transfer; skip it with kids
    for a, b in zip(segs, segs[1:]):
        if a["arrival_airport"]["id"] != b["departure_airport"]["id"]:
            return "airport change during layover"
    return None


def summarize(leg: dict) -> dict:
    segs = leg["flights"]
    return {
        "from": segs[0]["departure_airport"]["id"],
        "to": segs[-1]["arrival_airport"]["id"],
        "depart": segs[0]["departure_airport"]["time"],
        "arrive": segs[-1]["arrival_airport"]["time"],
        "minutes": leg.get("total_duration"),
        "stops": len(leg.get("layovers") or []),
        "segments": [{
            "from": s["departure_airport"]["id"], "to": s["arrival_airport"]["id"],
            "depart": s["departure_airport"]["time"], "arrive": s["arrival_airport"]["time"],
            "airline": s.get("airline"), "flight": s.get("flight_number"),
            "minutes": s.get("duration"), "aircraft": s.get("airplane"),
        } for s in segs],
        "layovers": [{
            "airport": l.get("id"), "name": l.get("name"),
            "minutes": l.get("duration"), "overnight": bool(l.get("overnight")),
        } for l in (leg.get("layovers") or [])],
    }


def signature(leg: dict) -> str:
    return "+".join(s.get("flight_number", "?").replace(" ", "") for s in leg["flights"])


def options(resp: dict) -> list[dict]:
    return (resp.get("best_flights") or []) + (resp.get("other_flights") or [])


# ---------- search ----------

def airline_filter() -> dict:
    if not ANY_AIRLINE:
        return {"include_airlines": ",".join(CFG["airlines"])}
    if EXCLUDED:
        return {"exclude_airlines": ",".join(sorted(EXCLUDED))}
    return {}


def base_params(dep: dt.date, ret: dt.date, adults: int, children: int) -> dict:
    return {
        "engine": "google_flights", "type": "1", "hl": "en",
        "gl": SEARCH["country"], "currency": SEARCH["currency"],
        "departure_id": CFG["origin"], "arrival_id": ",".join(CFG["destinations"]),
        "outbound_date": dep.isoformat(), "return_date": ret.isoformat(),
        "adults": adults, "children": children, "stops": "3",
        **airline_filter(),
        "layover_duration": f"{RULES['two_stop_min_layover']},1800",
        "max_duration": RULES["max_leg_minutes"],
    }


def skyscanner_link(it: dict) -> str:
    d = dt.date.fromisoformat(it["depart_date"]).strftime("%y%m%d")
    r = dt.date.fromisoformat(it["return_date"]).strftime("%y%m%d")
    kids = "%7C".join(str(a) for a in CHILD_AGES)
    return (f"https://www.skyscanner.ca/transport/flights/{CFG['origin'].lower()}/"
            f"{it['dest'].lower()}/{d}/{r}/?adultsv2={ADULTS}&childrenv2={kids}&cabinclass=economy")


def search_pair(api: Api, dep: dt.date, ret: dt.date) -> list[dict]:
    params = base_params(dep, ret, ADULTS, len(CHILD_AGES))
    first = api.get(params)
    outs = [o for o in options(first) if o.get("departure_token") and not leg_problem(o)]
    outs.sort(key=lambda o: o.get("price", 10**9))
    found = []
    for ob in outs[: SEARCH["outbound_candidates"]]:
        second = api.get({**params, "departure_token": ob["departure_token"]})
        rets = [r for r in options(second) if r.get("price") and not leg_problem(r)]
        if not rets:
            continue
        rt = min(rets, key=lambda r: r["price"])
        out_s, ret_s = summarize(ob), summarize(rt)
        it = {
            "id": f"{dep}|{ret}|{signature(ob)}|{signature(rt)}",
            "depart_date": dep.isoformat(), "return_date": ret.isoformat(),
            "dest": out_s["to"], "total": rt["price"],
            "currency": SEARCH["currency"], "out": out_s, "ret": ret_s,
            "airlines": sorted({s["airline"] for s in out_s["segments"] + ret_s["segments"] if s["airline"]}),
            "google_url": (second.get("search_metadata") or {}).get("google_flights_url")
                          or (first.get("search_metadata") or {}).get("google_flights_url"),
        }
        it["skyscanner_url"] = skyscanner_link(it)
        found.append(it)
    return found


def real_breakdown(api: Api, it: dict) -> dict | None:
    """Re-price the same flights for 1 adult to learn the true adult fare,
    then derive the child fare from the family total."""
    dep, ret = as_date(it["depart_date"]), as_date(it["return_date"])
    p = base_params(dep, ret, 1, 0)
    want_out = "+".join(s["flight"].replace(" ", "") for s in it["out"]["segments"])
    want_ret = "+".join(s["flight"].replace(" ", "") for s in it["ret"]["segments"])
    first = api.get(p)
    ob = next((o for o in options(first) if signature(o) == want_out), None)
    if not ob or not ob.get("departure_token"):
        return None
    second = api.get({**p, "departure_token": ob["departure_token"]})
    rt = next((r for r in options(second) if signature(r) == want_ret and r.get("price")), None)
    if not rt:
        return None
    adult = rt["price"]
    kids = len(CHILD_AGES)
    child = (it["total"] - ADULTS * adult) / kids if kids else 0
    if kids and not (0.3 * adult <= child <= 1.1 * adult):
        return None  # prices moved between calls; don't show a nonsense split
    return {"method": "measured", "adult": round(adult), "child": round(child),
            "checked": now_iso()}


def estimated_breakdown(total: float) -> dict:
    ratio = 0.8  # typical long-haul child fare vs adult, used only when not measured
    kids = len(CHILD_AGES)
    adult = total / (ADULTS + kids * ratio)
    return {"method": "estimated", "adult": round(adult), "child": round(adult * ratio)}


# ---------- state, notify, output ----------

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"runs": 0, "sweeps": 0, "quick_runs": 0, "results": {}, "lows": {}, "history": [], "breakdowns": {}}


def send_email(title: str, body: str, it: dict | None) -> bool:
    host, user, pw, to = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "ALERT_EMAIL_TO"))
    if not (host and user and pw and to):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = f"Fare Watch: {title}", user, to
    lines = [body, ""]
    if it:
        bd = it.get("breakdown") or estimated_breakdown(it["total"])
        label = "measured" if bd.get("method") == "measured" else "estimated"
        lines += [f"Per ticket ({label}): adult ${bd['adult']:,} x {ADULTS}, "
                  + ", ".join(f"child age {a} ${bd['child']:,}" for a in CHILD_AGES), ""]
        if it.get("google_url"):
            lines.append(f"Google Flights: {it['google_url']}")
        lines.append(f"Skyscanner: {it['skyscanner_url']}")
    if CFG["notify"].get("site_url"):
        lines.append(f"All fares: {CFG['notify']['site_url']}")
    msg.set_content("\n".join(lines))
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, pw)
            smtp.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as e:
        print(f"email failed: {e}", file=sys.stderr)
        return False


def push(title: str, body: str, it: dict | None = None) -> None:
    sent = send_email(title, body, it)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": "high", "Tags": "airplane"}
        if CFG["notify"].get("site_url"):
            headers["Click"] = CFG["notify"]["site_url"]
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, timeout=20)
            sent = True
        except requests.RequestException as e:
            print(f"push failed: {e}", file=sys.stderr)
    if not sent:
        print(f"[no email/ntfy configured] {title}: {body}")


def describe(it: dict) -> str:
    stops = lambda leg: "direct" if leg["stops"] == 0 else f"{leg['stops']} stop" + ("s" if leg["stops"] > 1 else "")
    return (f"${it['total']:,} {it['currency']} total, YYZ-{it['dest']} ({CITY.get(it['dest'], it['dest'])})\n"
            f"{it['depart_date']} to {it['return_date']}, {', '.join(it['airlines'])}\n"
            f"Out {stops(it['out'])}, back {stops(it['ret'])}")


def sweep_pairs(state: dict) -> list[tuple[dt.date, dt.date]]:
    step = CFG["date_step_days"]
    # departure offset cycles every sweep, return offset every `step` sweeps,
    # so every (departure, return) pair is checked once per step*step sweeps
    n = state["sweeps"]
    dep_off, ret_off = n % step, (n // step) % step
    deps = daterange(as_date(CFG["depart_window"]["start"]), as_date(CFG["depart_window"]["end"]), step, dep_off)
    rets = list(daterange(as_date(CFG["return_window"]["start"]), as_date(CFG["return_window"]["end"]), step, ret_off))
    return [(d, r) for d in deps for r in rets]


def quick_pairs(state: dict) -> list[tuple[dt.date, dt.date]]:
    """The date pairs holding the current cheapest fares, one per quick run, rotating."""
    pairs = []
    for it in sorted(state["results"].values(), key=lambda x: x["total"]):
        p = (as_date(it["depart_date"]), as_date(it["return_date"]))
        if p not in pairs:
            pairs.append(p)
        if len(pairs) >= QUICK["watch_top_pairs"]:
            break
    if not pairs:
        return []
    k = QUICK["pairs_per_run"]
    slot = int(dt.datetime.now(dt.timezone.utc).timestamp() // (QUICK["every_minutes"] * 60))
    start = (slot * k) % len(pairs)
    return [pairs[(start + j) % len(pairs)] for j in range(min(k, len(pairs)))]


def main() -> int:
    if not os.environ.get("SERPAPI_KEY") and not os.environ.get("FARE_WATCH_MOCK"):
        print("Set SERPAPI_KEY", file=sys.stderr)
        return 1
    quick = "--quick" in sys.argv
    state = load_state()
    state.setdefault("sweeps", state.get("runs", 0))
    state.setdefault("quick_runs", 0)
    api = Api()

    if quick:
        pairs = quick_pairs(state)
        if not pairs:
            print("quick: nothing tracked yet; waiting for a full sweep")
            return 0
        SEARCH["outbound_candidates"] = QUICK["outbound_candidates"]
        state["quick_runs"] += 1
    else:
        pairs = sweep_pairs(state)
    print(f"{'quick' if quick else 'sweep'}: {len(pairs)} date pairs")

    fresh: list[dict] = []
    try:
        for dep, ret in pairs:
            got = search_pair(api, dep, ret)
            print(f"  {dep} / {ret}: {len(got)} valid")
            fresh.extend(got)
    except QuotaReached:
        print("  call cap reached, stopping early")

    stamp = now_iso()
    changed = False
    for it in fresh:
        it["seen"] = stamp
        prev = state["results"].get(it["id"])
        it["first_seen"] = prev["first_seen"] if prev else stamp
        if prev and prev["total"] == it["total"]:
            it["prev_total"] = prev.get("prev_total")   # keep the last real change for the badge
        else:
            it["prev_total"] = prev["total"] if prev else None
            changed = True
        state["results"][it["id"]] = it

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=SEARCH["keep_results_hours"])
    state["results"] = {k: v for k, v in state["results"].items()
                        if dt.datetime.fromisoformat(v["seen"]) >= cutoff}
    ranked = sorted(state["results"].values(), key=lambda x: x["total"])

    # real adult/child split for the cheapest few (2 calls each; sweeps only)
    if not quick:
        try:
            for it in sorted(fresh, key=lambda x: x["total"])[: SEARCH["breakdown_top_n"]]:
                cached = state["breakdowns"].get(it["id"])
                if cached and cached.get("total") == it["total"]:
                    continue
                bd = real_breakdown(api, it)
                if bd:
                    state["breakdowns"][it["id"]] = {**bd, "total": it["total"]}
        except QuotaReached:
            pass
    live_ids = set(state["results"])
    state["breakdowns"] = {k: v for k, v in state["breakdowns"].items() if k in live_ids}
    for it in ranked:
        bd = state["breakdowns"].get(it["id"])
        it["breakdown"] = bd if bd and bd.get("total") == it["total"] else estimated_breakdown(it["total"])

    # all-time lows: every itinerary ever found, kept at its cheapest price
    hall = state.setdefault("hall", {})
    for it in fresh:
        h = hall.get(it["id"])
        if h is None or it["total"] < h["low_total"]:
            snap = {k: v for k, v in it.items() if k not in ("prev_total", "new_low", "drop", "seen")}
            hall[it["id"]] = h = {"it": snap, "low_total": it["total"], "low_at": stamp,
                                  "first_seen": h["first_seen"] if h else stamp}
        h["last_total"], h["last_seen"] = it["total"], stamp
        h["times_seen"] = h.get("times_seen", 0) + 1
    keep = sorted(hall, key=lambda k: hall[k]["low_total"])[: HALL_KEEP]
    state["hall"] = hall = {k: hall[k] for k in keep}

    # highlights for the page: beats the previous overall low / cheaper than last time seen
    lows = state["lows"]
    prev_low = (lows.get("ALL") or {}).get("total")
    for it in fresh:
        it["new_low"] = prev_low is not None and it["total"] < prev_low
        it["drop"] = (it["prev_total"] - it["total"]) if it.get("prev_total") and it["prev_total"] > it["total"] else 0

    # alerts (only if email/ntfy secrets are set): new lowest overall, and per destination
    min_drop = CFG["notify"]["min_drop"]
    first_run = not lows
    if fresh:
        best = min(fresh, key=lambda x: x["total"])
        old = lows.get("ALL")
        if old is None or best["total"] <= old["total"] - min_drop:
            title = "Fare Watch is running" if first_run else "New lowest fare to India"
            push(title, describe(best) + ("" if old is None else f"\nWas ${old['total']:,}"), best)
            lows["ALL"] = {"total": best["total"], "id": best["id"], "at": stamp}
            changed = True
        for dest in {x["dest"] for x in fresh}:
            b = min((x for x in fresh if x["dest"] == dest), key=lambda x: x["total"])
            o = lows.get(dest)
            if o is None or b["total"] <= o["total"] - min_drop:
                if o is not None and b["id"] != lows["ALL"]["id"] and CFG["notify"].get("per_city_alerts", False):
                    push(f"New low to {CITY.get(dest, dest)}", describe(b) + f"\nWas ${o['total']:,}", b)
                lows[dest] = {"total": b["total"], "id": b["id"], "at": stamp}
                changed = True

    state["calls_month"] = month_calls(state, api.calls)
    state["last_check"] = stamp
    if quick and not changed:
        # nothing moved: write nothing, so the repo doesn't get a commit every quick check
        print(f"quick: no price change, {api.calls} API calls")
        return 0

    if not quick:
        state["sweeps"] += 1
    state["runs"] = state["sweeps"] + state["quick_runs"]
    state["history"].append({"at": stamp, "min": min((x["total"] for x in fresh), default=None),
                             "calls": api.calls, "kind": "quick" if quick else "sweep"})
    state["history"] = state["history"][-400:]
    STATE_FILE.write_text(json.dumps(state, indent=1))

    DATA_FILE.write_text(json.dumps({
        "updated": stamp, "runs": state["runs"], "sweeps": state["sweeps"],
        "calls_last_run": api.calls, "calls_month": state["calls_month"],
        "quick_every_min": QUICK["every_minutes"],
        "origin": CFG["origin"], "cities": CITY,
        "passengers": {"adults": ADULTS, "child_ages": CHILD_AGES},
        "airline_mode": "any" if ANY_AIRLINE else "list",
        "airlines": CFG["airlines"], "excluded": sorted(EXCLUDED), "rules": RULES,
        "windows": {"depart": [str(CFG["depart_window"]["start"]), str(CFG["depart_window"]["end"])],
                    "return": [str(CFG["return_window"]["start"]), str(CFG["return_window"]["end"])]},
        "all_time": [{**h["it"], "low_total": h["low_total"], "low_at": h["low_at"],
                      "first_seen": h["first_seen"], "last_total": h["last_total"],
                      "last_seen": h["last_seen"], "times_seen": h["times_seen"]}
                     for h in sorted(hall.values(), key=lambda h: h["low_total"])[:HALL_SHOW]],
        "lows": lows, "prev_low": prev_low, "history": state["history"][-120:],
        "checked_pairs": [[str(d), str(r)] for d, r in pairs],
        # this check's results only, so the top 3 reflect what's bookable right now
        "results": sorted(fresh, key=lambda x: x["total"])[:80],
    }, indent=1))
    print(f"done: {len(fresh)} checked, {len(ranked)} tracked, {api.calls} API calls")
    return 0


def month_calls(state: dict, calls: int) -> dict:
    ym = dt.date.today().strftime("%Y-%m")
    mc = state.get("calls_month") or {}
    if mc.get("month") != ym:
        mc = {"month": ym, "calls": 0}
    mc["calls"] += calls
    return mc


if __name__ == "__main__":
    sys.exit(main())
