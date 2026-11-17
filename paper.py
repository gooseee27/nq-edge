"""Paper trading (fake trades) on top of the gamma + orderflow setups.

No real orders are ever placed. Every trade lives in data/paper_trades.csv.

Rules (all adjustable in the dashboard):
  - Two sessions, US day and Asia (see sessions.py). Each has its own stop, target, minimum score,
    slippage and trade cap. Asia uses overnight gamma levels (see logger.py).
  - A trade can open when a setup scores at least min_score at a gamma level.
  - Put wall: long. Call wall: short. Other levels: follow the delta divergence.
  - Fixed stop and target in NQ points, one trade at a time, a daily trade cap,
    and a cooldown after each trade closes.
  - Everything closes by the session's flat time (15:55 ET for US, 01:55 ET for Asia).
  - Fills assume the bar's high and low. If one bar touches both stop and target,
    the stop is assumed to hit first (the pessimistic choice).
  - Slippage is charged on entries, stops and end of day exits (not on targets).

Limits: Yahoo's NQ data is delayed, so entries are filled at slightly stale prices.
Treat results as a rough guide, not proof.
"""
import csv
import json
import os

import safeio
import uuid
from datetime import timedelta

import pandas as pd

import confluence
import logger
import notify
import orderflow
import sessions

DATA = logger.DATA
TRADES = os.path.join(DATA, "paper_trades.csv")
SETTINGS = os.path.join(DATA, "paper_settings.json")
ET = sessions.ET

DEFAULTS = {
    "stop_pts": 25.0,
    "target_pts": 50.0,
    "min_score": 2,
    "max_trades_per_day": 3,
    "cooldown_min": 45,
    "slippage_pts": 0.5,
    "point_value": 2.0,      # 2.0 = MNQ micro contract, 20.0 = full size NQ
    "contracts": 1,
    "first_entry_et": "09:45",
    "last_entry_et": "15:30",
    "flat_et": "15:55",
    "proximity_pct": sessions.US_PROXIMITY,
    # Second rule set that runs next to the strict one, tracked separately (see find_signal_loose)
    "loose_enabled": True,
    "loose_delta_z": 1.0,        # at a wall, strong one-sided flow alone is enough
    "loose_prox_mult": 1.6,      # counts as "at a level" 1.6x further away
    "retest_enabled": True,      # break and retest setups, for both rule sets
    # "with_regime": in negative gamma only trade in the direction of the last hour's move (dealers
    # amplify moves); in positive gamma only fade it (dealers dampen moves). "off" ignores the regime.
    "regime_mode": "off",
    # "touch": trade the first touch of any gamma level (confluence.touch_setup), no orderflow needed.
    # "orderflow": the original rules (absorption, divergence, retests, loose rules).
    "signal_mode": "orderflow",
}
PROFILES = ("strict", "loose")
DEFAULTS.update(sessions.ASIA_DEFAULTS)

FIELDS = ["id", "opened_utc", "bar_utc", "session", "profile", "kind", "side", "level", "level_price", "entry", "stop", "target",
          "score", "why", "status", "closed_utc", "exit", "reason", "pnl_pts", "pnl_usd"]


