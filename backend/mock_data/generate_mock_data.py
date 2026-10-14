#!/usr/bin/env python3
"""
Mock data for the PumpWise demo business (PLAN.md Phase 12). Standard library only; deterministic for a given seed.

    python mock_data/generate_mock_data.py --end 2026-10-13 --days 30 --seed 7

writes mock_data/seed_demo.json. `manage.py seed_demo` loads that file through the real services (so Row-Level
Security, pricing, reconciliation and rollups all behave exactly as for a real customer), and the nightly job
regenerates it with --end set to yesterday so the demo always looks live.

What is in it: two stations in Harare. Station 1 runs one attendant per shift with shared pumps; station 2 runs two
attendants with assigned pumps (the paid features). One shift per day per station, dips at 05:30 and 15:00, deliveries
when a tank runs low, a price rise 10 days ago, cash in USD and ZiG (ZWG), EcoCash, card and credit. Planted on purpose:
an unexplained fuel loss on the last day (station 1 petrol) and a cash difference of -4% the day before (station 1).

Times are local station times ("YYYY-MM-DDTHH:MM"). The "live" block is relative to the moment of loading (minutes
before now): a shift that closed today at station 2 and a shift open right now at station 1.
"""
import argparse
import json
import random
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

D = Decimal
VERSION = 1
LOSS_DAYS_BEFORE_END, CASH_DAYS_BEFORE_END = 0, 1  # inside the alert look-back window, so a fresh demo shows live alerts
PLANTED_LOSS_LITRES = D(380)
CASH_DIFFERENCE = D("0.04")
WEEKDAY_FACTOR = {4: 1.25, 5: 1.4, 6: 0.8}  # Fri, Sat busy; Sun quiet
ZWG_RATE = D("0.037")  # 1 ZWG = 0.037 USD
CALIBRATION = [[0, 0], [400, 6000], [800, 13500], [1200, 20000]]
PUMPS = (("Pump 1", "PETROL"), ("Pump 2", "PETROL"), ("Pump 3", "DIESEL"))
STAFF = [
    {"key": "manager", "role": "manager", "full_name": "Demo Manager"},
    {"key": "att1", "role": "attendant", "full_name": "Demo Attendant"},
    {"key": "att2", "role": "attendant", "full_name": "Demo Attendant Two"},
]


def _at(day: date, hour: int, minute: int = 0) -> str:
    return datetime(day.year, day.month, day.day, hour, minute).strftime("%Y-%m-%dT%H:%M")


def _q(x, places="0.01"):
    return x.quantize(D(places))


def cash_lines(handed: Decimal):
    """Split what one person hands in across payment types and currencies (5% of it in ZiG)."""
    zwg_part = _q(handed * D("0.05"))
    lines = [
        {"payment_type": "cash", "currency": "USD", "amount": _q(handed * D("0.55"))},
        {"payment_type": "cash", "currency": "ZWG", "amount": _q(zwg_part / ZWG_RATE)},
        {"payment_type": "mobile_money", "currency": "USD", "amount": _q(handed * D("0.25"))},
        {"payment_type": "card", "currency": "USD", "amount": _q(handed * D("0.10"))},
    ]
    used = sum(c["amount"] * (ZWG_RATE if c["currency"] == "ZWG" else 1) for c in lines)
    lines.append({"payment_type": "credit", "currency": "USD", "amount": _q(max(handed - used, D(0)))})
    return [{**c, "amount": str(c["amount"])} for c in lines]


