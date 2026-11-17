"""Do the gamma levels matter? Measure what price does every time it reaches one.

Not tied to the paper trader's rules, so it collects many more samples than trades do.

A TOUCH: a 1 minute bar comes within TOL points of a level after price was at least FAR points
away 15 minutes earlier (and stayed on that side). The side price came from sets the "bounce"
direction: coming from above, a bounce is up; coming from below, a bounce is down.
OUTCOME (first to happen within an hour, measured from the touch bar's close):
  bounce   price moves THRESH points in the bounce direction first
  break    price moves THRESH points through the level first (a bar that does both counts as a break)
  neither  neither happens within an hour
CONTROLS: the same levels shifted up and down by 80 and 160 points, run through exactly the same
rules at the same moments. If real levels matter, they should bounce more than the controls do.
That comparison also cancels out a market that is simply trending.

Needs bars (data/bars_all.csv, filled by every scan, plus the older bars files) and the levels that
were in force at each time (snapshots, and overnight levels for evenings).
Not advice, and events overlap in time, so treat the numbers as a rough guide until there are many.
"""
import csv
import glob
import json
import math
import os

import safeio
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd

import logger
import orderflow
import paper
import sessions

ALL = os.path.join(logger.DATA, "bars_all.csv")
OUT = os.path.join(logger.DATA, "reactions.json")
TOL, FAR, APPROACH, COOLDOWN = 8.0, 30.0, 15, 45
THRESH, HORIZON = 25.0, 60
OFFSETS = (80.0, -80.0, 160.0, -160.0)
MIN_DECIDED = 30
COLS = ["open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------- bar history
def _read_bars(path):
    try:
        df = pd.read_csv(path, index_col=0)
        df.columns = [str(c).strip().lower() for c in df.columns]
        if not set(COLS) <= set(df.columns):
            return None
        df.index = pd.to_datetime(df.index, utc=True, errors="coerce")
        df = df[~df.index.isna()][COLS].apply(pd.to_numeric, errors="coerce").dropna()
        return df
    except (OSError, ValueError, pd.errors.ParserError, KeyError):
        return None


def store_bars(bars):
    """Add the newest bars to the long-term file (Yahoo only keeps a few days of 1 minute data)."""
    b = paper._normalize(bars)[COLS]
    old = _read_bars(ALL)
    merged = b if old is None or old.empty else pd.concat([old, b])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    merged = merged[merged.index >= merged.index[-1] - pd.Timedelta(days=120)]
    os.makedirs(logger.DATA, exist_ok=True)
    tmp = ALL + ".tmp"
    merged.to_csv(tmp)
    safeio.replace(tmp, ALL)


def load_history():
    """Every bar saved so far: the long-term file plus the per-snapshot files and the latest scan."""
    parts = []
    for path in [ALL] + sorted(glob.glob(os.path.join(logger.BARS, "*.csv"))):
        df = _read_bars(path)
        if df is not None and not df.empty:
            parts.append(df)
    if not parts:
        return pd.DataFrame(columns=COLS, index=pd.DatetimeIndex([], tz="UTC"))
    m = pd.concat(parts)
    return m[~m.index.duplicated(keep="last")].sort_index()


# ---------------------------------------------------------------- the levels in force at each moment
class LevelBook:
    def __init__(self):
        self.day = defaultdict(list)                 # ET date -> [(ts, row)] oldest first
        for path in (logger.SNAP, logger.PENDING):
            try:
                with open(path, newline="", encoding="utf-8-sig") as f:
                    for r in csv.DictReader(f):
                        try:
                            ts = pd.Timestamp(r["ts_utc"])
                            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
                            self.day[sessions.to_et(ts).date()].append((ts, r))
                        except (KeyError, ValueError):
                            continue
            except OSError:
                continue
        for v in self.day.values():
            v.sort(key=lambda x: x[0])
        self.over = logger.read_overnight()
        self._cache = {}

    def _levels(self, row):
        k = id(row)
        if k not in self._cache:
            lv = paper.row_to_levels(row)
            out, used = {}, []
            for key in ("call_wall", "put_wall", "gamma_flip"):
                if lv[key] is not None:
                    out[key] = lv[key]
                    used.append(lv[key])
            for i, s in enumerate(lv["top_strikes"]):
                if not any(abs(s - u) < 1.0 for u in used):
                    out[f"strike{i}"] = s
                    used.append(s)
            self._cache[k] = out
        return self._cache[k]

    def at(self, ts):
        et = sessions.to_et(ts)
        if sessions.levels_kind(ts) == "day":
            cand = [r for t, r in self.day.get(et.date(), []) if t <= ts]
            row = cand[-1] if cand else None
        else:
            base = et.date() if et.hour >= 16 else et.date() - timedelta(days=1)
            row = None
            for i in range(4):
                row = self.over.get((base - timedelta(days=i)).isoformat())
                if row:
                    break
        return self._levels(row) if row else None


# ---------------------------------------------------------------- finding touches and their outcomes
def _group(key):
    return "strike" if key.startswith("strike") else key


def find_events(b, book=None):
    """All touches (real levels and controls) with their outcomes. b: UTC indexed 1 minute bars."""
    book = book or LevelBook()
    idx, hi, lo, cl = b.index, b.high.values, b.low.values, b.close.values
    n = len(b)
    flow_cache, last, events = {}, {}, []

    def flow_ok(i, d):
        if i not in flow_cache:
            flow_cache[i] = orderflow.analyze(b.iloc[max(0, i - 199):i + 1])
        f = flow_cache[i]
        div = f.get("divergence") or ""
        return bool(f["absorption"] or (d == 1 and div.startswith("bullish")) or (d == -1 and div.startswith("bearish")))

    for i in range(APPROACH, n - HORIZON):
        if idx[i] - idx[i - APPROACH] > pd.Timedelta(minutes=APPROACH + 15):
            continue                                                  # a gap (break, weekend): not one continuous approach
        if idx[i + HORIZON] - idx[i] > pd.Timedelta(minutes=HORIZON + 30):
            continue                                                  # the outcome window has a gap
        lv = book.at(idx[i])
        if not lv:
            continue
        ref, path = cl[i - APPROACH], cl[i - APPROACH:i]
        for key, price in lv.items():
            for off in (0.0,) + OFFSETS:
                L = price + off
                if hi[i] < L - TOL or lo[i] > L + TOL:
                    continue
                if ref > L + FAR and path.min() > L - TOL:
                    d = 1                                             # came from above, a bounce is up
                elif ref < L - FAR and path.max() < L + TOL:
                    d = -1                                            # came from below, a bounce is down
                else:
                    continue
                lk = (key, off)
                if i - last.get(lk, -10 ** 9) < COOLDOWN:
                    continue
                last[lk] = i
                p0, res, mf, ma = cl[i], "neither", 0.0, 0.0
                for j in range(i + 1, i + HORIZON + 1):
                    fav = (hi[j] - p0) if d == 1 else (p0 - lo[j])
                    adv = (p0 - lo[j]) if d == 1 else (hi[j] - p0)
                    mf, ma = max(mf, fav), max(ma, adv)
                    if res == "neither":
                        if adv >= THRESH:
                            res = "break"                             # checked first: a bar that does both counts as a break
                        elif fav >= THRESH:
                            res = "bounce"
                events.append({"ts": idx[i].isoformat(timespec="seconds"), "group": _group(key), "real": off == 0.0, "dir": d,
                               "result": res, "fav": mf, "adv": ma, "drift30": (cl[i + 30] - p0) * d,
                               "confirmed": flow_ok(i, d), "session": (sessions.session_at(idx[i], paper.load_settings()) or {}).get("name") or "other"})
    return events


# ---------------------------------------------------------------- summary
def _stat(ev):
    n = len(ev)
    bo = sum(1 for e in ev if e["result"] == "bounce")
    br = sum(1 for e in ev if e["result"] == "break")
    return {"n": n, "bounce": bo, "break": br, "neither": n - bo - br,
            "bouncePct": round(100 * bo / n, 1) if n else None, "breakPct": round(100 * br / n, 1) if n else None,
            "decidedBouncePct": round(100 * bo / (bo + br), 1) if bo + br else None,
            "avgFav": round(sum(e["fav"] for e in ev) / n, 1) if n else None,
            "avgAdv": round(sum(e["adv"] for e in ev) / n, 1) if n else None}


def _pair(real, ctrl):
    return {"real": _stat(real), "control": _stat(ctrl)}


def _verdict(real, ctrl):
    r, c = _stat(real), _stat(ctrl)
    nr, nc = r["bounce"] + r["break"], c["bounce"] + c["break"]
    if nr < MIN_DECIDED:
        return {"kind": "early", "title": "Not enough data yet",
                "text": f"There are {nr} clear touches at real levels so far (a touch ends in a bounce or a break). "
                        f"About {MIN_DECIDED} are needed before the comparison means much. Keep it running."}
    p1, p2 = r["bounce"] / nr, (c["bounce"] / nc if nc else 0.5)
    p = (r["bounce"] + c["bounce"]) / (nr + nc) if nr + nc else 0.5
    se = math.sqrt(max(p * (1 - p) * (1 / nr + (1 / nc if nc else 0)), 1e-9))
    z = (p1 - p2) / se
    diff = round(100 * (p1 - p2), 1)
    base = (f"At real levels price bounced {100 * p1:.0f}% of the time it had a clear result, against {100 * p2:.0f}% at control levels "
            f"(a difference of {diff:+.1f} percentage points).")
    if z >= 2:
        return {"kind": "yes", "z": round(z, 2), "diff": diff, "title": "The levels look like they matter",
                "text": base + " That is more than chance would usually produce, so price does seem to react to the real levels."}
    if z <= -2:
        return {"kind": "reverse", "z": round(z, 2), "diff": diff, "title": "Price breaks real levels more than controls",
                "text": base + " That points the other way: real levels are broken more often than random ones."}
    return {"kind": "no", "z": round(z, 2), "diff": diff, "title": "No clear difference yet",
            "text": base + " That gap is within what chance could produce, so there is no evidence yet that the levels matter."}


def summarize(events, b):
    real = [e for e in events if e["real"]]
    ctrl = [e for e in events if not e["real"]]
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "bars": int(len(b)), "from": b.index[0].isoformat(timespec="seconds") if len(b) else None,
           "to": b.index[-1].isoformat(timespec="seconds") if len(b) else None,
           "days": len({sessions.to_et(t).date() for t in b.index[::30]}) if len(b) else 0,
           "params": {"tol": TOL, "far": FAR, "threshold": THRESH, "horizonMin": HORIZON, "controls": list(OFFSETS)},
           "verdict": _verdict(real, ctrl), "overall": _pair(real, ctrl)}
    out["byLevel"] = {g: _pair([e for e in real if e["group"] == g], [e for e in ctrl if e["group"] == g])
                      for g in ("put_wall", "call_wall", "gamma_flip", "strike")}
    out["byFlow"] = {"confirmed": _pair([e for e in real if e["confirmed"]], [e for e in ctrl if e["confirmed"]]),
                     "unconfirmed": _pair([e for e in real if not e["confirmed"]], [e for e in ctrl if not e["confirmed"]])}
    out["bySession"] = {s: _pair([e for e in real if e["session"] == s], [e for e in ctrl if e["session"] == s])
                        for s in ("us", "asia", "other")}
    return out


def run():
    b = load_history()
    if len(b) < 200:
        raise RuntimeError("Not enough saved price history yet. Scans and snapshots save it, so give it a day or two.")
    res = summarize(find_events(b), b)
    os.makedirs(logger.DATA, exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res, f)
    safeio.replace(tmp, OUT)
    return res


def read():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def stale(hours=6):
    try:
        return (datetime.now().timestamp() - os.path.getmtime(OUT)) > hours * 3600
    except OSError:
        return True


if __name__ == "__main__":
    r = run()
    print(r["verdict"]["title"] + ". " + r["verdict"]["text"])
