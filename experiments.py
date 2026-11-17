"""Locked experiments: rule ideas written down first and judged only on what happens afterward.

Each experiment is frozen the first time this runs: its full settings are saved together with the date
and time ("locked"). From then on it is scored only on trades the backtest replay opens AFTER that moment,
so the result cannot have been fitted to data it has already seen. Nothing here places paper trades or
sends alerts. It replays the saved price history in the background (every few hours from the scan, or
on demand from the dashboard).

    py experiments.py     update and print the results

To change an idea, add a new experiment instead of editing a locked one (editing it would undo the point).
"""
import json
import os

import safeio
from datetime import datetime, timezone

import pandas as pd

import backtest
import logger
import paper

FILE = os.path.join(logger.DATA, "experiments.json")
WARMUP = pd.Timedelta(days=3)          # history before the lock, so indicators and open trades are set up

IDEAS = [
    ("baseline", "Your rules as of the lock",
     "Your settings, frozen. The fair yardstick for the others.", {}),
    ("us_only", "US session only",
     "Same rules with the Asia session off. Asia was the weakest part of the first replay.", {"asia_enabled": False}),
    ("with_regime", "Trade with the gamma regime",
     "In negative gamma, only trades in the direction of the last hour's move (dealer hedging amplifies moves). "
     "In positive gamma, only trades against it (hedging dampens moves).", {"regime_mode": "with_regime"}),
    ("first_two_hours", "First two hours of the US day",
     "US session only, and new trades only from 9:45 to 11:30 AM New York time, when hedging flow is usually strongest.",
     {"asia_enabled": False, "last_entry_et": "11:30"}),
    # Added Nov 9. A replay of the saved history (Oct 12 to Nov 9) found the profit concentrated in the strict
    # rules during the US session, and higher when checked every minute instead of every 5. That came from about 15
    # variations tried on the same data, so it may be luck: only what happens after the lock counts.
    ("focus_us_1m", "Strict rules, US only, checked every minute",
     "Orderflow confirmation at a gamma level (the strict rules), US day session only, no loose rules, "
     "with the market checked every minute.",
     {"loose_enabled": False, "asia_enabled": False, "scan_step": 1}),
    ("focus_us_5m", "Strict rules, US only, checked every 5 minutes",
     "The same as above but checked every 5 minutes like your scan does now. Comparing the two shows whether "
     "checking more often is what helps.",
     {"loose_enabled": False, "asia_enabled": False, "scan_step": 5}),
]


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load():
    try:
        with open(FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("experiments"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"experiments": [], "results": {}, "generated": None}


def save(d):
    os.makedirs(logger.DATA, exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    safeio.replace(tmp, FILE)


def ensure():
    """Lock any built-in idea that is not locked yet, using the current settings plus its changes."""
    d = load()
    have = {e["id"] for e in d["experiments"]}
    base = paper.load_settings()
    added = False
    for key, name, idea, changes in IDEAS:
        if key not in have:
            d["experiments"].append({"id": key, "name": name, "idea": idea, "locked_utc": _now(),
                                     "changes": changes, "settings": {**base, **changes}})
            added = True
    if added:
        save(d)
    return d


def run():
    d = ensure()
    b = backtest.load_bars()
    results, state = {}, d.setdefault("state", {})
    for e in d["experiments"]:
        lock = pd.Timestamp(e["locked_utc"])
        after = b[b.index >= lock]
        if len(after) < 30:
            results[e["id"]] = {"stats": None, "trades": 0, "since": e["locked_utc"], "bars": int(len(after))}
            continue
        settings = {**paper.DEFAULTS, **e["settings"]}       # keys added in later versions get their defaults
        prev = state.get(e["id"]) or {}
        start = pd.Timestamp(prev["last_bar"]) if prev.get("last_bar") else lock - WARMUP
        window = b[b.index >= start - pd.Timedelta(days=1)]  # a day of lead-in for indicators and open trades
        all_tr = backtest.replay(window, settings, trades0=prev.get("trades") or [], start=start if prev else None,
                                 step=int(settings.get("scan_step", 5)))
        state[e["id"]] = {"trades": all_tr, "last_bar": b.index[-1].isoformat()}   # next run only scans newer bars
        tr = [t for t in all_tr if pd.Timestamp(t["opened_utc"]) >= lock]
        results[e["id"]] = {"stats": backtest.stats(tr), "trades": len(tr), "since": e["locked_utc"], "bars": int(len(after)),
                            "days": len({paper.to_et(t).date() for t in after.index[::60]})}
    d["results"], d["generated"] = results, _now()
    save(d)
    return d


def stale(hours=6):
    try:
        return (datetime.now().timestamp() - os.path.getmtime(FILE)) > hours * 3600 or not load().get("generated")
    except OSError:
        return True


def summary_line(e, r):
    s = (r or {}).get("stats")
    if not s:
        return f"{e['name']:32s} no trades yet since {e['locked_utc'][:10]}"
    return backtest.line(e["name"][:22], s)


if __name__ == "__main__":
    d = run()
    for e in d["experiments"]:
        print(summary_line(e, d["results"].get(e["id"])))
