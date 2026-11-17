"""Replay the paper trader over all the price history saved so far, with no data delay.

    py backtest.py              your current rules
    py backtest.py --compare    your rules next to a few variations (takes several minutes)

It runs the real paper trading code every 5 minutes through history, using the gamma levels that were
in force at each moment (nothing from the future). Your real trades and settings are not touched, and no
phone alerts are sent. Results go to the screen and to data/backtest_trades.csv.

Read the "95% range": it is the spread of average points per trade you could get from luck alone with
this many trades. If it includes 0, there is no evidence of an edge yet. Every saved day makes it narrower.
Do not pick the variation with the best number and stop there: with few trades the best one is usually luck.
"""
import csv
import os
import random
import shutil
import sys
import tempfile

import pandas as pd

import logger
import notify
import paper
import reactions

VARIANTS = {
    "your current rules": {},
    "strict rules only": {"loose_enabled": False},
    "US session only": {"asia_enabled": False},
    "stop 20, target 40": {"stop_pts": 20, "target_pts": 40, "asia_stop_pts": 12, "asia_target_pts": 24},
    "stop 25, target 25": {"stop_pts": 25, "target_pts": 25, "asia_stop_pts": 15, "asia_target_pts": 15},
}


def load_bars():
    parts = [reactions._read_bars(reactions.ALL)]
    if os.path.isdir(logger.BARS):
        parts += [reactions._read_bars(os.path.join(logger.BARS, f)) for f in sorted(os.listdir(logger.BARS)) if f.endswith(".csv")]
    parts = [p for p in parts if p is not None and not p.empty]
    if not parts:
        raise RuntimeError("No saved price history yet.")
    b = pd.concat(parts)
    return b[~b.index.duplicated(keep="last")].sort_index()


class _NoLevels(Exception):
    pass


WINDOW = 600     # bars handed to each scan: enough for orderflow, retests and the longest trade (about 6 hours)


def replay(b, settings, trades0=None, start=None, step=5):
    """Run paper.scan_once every 5 minutes through bars b with the given full settings. Returns the trades.
    trades0 and start let a caller pick up where an earlier replay stopped (only bars after start are scanned).
    Uses a throwaway folder for trades and settings, so the real ones are never touched."""
    tmp = tempfile.mkdtemp(prefix="nq_replay_")
    book = reactions.LevelBook()
    now = {"t": None}

    def day_row(day):
        cand = [r for t, r in book.day.get(day, []) if t <= now["t"]]
        return cand[-1] if cand else None

    def over_row(key):
        for i in range(4):
            r = book.over.get((key - pd.Timedelta(days=i).to_pytimedelta()).isoformat())
            if r:
                return r
        return None

    def no_snapshot(demo=False):
        raise _NoLevels()

    saved = (paper.TRADES, paper.SETTINGS, paper._levels_row_for, paper.overnight_row_for, logger.run_snapshot, notify.send)
    paper.TRADES, paper.SETTINGS = os.path.join(tmp, "trades.csv"), os.path.join(tmp, "settings.json")
    paper._levels_row_for, paper.overnight_row_for, logger.run_snapshot = day_row, over_row, no_snapshot
    notify.send = lambda *a, **k: (True, "")                  # never alert the phone about replayed trades
    try:
        paper.save_settings(settings)
        paper.write_trades(list(trades0 or []))
        first = 0 if start is None else int(b.index.searchsorted(pd.Timestamp(start), side="right"))
        for i in range(first, len(b)):
            t = b.index[i]
            if t.minute % step:
                continue
            now["t"] = t
            try:
                paper.scan_once(bars=b.iloc[max(0, i - WINDOW):i + 1], now=t + pd.Timedelta(seconds=30))
            except _NoLevels:
                pass
        return paper.read_trades()
    finally:
        paper.TRADES, paper.SETTINGS, paper._levels_row_for, paper.overnight_row_for, logger.run_snapshot, notify.send = saved
        shutil.rmtree(tmp, ignore_errors=True)


def stats(trades):
    p = [float(t["pnl_pts"]) for t in trades if t.get("status") == "closed" and t.get("pnl_pts") not in ("", None)]
    if not p:
        return None
    random.seed(1)
    boots = sorted(sum(random.choice(p) for _ in p) / len(p) for _ in range(4000))
    eq = peak = dd = 0.0
    for x in p:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    lo, hi = boots[100], boots[3899]
    if len(p) < 10:
        verdict = "too few trades to tell"
    else:
        verdict = "no evidence of an edge yet" if lo <= 0 <= hi else ("looks positive" if lo > 0 else "looks negative")
    return {"n": len(p), "win": round(100 * sum(1 for x in p if x > 0) / len(p), 1), "total": round(sum(p), 1),
            "avg": round(sum(p) / len(p), 2), "lo": round(lo, 2), "hi": round(hi, 2), "dd": round(dd, 1), "verdict": verdict}


def line(name, s):
    if not s:
        return f"{name:22s} no trades"
    return (f"{name:22s} {s['n']:3d} trades, {s['win']:3.0f}% winners, {s['total']:+7.1f} pts, {s['avg']:+5.1f} per trade "
            f"(95% range {s['lo']:+.1f} to {s['hi']:+.1f}), worst drawdown {s['dd']:.0f} pts: {s['verdict']}")


def main():
    b = load_bars()
    print(f"Price history: {len(b):,} one minute bars, {b.index[0]:%b %d} to {b.index[-1]:%b %d}.")
    base = paper.load_settings()
    names = list(VARIANTS) if "--compare" in sys.argv else ["your current rules"]
    first = None
    for name in names:
        tr = replay(b, {**base, **VARIANTS[name]})
        first = first if first is not None else tr
        print(line(name, stats(tr)))
    print("\nYour current rules, split up:")
    for key, label in (("session", "Session"), ("profile", "Rules"), ("kind", "Setup"), ("side", "Side")):
        groups = {}
        for t in first:
            groups.setdefault(t.get(key) or "-", []).append(t)
        for g, tr in sorted(groups.items()):
            print("  " + line(f"{label}: {g}", stats(tr)))
    out = os.path.join(logger.DATA, "backtest_trades.csv")
    try:
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=paper.FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(first)
        print("\nTrades saved to", out)
    except PermissionError:
        print("\nCould not save backtest_trades.csv (open in Excel?).")


if __name__ == "__main__":
    main()