def generate(*, end: date, days: int = 30, seed: int = 7, name: str = "Demo Fuel Services") -> dict:
    rnd = random.Random(seed)
    first = end - timedelta(days=days - 1)
    price_rise = end - timedelta(days=9)  # "10 days ago" when loaded the day after `end`
    stations, state, events = [], {}, []

    for idx, sname in enumerate(("Harare CBD", "Borrowdale")):
        key = f"s{idx + 1}"
        assigned = idx == 1
        tanks = [{"key": f"{key}-{code}", "name": f"{code.title()} tank", "fuel": code, "capacity": "20000", "reserve": "1500", "calibration": CALIBRATION}
                 for code in ("PETROL", "DIESEL")]
        pumps, meters, holders = [], {}, {}
        for pname, code in PUMPS:
            pkey = f"{key}-{pname.split()[-1]}"
            nozzles = []
            for number in (1, 2):
                meters[f"{pkey}:{number}"] = D(rnd.randint(1000, 50000))
                nozzles.append({"number": number, "meter": str(meters[f"{pkey}:{number}"])})
            pumps.append({"key": pkey, "name": pname, "tank": f"{key}-{code}", "brand": rnd.choice(["Wayne", "Gilbarco", "Tokheim"]),
                          "model": f"M{rnd.randint(100, 900)}", "year": rnd.randint(2008, 2020), "nozzles": nozzles})
            holders[pkey] = "att2" if (assigned and pname == "Pump 3") else "att1"
        prices = []
        for code, (old, new) in {"PETROL": ("1.45", "1.52"), "DIESEL": ("1.50", "1.55")}.items():
            prices.append({"fuel": code, "currency": "USD", "price": old, "from": _at(first - timedelta(days=10), 0)})
            prices.append({"fuel": code, "currency": "USD", "price": new, "from": _at(price_rise, 0)})
        stations.append({
            "key": key, "name": sname, "address": f"{sname}, Harare", "pricing_currency": "USD",
            "attendants_mode": "multiple" if assigned else "single", "assignment_mode": "assigned" if assigned else "shared",
            "tanks": tanks, "pumps": pumps, "prices": prices,
        })
        state[key] = {"idx": idx, "assigned": assigned, "pumps": pumps, "meters": meters, "holders": holders,
                      "levels": {f"{key}-PETROL": D(15000), f"{key}-DIESEL": D(6500)}}  # diesel starts low: a delivery on day one

    def price(fuel, when: date):
        return D({"PETROL": ("1.45", "1.52"), "DIESEL": ("1.50", "1.55")}[fuel][1 if when >= price_rise else 0])

    def shift(s, start_at, end_at, when: date, factor, short_frac):
        st, start_readings, end_readings = state[s["key"]], {}, {}
        sold_by_tank, expected = {}, {}
        for p in st["pumps"]:
            fuel = p["tank"].rsplit("-", 1)[1]
            for n in p["nozzles"]:
                nk = f"{p['key']}:{n['number']}"
                start_readings[nk] = str(st["meters"][nk])
                base = rnd.uniform(250, 450) if fuel == "PETROL" else rnd.uniform(300, 480)
                lit = D(str(round(base * factor * rnd.uniform(0.85, 1.15), 2)))
                st["meters"][nk] += lit
                end_readings[nk] = str(st["meters"][nk])
                sold_by_tank[p["tank"]] = sold_by_tank.get(p["tank"], D(0)) + lit
                who = st["holders"][p["key"]]
                expected[who] = expected.get(who, D(0)) + lit * price(fuel, when)
        cash = {who: cash_lines(_q(amount * (D(str(round(rnd.uniform(0.999, 1.001), 4))) - short_frac))) for who, amount in sorted(expected.items())}
        ev = {"type": "shift", "station": s["key"], "start": start_at, "end": end_at, "lead": "att1",
              "attendants": ["att2"] if st["assigned"] else [], "start_readings": start_readings, "end_readings": end_readings, "cash": cash}
        if st["assigned"]:
            ev["assignments"] = dict(st["holders"])
        return ev, sold_by_tank

    for i in range(days):
        day = first + timedelta(days=i)
        factor = WEEKDAY_FACTOR.get(day.weekday(), 1.0)
        for s in stations:
            st = state[s["key"]]
            for t in s["tanks"]:
                noisy = _q(st["levels"][t["key"]] + D(str(round(rnd.uniform(-5, 5), 1))), "0.1")
                events.append({"type": "dip", "tank": t["key"], "at": _at(day, 5, 30), "litres": str(noisy)})
                if st["levels"][t["key"]] < 7000:
                    events.append({"type": "delivery", "tank": t["key"], "at": _at(day, 9), "litres": "12000", "supplier": "Zuva Petroleum",
                                   "note_number": f"ZP-{rnd.randint(10000, 99999)}"})
                    st["levels"][t["key"]] += 12000
            short = CASH_DIFFERENCE if (st["idx"] == 0 and day == end - timedelta(days=CASH_DAYS_BEFORE_END)) else D(0)
            ev, sold = shift(s, _at(day, 6), _at(day, 14), day, factor, short)
            events.append(ev)
            for t in s["tanks"]:
                planted = st["idx"] == 0 and day == end - timedelta(days=LOSS_DAYS_BEFORE_END) and t["fuel"] == "PETROL"
                loss = PLANTED_LOSS_LITRES if planted else D(str(round(rnd.uniform(0, 0.002) * float(sold.get(t["key"], 0)), 2)))
                st["levels"][t["key"]] = st["levels"][t["key"]] - sold.get(t["key"], D(0)) - loss
                events.append({"type": "dip", "tank": t["key"], "at": _at(day, 15), "litres": str(_q(st["levels"][t["key"]], "0.1"))})

    # live: relative to when the data is loaded (minutes before now)
    s1, s2 = stations
    today = end + timedelta(days=1)
    live = [{"type": "dip", "tank": t["key"], "minutes_ago": 310, "litres": str(_q(state["s2"]["levels"][t["key"]], "0.1"))} for t in s2["tanks"]]
    ev, sold = shift(s2, None, None, today, 0.5, D(0))
    live.append({**ev, "start_minutes_ago": 300, "end_minutes_ago": 60})
    for t in s2["tanks"]:
        state["s2"]["levels"][t["key"]] -= sold.get(t["key"], D(0))
        live.append({"type": "dip", "tank": t["key"], "minutes_ago": 50, "litres": str(_q(state["s2"]["levels"][t["key"]], "0.1"))})
    live += [{"type": "dip", "tank": t["key"], "minutes_ago": 65, "litres": str(_q(state["s1"]["levels"][t["key"]], "0.1"))} for t in s1["tanks"]]
    live.append({"type": "open_shift", "station": "s1", "start_minutes_ago": 60, "lead": "att1",
                 "start_readings": {k: str(v) for k, v in state["s1"]["meters"].items()}})

    return {
        "version": VERSION, "end": end.isoformat(), "days": days, "seed": seed,
        "business": {"name": name, "summary_phone": "+263770000001"},
        "staff": STAFF,
        "exchange_rates": [{"from_currency": "ZWG", "to_currency": "USD", "rate": str(ZWG_RATE), "effective_from": _at(first - timedelta(days=20), 0)}],
        "stations": stations, "events": events, "live": live,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--end", type=date.fromisoformat, default=date.today() - timedelta(days=1), help="last full day (default: yesterday)")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--name", default="Demo Fuel Services")
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("seed_demo.json"))
    args = parser.parse_args(argv)
    data = generate(end=args.end, days=args.days, seed=args.seed, name=args.name)
    args.out.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {args.out} ({len(data['events'])} events, {args.days} days to {args.end})")


if __name__ == "__main__":
    main()