# ---------------------------------------------------------------- files
def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    os.makedirs(DATA, exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def read_trades():
    try:
        with open(TRADES, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def write_trades(rows):
    os.makedirs(DATA, exist_ok=True)
    tmp = TRADES + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    safeio.replace(tmp, TRADES)  # raises PermissionError if the file is open in Excel


# ---------------------------------------------------------------- time helpers
_mins = sessions.mins
to_et = sessions.to_et


def _normalize(bars):
    b = bars.copy()
    if b.index.tz is None:
        b.index = b.index.tz_localize("UTC")
    return b


# ---------------------------------------------------------------- trade management
def _close(t, ts, price, reason, s):
    side = 1 if t["side"] == "long" else -1
    fill = price if reason == "target" else price - side * float(s["slippage_pts"])
    pts = (fill - float(t["entry"])) * side
    t.update(status="closed", closed_utc=pd.Timestamp(ts).tz_convert("UTC").isoformat(timespec="seconds"),
             exit=round(fill, 2), reason=reason, pnl_pts=round(pts, 2),
             pnl_usd=round(pts * float(s["point_value"]) * float(s["contracts"]), 2))


def manage(trades, bars, s):
    """Close open trades that hit their stop, target, or their session's flat time. Returns messages."""
    b = _normalize(bars)
    msgs = []
    if b.empty:
        return msgs
    for t in trades:
        if t.get("status") != "open":
            continue
        name = t.get("session") or sessions.US
        e = sessions.effective(s, name)
        opened = pd.Timestamp(t["opened_utc"])
        if opened.tzinfo is None:
            opened = opened.tz_localize("UTC")
        entry_day = to_et(opened).date()
        flat_at = sessions.deadline(name, sessions.session_key(name, opened), s)
        side = 1 if t["side"] == "long" else -1
        stop, target = float(t["stop"]), float(t["target"])
        # Check from the bar the entry price came from, not from when the scan ran. Yahoo runs 10 to 15
        # minutes late, so starting at the scan time skipped whatever price did in between.
        start = pd.Timestamp(t.get("bar_utc") or t["opened_utc"])
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        after = b[b.index > start]
        result, prev_ts, prev_close = None, None, None
        for ts, bar in after.iterrows():
            if ts >= flat_at:
                if ts - flat_at <= pd.Timedelta(minutes=sessions.GAP_MIN):
                    result = (ts, float(bar.open), "end of day")     # a bar right at the flat time
                elif prev_ts is not None:
                    result = (prev_ts, prev_close, "end of day")     # data jumped past it: last known price
                break
            hi, lo = float(bar.high), float(bar.low)
            if side == 1:
                if lo <= stop:
                    result = (ts, stop, "stop")
                    break
                if hi >= target:
                    result = (ts, target, "target")
                    break
            else:
                if hi >= stop:
                    result = (ts, stop, "stop")
                    break
                if lo <= target:
                    result = (ts, target, "target")
                    break
            prev_ts, prev_close = ts, float(bar.close)
        if result:
            _close(t, result[0], result[1], result[2], e)
            msgs.append(f"Paper {t['side']} closed ({t['reason']}): {t['pnl_pts']:+} pts")
        else:
            last_day = to_et(b.index[-1]).date()
            if start < b.index[0] or (after.empty and last_day > entry_day):
                t.update(status="void", reason="no data to settle", closed_utc=b.index[-1].isoformat(timespec="seconds"))
                msgs.append("A paper trade could not be settled (price history too old) and was voided.")
    return msgs


# ---------------------------------------------------------------- signals
def find_signal(setups, flow, s):
    """Returns (side, setup) for the best qualifying setup, or None."""
    div = flow.get("divergence") or ""
    bull, bear = div.startswith("bullish"), div.startswith("bearish")
    absorb = bool(flow.get("absorption"))
    for st in sorted(setups, key=lambda x: -x["score"]):
        if st["score"] < s["min_score"]:
            continue
        if st.get("kind") == "retest":
            return st["side"], st
        lvl = st["level"]
        if lvl == "put_wall":
            side = "long" if (bull or absorb) and not bear else None
        elif lvl == "call_wall":
            side = "short" if (bear or absorb) and not bull else None
        else:
            side = "long" if bull else "short" if bear else None
        if side:
            return side, st
    return None


def find_signal_loose(setups, flow, s, full):
    """The loose rule set. Everything the strict rules take, plus: at a wall, strong one-sided flow alone
    is enough (buying at the put wall, selling at the call wall), and at other levels strong flow sets the
    direction. It ignores the minimum score. Returns (side, setup) or None."""
    sig = find_signal(setups, flow, {**s, "min_score": 0})
    if sig:
        return sig
    z = float(flow.get("delta_z") or 0.0)
    zmin = float(full.get("loose_delta_z", 1.0))
    div = flow.get("divergence") or ""
    bull, bear = div.startswith("bullish"), div.startswith("bearish")
    for st in sorted(setups, key=lambda x: abs(x["distance"])):
        if st.get("kind") == "retest":
            continue
        lvl, side = st["level"], None
        if lvl == "put_wall" and z >= zmin and not bear:
            side = "long"
        elif lvl == "call_wall" and z <= -zmin and not bull:
            side = "short"
        elif lvl not in ("put_wall", "call_wall") and abs(z) >= 1.5:
            side = "long" if z > 0 else "short"
        if side:
            return side, {**st, "notes": list(st["notes"]) + [f"strong flow only (loose rules, z={z:.1f})"]}
    return None


def _with_regime(side, levels, b):
    """True when a trade agrees with the gamma regime (see regime_mode in DEFAULTS)."""
    if len(b) < 62:
        return False
    move = float(b.close.iloc[-1] - b.close.iloc[-61])
    if move == 0:
        return False
    trend = 1 if move > 0 else -1
    want = trend if levels.get("regime") == "negative_gamma" else -trend
    return (1 if side == "long" else -1) == want


def profile_of(t):
    return t.get("profile") or "strict"


def profile_settings(e, profile, full):
    """The session's settings, adjusted for a rule set. Loose counts price as 'at a level' from further away."""
    if profile == "loose":
        return {**e, "proximity_pct": float(e["proximity_pct"]) * float(full["loose_prox_mult"])}
    return e


def gate_reason(trades, profile, name, key, now, s):
    """Why a rule set cannot open a trade right now, or '' if it can. Each rule set has its own limits."""
    mine = [t for t in trades if profile_of(t) == profile]
    if any(t.get("status") == "open" for t in mine):
        return "a trade is already open"
    used = [t for t in mine if t.get("status") != "void" and t.get("opened_utc") and _trade_session(t) == (name, key)]
    if len(used) >= int(s["max_trades_per_day"]):
        return "the session trade limit is used up"
    closed = [pd.Timestamp(t["closed_utc"]) for t in mine if t.get("status") == "closed" and t.get("closed_utc")]
    if closed and (now - max(closed)) < pd.Timedelta(minutes=float(s["cooldown_min"])):
        return "the cooldown is not over"
    return ""


def _open_trade(side, st, price, s, now, session=sessions.US, profile="strict", bar_ts=None):
    d = 1 if side == "long" else -1
    return {
        "id": uuid.uuid4().hex[:8],
        "opened_utc": pd.Timestamp(now).tz_convert("UTC").isoformat(timespec="seconds"),
        "bar_utc": pd.Timestamp(bar_ts).tz_convert("UTC").isoformat(timespec="seconds") if bar_ts is not None else "",
        "session": session, "profile": profile, "kind": st.get("kind", "bounce"),
        "side": side, "level": st["level"], "level_price": st["price"],
        "entry": round(price + d * float(s["slippage_pts"]), 2),
        "stop": round(price - d * float(s["stop_pts"]), 2),
        "target": round(price + d * float(s["target_pts"]), 2),
        "score": st["score"], "why": "; ".join(st["notes"]) or st["lean"],
        "status": "open", "closed_utc": "", "exit": "", "reason": "", "pnl_pts": "", "pnl_usd": "",
    }


# ---------------------------------------------------------------- levels for today
def _levels_row_for(day):
    best = None
    for path in (logger.SNAP, logger.PENDING):
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    try:
                        if to_et(r["ts_utc"]).date() == day:
                            best = r
                    except (KeyError, ValueError):
                        continue
        except OSError:
            continue
    return best


def overnight_row_for(day, max_back=3):
    """Overnight levels saved by the logger: the newest entry on `day` or up to `max_back` days earlier
    (so Sunday night uses Friday's, and a Monday holiday still has something to use)."""
    data = logger.read_overnight()
    for i in range(max_back + 1):
        r = data.get((day - timedelta(days=i)).isoformat())
        if r:
            return r
    return None


def levels_row(kind, ts):
    """The snapshot row that describes the market at `ts`: 'day' levels or 'overnight' levels."""
    et = to_et(ts)
    if kind == "overnight":
        base = et.date() if et.hour >= 16 else et.date() - timedelta(days=1)
        return overnight_row_for(base)
    return _levels_row_for(et.date())


def _fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def row_to_levels(r):
    try:
        strikes = [float(x) for x in json.loads(r.get("top_strikes") or "[]")]
    except ValueError:
        strikes = []
    return {"call_wall": _fnum(r.get("call_wall")), "put_wall": _fnum(r.get("put_wall")),
            "gamma_flip": _fnum(r.get("gamma_flip")), "top_strikes": strikes,
            "regime": r.get("regime") or "unknown"}


# ---------------------------------------------------------------- stats
def _stat_block(trades):
    closed = [t for t in trades if t.get("status") == "closed" and _fnum(t.get("pnl_pts")) is not None]
    pts = sum(_fnum(t["pnl_pts"]) for t in closed)
    usd = sum(_fnum(t.get("pnl_usd")) or 0 for t in closed)
    wins = sum(1 for t in closed if _fnum(t["pnl_pts"]) > 0)
    return {"closed": len(closed), "wins": wins, "pts": pts, "usd": usd,
            "open": sum(1 for t in trades if t.get("status") == "open")}


def stats(trades):
    """Totals, plus the same numbers per session so US and Asia can be judged separately."""
    out = _stat_block(trades)
    out["by_session"] = {name: _stat_block([t for t in trades if (t.get("session") or sessions.US) == name])
                         for name in (sessions.US, sessions.ASIA)}
    out["by_profile"] = {p: _stat_block([t for t in trades if profile_of(t) == p]) for p in PROFILES}
    out["by_kind"] = {k: _stat_block([t for t in trades if (t.get("kind") or "bounce") == k]) for k in ("bounce", "retest", "touch")}
    return out


def _trade_session(t):
    name = t.get("session") or sessions.US
    return name, sessions.session_key(name, t["opened_utc"])


# ---------------------------------------------------------------- the scan
def scan_once(bars=None, row=None, now=None):
    """One pass: settle open trades, then look for a new signal. Returns (message, changed)."""
    s = load_settings()
    if bars is None:
        bars = orderflow.fetch_nq_bars(period="2d", interval="1m")
    b = _normalize(bars)
    if b.empty:
        return "No price data.", False
    try:  # keep the newest bars for the dashboard chart
        os.makedirs(logger.BARS, exist_ok=True)
        b.to_csv(os.path.join(logger.BARS, "latest.csv"))
    except OSError:
        pass
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    last_ts = b.index[-1]
    last_et = to_et(last_ts)

    trades = read_trades()
    msgs = manage(trades, b, s)
    changed = bool(msgs)

    def finish():
        if changed:
            try:
                write_trades(trades)
                for m in msgs:                       # push trade opens and closes to the phone, if set up
                    if m.startswith("Paper "):
                        ok, err = notify.send("NQ paper trade", m)
                        if not ok:
                            logger.log_line("PHONE ALERT FAILED " + err)
            except PermissionError:
                msgs.append("paper_trades.csv is open in another program (Excel?). Close it so trades can be saved.")
        return "; ".join(msgs) or "Nothing to do.", changed

    stale = (now - last_ts) > pd.Timedelta(minutes=45)
    sess = sessions.session_at(last_ts, s)          # which entry window the newest bar is in
    if stale or sess is None:
        return finish()
    name, key = sess["name"], sess["key"]
    e = sessions.effective(s, name)                  # this session's stop, target, score, slippage
    profiles = [p for p in PROFILES if p == "strict" or bool(s["loose_enabled"])]
    eligible = [p for p in profiles if not gate_reason(trades, p, name, key, now, s)]   # each rule set has its own limits
    if not eligible:
        return finish()

    if row is None and name == sessions.US:
        row = _levels_row_for(last_et.date())
        if row is None:
            row, _ = logger.run_snapshot(demo=False)  # no levels yet today: take the morning snapshot now
            msgs.append("Took today's gamma snapshot.")
            changed = True
    elif row is None:
        row = overnight_row_for(key)                  # Asia: the options market is closed, so no new snapshot
        if row is None:
            msgs.append("Asia session: no overnight gamma levels saved yet. They are saved with every snapshot "
                        "taken during US market hours, so this starts after the next one.")
            return finish()
    levels = row_to_levels(row)
    flow = orderflow.analyze(b[["open", "high", "low", "close", "volume"]])
    for profile in eligible:
        pe = profile_settings(e, profile, s)
        if s.get("signal_mode") == "touch":
            if profile != "strict":
                continue                              # the touch rules are one rule set on their own
            tp = confluence.touch_setup(levels, b.close.values[-60:], b.high.values[-60:], b.low.values[-60:])
            sig = (tp["side"], tp) if tp else None
        else:
            setups = confluence.score(levels, flow, proximity_pct=float(pe["proximity_pct"]))
            if s["retest_enabled"]:
                setups = setups + confluence.retest_setups(levels, b.close.values, flow)
            sig = find_signal(setups, flow, pe) if profile == "strict" else find_signal_loose(setups, flow, pe, s)
        if sig:
            side, st = sig
            if s.get("regime_mode") == "with_regime" and not _with_regime(side, levels, b):
                continue
            if profile == "loose" and any(x.get("status") == "open" and x.get("side") == side and
                                          abs(float(x["entry"]) - flow["price"]) <= float(pe["stop_pts"]) for x in trades):
                continue                              # loose never copies a trade already open at about the same price
            t = _open_trade(side, st, flow["price"], pe, now, session=name, profile=profile, bar_ts=last_ts)
            trades.append(t)
            kind = t["kind"] if t["kind"] in ("retest", "touch") else "bounce"
            msgs.append(f"Paper {side} opened at {t['entry']} (stop {t['stop']}, target {t['target']}) "
                        f"off {st['level'].replace('_', ' ')} ({sessions.LABELS[name]}, {profile} rules, {kind})")
            changed = True
    return finish()
