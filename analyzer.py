"""Plain-English read of the current market, a score, and what would trigger an entry.

Rules based and fully deterministic. It describes the situation with the same rules the paper
trader uses (paper.py and confluence.py), so the two never disagree. It is a description of
conditions, not a prediction and not advice.
"""
import json
import os

import safeio

import pandas as pd

import confluence
import logger
import orderflow
import paper
import sessions

LIVE = os.path.join(logger.DATA, "live.json")
STALE_MIN = 45


def _n(x, d=0):
    return f"{x:,.{d}f}"


def _ptxt(x):
    return f"{abs(x):,.0f} point{'' if round(abs(x)) == 1 else 's'}"


def _lvl(key):
    """Plain name for a level key, for sentences."""
    if str(key).startswith("gex_strike"):
        return "high gamma strike"
    return str(key).replace("_", " ")


def _named(levels):
    out = []
    for key, label in (("call_wall", "Call wall"), ("put_wall", "Put wall"), ("gamma_flip", "Gamma flip")):
        v = levels.get(key)
        if v is not None:
            out.append((key, label, v))
    for i, v in enumerate(levels.get("top_strikes") or []):
        if not any(abs(v - o[2]) < 1 for o in out):
            out.append((f"gex_strike_{i + 1}", "High gamma strike", v))
    return out


def _flow_words(flow):
    z = flow["delta_z"]
    if z >= 1.5:
        pressure = "strong buying pressure"
    elif z >= 0.5:
        pressure = "mild buying pressure"
    elif z <= -1.5:
        pressure = "strong selling pressure"
    elif z <= -0.5:
        pressure = "mild selling pressure"
    else:
        pressure = "balanced buying and selling"
    bits = [f"Recent flow shows {pressure}."]
    if flow["absorption"]:
        bits.append("Volume is heavy while price barely moves (absorption), which can mean one side is soaking up the other.")
    div = flow.get("divergence") or ""
    if div.startswith("bullish"):
        bits.append("Price has been falling while buying edges up (bullish divergence).")
    elif div.startswith("bearish"):
        bits.append("Price has been rising while buying fades (bearish divergence).")
    if not flow["absorption"] and not div:
        bits.append("No absorption or divergence right now.")
    return " ".join(bits)


def _timing_note(mins, name=sessions.US):
    if name == sessions.ASIA:
        return ("It is the Asia session. Volume is usually thin, so moves can be jumpy. The options market is closed, "
                "so the gamma levels are carried over from the US close and are not updated until tomorrow.")
    if 9 * 60 + 30 <= mins < 10 * 60:
        return "It is the opening half hour, usually the most volatile stretch, so signals tend to be noisier."
    if 11 * 60 + 30 <= mins < 13 * 60 + 30:
        return "It is the midday lull, when volume tends to thin out and moves get choppier."
    if 15 * 60 <= mins < 16 * 60:
        return "It is the final hour, often busier as traders position for the close. The paper trader stops opening trades at 3:30 PM ET."
    return ""


def analyze(bars, row, settings=None, trades=None, now=None):
    s = settings or paper.load_settings()
    trades = trades if trades is not None else paper.read_trades()
    b = paper._normalize(bars)[["open", "high", "low", "close", "volume"]]
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    last_ts = b.index[-1]
    price = float(b.close.iloc[-1])
    age = (now - last_ts).total_seconds() / 60
    now_et = paper.to_et(now)
    mins = now_et.hour * 60 + now_et.minute

    # which session is open now (or, if none, the next one), and that session's own rules
    sess_now = sessions.session_at(now, s)
    nxt = None if sess_now else sessions.next_window(now, s)
    name = sess_now["name"] if sess_now else (nxt["name"] if nxt else sessions.US)
    e = sessions.effective(s, name)
    in_window = sess_now is not None
    prox = price * float(e["proximity_pct"])
    levels_kind = sessions.levels_kind(last_ts)

    out = {
        "asOf": last_ts.isoformat(timespec="seconds"), "generated": now.isoformat(timespec="seconds"),
        "ageMin": round(age, 1), "price": round(price, 2),
        "settings": {"stop": e["stop_pts"], "target": e["target_pts"], "pointValue": s["point_value"],
                     "contracts": s["contracts"], "minScore": e["min_score"], "proximity": round(prox, 1)},
        "levelsFrom": row.get("ts_utc") if row else None,
        "session": {"name": name, "label": sessions.LABELS[name], "active": in_window,
                    "next": nxt["text"] if nxt else None, "levels": levels_kind},
    }
    if not row:
        if name == sessions.ASIA or levels_kind == "overnight":
            v = {"kind": "nodata", "title": "No overnight gamma levels yet",
                 "text": ("The Asia session needs overnight levels, which are saved with each snapshot taken during US market hours. "
                          "The next snapshot during the US day will create them.")}
        else:
            v = {"kind": "nodata", "title": "No gamma levels for today yet",
                 "text": "Take a snapshot to load today's levels. Without them there is nothing to compare price against."}
        out.update(verdict=v, score=None, story=[], scenarios=[], gates=[])
        return out

    levels = paper.row_to_levels(row)
    flow = orderflow.analyze(b)
    bounce_setups = confluence.score(levels, flow, proximity_pct=float(e["proximity_pct"]))
    retests = confluence.retest_setups(levels, b.close.values, flow) if s.get("retest_enabled", True) else []
    setups = bounce_setups + retests
    named = _named(levels)
    out["flow"] = {"deltaZ": round(flow["delta_z"], 2), "absorption": bool(flow["absorption"]),
                   "divergence": flow.get("divergence") or "", "volRatio": round(flow["vol_ratio"], 2)}
    out["setups"] = setups
    div = flow.get("divergence") or ""
    bull, bear = div.startswith("bullish"), div.startswith("bearish")

    # ---- score
    parts = [
        {"label": "Absorption", "hint": "heavy volume, little movement", "pts": 2 if flow["absorption"] else 0, "max": 2},
        {"label": "Delta divergence", "hint": "price and buying disagree", "pts": 2 if div else 0, "max": 2},
        {"label": "One sided pressure", "hint": "unusually strong buying or selling", "pts": 1 if abs(flow["delta_z"]) > 1.5 else 0, "max": 1},
    ]
    score = sum(p["pts"] for p in parts)
    near = sorted(bounce_setups, key=lambda x: abs(x["distance"]))
    out["score"] = {"value": score, "max": 5, "need": e["min_score"], "parts": parts, "nearLevel": bool(near)}

    # ---- gates the paper trader applies before opening a trade
    strict_trades = [t for t in trades if paper.profile_of(t) == "strict"]    # the loose rules have their own limits
    open_trade = any(t.get("status") == "open" for t in strict_trades)
    if sess_now:
        used = [t for t in strict_trades if t.get("status") != "void" and t.get("opened_utc")
                and paper._trade_session(t) == (sess_now["name"], sess_now["key"])]
    else:
        used = []
    under_cap = len(used) < int(s["max_trades_per_day"])
    closed_times = [pd.Timestamp(t["closed_utc"]) for t in strict_trades if t.get("status") == "closed" and t.get("closed_utc")]
    since = (now - max(closed_times)).total_seconds() / 60 if closed_times else None
    cooled = since is None or since >= float(s["cooldown_min"])
    fresh = age <= STALE_MIN or not in_window
    clock = now_et.strftime("%I:%M %p").lstrip("0")
    if in_window:
        win_detail = (f"The {sessions.LABELS[name]} is open. New trades can start until "
                      f"{sessions.fmt12(e['last_entry_et'])} ET. It is {clock} ET.")
    else:
        win_detail = (f"{nxt['text']}." if nxt else "No session is scheduled.") + f" It is {clock} ET."
    gates = [
        {"label": "Inside the entry window", "ok": in_window, "detail": win_detail},
        {"label": "No paper trade already open", "ok": not open_trade, "detail": "One trade at a time."},
        {"label": "Under the session trade limit", "ok": under_cap,
         "detail": f"{len(used)} of {s['max_trades_per_day']} used this session."},
        {"label": "Cooldown finished", "ok": cooled,
         "detail": "No recent trade." if since is None else f"Last trade closed {since:,.0f} minutes ago, needs {s['cooldown_min']:g}."},
        {"label": "Price data is current", "ok": fresh, "detail": f"Latest price is {age:,.0f} minutes old."},
    ]
    gates_ok = all(g["ok"] for g in gates)
    out["gates"] = gates

    # ---- entry scenarios
    def plan(side):
        d = 1 if side == "long" else -1
        risk = float(e["stop_pts"]) * float(s["point_value"]) * float(s["contracts"])
        return {"side": side, "entry": round(price, 2), "stop": round(price - d * float(e["stop_pts"]), 2),
                "target": round(price + d * float(e["target_pts"]), 2),
                "rewardRisk": round(float(e["target_pts"]) / float(e["stop_pts"]), 1), "riskUsd": round(risk, 2)}

    def scenario(key, side, name, confirms, confirm_ok, confirm_detail):
        lvl = levels.get(key)
        if lvl is None:
            return None
        dist = price - lvl
        near_ok = abs(dist) <= prox
        conds = [
            {"label": f"Price within {_n(prox)} points of the {name.lower()}", "ok": near_ok,
             "detail": f"Now {_ptxt(dist)} {'above' if dist >= 0 else 'below'} it ({_n(lvl)})."},
            {"label": confirms, "ok": confirm_ok, "detail": confirm_detail},
            {"label": f"Orderflow score at least {e['min_score']}", "ok": score >= e["min_score"],
             "detail": f"Score is {score}."},
        ]
        met = all(c["ok"] for c in conds) and gates_ok
        return {"key": key, "side": side, "title": f"{side.capitalize()} near the {name.lower()} ({_n(lvl)})",
                "idea": "Support: look for a bounce." if side == "long" else "Resistance: look for a rejection.",
                "conditions": conds, "met": met, "plan": plan(side), "distance": round(dist, 1)}

    scenarios = []
    sc = scenario("put_wall", "long", "Put wall", "Absorption or bullish divergence",
                  (bull or flow["absorption"]) and not bear,
                  "Bearish divergence blocks a long." if bear else
                  "Seen." if (bull or flow["absorption"]) else "Neither yet.")
    if sc:
        scenarios.append(sc)
    sc = scenario("call_wall", "short", "Call wall", "Absorption or bearish divergence",
                  (bear or flow["absorption"]) and not bull,
                  "Bullish divergence blocks a short." if bull else
                  "Seen." if (bear or flow["absorption"]) else "Neither yet.")
    if sc:
        scenarios.append(sc)
    others = [x for x in near if x["level"] not in ("put_wall", "call_wall")]
    if others:
        o = others[0]
        side = "long" if bull else "short" if bear else None
        conds = [
            {"label": f"Price within {_n(prox)} points of the {_lvl(o['level'])}", "ok": True,
             "detail": f"Now {_ptxt(o['distance'])} {'above' if o['distance'] >= 0 else 'below'} {_n(o['price'])}."},
            {"label": "A delta divergence sets the direction", "ok": side is not None,
             "detail": div or "No divergence yet."},
            {"label": f"Orderflow score at least {e['min_score']}", "ok": score >= e["min_score"], "detail": f"Score is {score}."},
        ]
        scenarios.append({"key": o["level"], "side": side or "either", "title": f"Follow the divergence at {_lvl(o['level'])} ({_n(o['price'])})",
                          "idea": "Direction comes from the divergence.", "conditions": conds,
                          "met": all(c["ok"] for c in conds) and gates_ok, "plan": plan(side) if side else None,
                          "distance": o["distance"]})
    for rt in retests:
        side = rt["side"]
        scenarios.append({
            "key": rt["level"] + "_retest", "side": side, "title": f"{side.capitalize()} on the retest of the {_lvl(rt['level'])} ({_n(rt['price'])})",
            "idea": "Break and retest: price broke through, ran past it, and came back to test it from the other side.",
            "conditions": [
                {"label": "Price broke through the level and ran past it", "ok": True, "detail": rt["notes"][0] + "."},
                {"label": "It pulled back to the level and held", "ok": True, "detail": f"Now {_ptxt(rt['distance'])} {'above' if rt['distance'] >= 0 else 'below'} it."},
                {"label": "Orderflow does not disagree", "ok": True, "detail": "No opposing divergence."},
                {"label": f"Orderflow score at least {e['min_score']}", "ok": rt["score"] >= e["min_score"], "detail": f"Retest score is {rt['score']}."}],
            "met": rt["score"] >= e["min_score"] and gates_ok, "plan": plan(side), "distance": rt["distance"]})
    out["scenarios"] = scenarios

    # ---- the loose rules, which run next to the strict ones
    loose = {"enabled": bool(s.get("loose_enabled")), "side": None, "level": None, "text": ""}
    if loose["enabled"]:
        pe = paper.profile_settings(e, "loose", s)
        lsig = paper.find_signal_loose(confluence.score(levels, flow, proximity_pct=float(pe["proximity_pct"])) + retests, flow, pe, s)
        why = paper.gate_reason(trades, "loose", name, sess_now["key"], now, s) if sess_now else "no session is open"
        if lsig:
            loose.update(side=lsig[0], level=lsig[1]["level"])
            loose["text"] = (f"The loose rules would take a {lsig[0]} at the {_lvl(lsig[1]['level'])}" +
                             (f", but {why}." if why else " now."))
        else:
            loose["text"] = "No loose setup right now."
        loose["blocked"] = bool(why)
    out["loose"] = loose

    # ---- what is happening
    story = []
    flip = levels.get("gamma_flip")
    reg = levels.get("regime")
    if reg == "negative_gamma":
        txt = ("Net gamma is negative. Dealer hedging tends to amplify moves, so swings can run further and faster, "
               "and fading a move is riskier.")
    elif reg == "positive_gamma":
        txt = "Net gamma is positive. Dealer hedging tends to dampen moves, so price often drifts back toward large strikes."
    else:
        txt = "The gamma regime is mixed, so there is no strong lean from dealer hedging."
    if flip is not None:
        txt += f" Price is {_ptxt(price - flip)} {'above' if price >= flip else 'below'} the gamma flip ({_n(flip)})."
    if levels_kind == "overnight":
        txt += (" These are overnight levels: the US day's gamma levels with the options that expired at the 4 PM "
                "close removed. They are not updated while the options market is closed.")
    story.append({"title": "Gamma regime", "text": txt})

    above = sorted([x for x in named if x[2] > price], key=lambda x: x[2])
    below = sorted([x for x in named if x[2] <= price], key=lambda x: -x[2])
    parts_txt = []
    if above:
        parts_txt.append(f"Nearest level above is the {above[0][1].lower()} at {_n(above[0][2])}, {_ptxt(above[0][2] - price)} away.")
    if below:
        parts_txt.append(f"Nearest below is the {below[0][1].lower()} at {_n(below[0][2])}, {_ptxt(price - below[0][2])} away.")
    cw, pw = levels.get("call_wall"), levels.get("put_wall")
    if cw is not None and pw is not None:
        if pw <= price <= cw:
            parts_txt.append("Price sits between the put wall and the call wall.")
        elif price > cw:
            parts_txt.append("Price is above the call wall, so the usual ceiling has been broken.")
        else:
            parts_txt.append("Price is below the put wall, so the usual floor has been broken.")
    story.append({"title": "Where price is", "text": " ".join(parts_txt)})

    recent = b.tail(30)
    chg = float(recent.close.iloc[-1] - recent.close.iloc[0])
    if sess_now and name == sessions.ASIA:   # the range since this Asia session opened, not since midnight
        start = sessions._at(sess_now["key"], paper._mins(e["first_entry_et"]))
        day, rng_name = b[b.index >= start], "tonight's"
    else:
        day, rng_name = b[[paper.to_et(t).date() == paper.to_et(last_ts).date() for t in b.index]], "today's"
    mtxt = f"Over the last 30 minutes price moved {'up' if chg >= 0 else 'down'} {_ptxt(chg)}."
    if len(day) > 5 and float(day.high.max() - day.low.min()) > 0:
        pos = (price - float(day.low.min())) / float(day.high.max() - day.low.min())
        mtxt += (f" It is in the {'top' if pos > 0.67 else 'bottom' if pos < 0.33 else 'middle'} third of {rng_name} range "
                 f"({_n(float(day.low.min()))} to {_n(float(day.high.max()))}).")
    story.append({"title": "Momentum", "text": mtxt})
    story.append({"title": "Orderflow", "text": _flow_words(flow) + " (Estimated from 1 minute bars, not true buy and sell volume.)"})
    act = "quiet" if flow["vol_ratio"] < 0.7 else "busy" if flow["vol_ratio"] > 1.3 else "normal"
    ttxt = f"Trading activity is {act} compared with the last couple of hours."
    if in_window and name == sessions.ASIA:
        note = _timing_note(mins, sessions.ASIA)
    elif now_et.weekday() < 5:
        note = _timing_note(mins)
    else:
        note = ""
    if note:
        ttxt += " " + note
    story.append({"title": "Activity and timing", "text": ttxt})
    out["story"] = story

    # ---- verdict
    sig = paper.find_signal(setups, flow, e)
    if not fresh:
        v = ("stale", "Price data is delayed", f"The latest price is {age:,.0f} minutes old, so this read may be out of date.")
    elif open_trade:
        v = ("open", "A paper trade is open", "The paper trader is managing it. No new entry until it closes.")
    elif not in_window:
        v = ("closed", "Outside the entry windows",
             (nxt["text"] + ". " if nxt else "") + "The paper trader only opens trades during its sessions: " + sessions.describe(s))
    elif sig and gates_ok:
        side, st = sig
        if st.get("kind") == "retest":
            v = ("setup", f"Setup active: {side} on the retest of the {_lvl(st['level'])}",
                 f"Price broke through the level, ran past it and is testing it again (score {st['score']}). "
                 f"The paper trader would enter now ({sessions.LABELS[name]}).")
        else:
            v = ("setup", f"Setup active: {side} at the {_lvl(st['level'])}",
                 f"Price is at a gamma level and orderflow confirms (score {st['score']}). The paper trader would enter now "
                 f"({sessions.LABELS[name]}).")
    elif sig:
        blocked = [g["label"] for g in gates if not g["ok"]]
        v = ("blocked", "Setup present, but entry is blocked", "Blocked by: " + ", ".join(blocked).lower() + ".")
    elif loose.get("side") and not loose.get("blocked"):
        v = ("watch", f"Loose setup: {loose['side']} at the {_lvl(loose['level'])}",
             "The strict rules are not met (they need absorption or a divergence), but the loose rules would take this trade.")
    elif near:
        n0 = near[0]
        v = ("watch", f"Watch: price is near the {_lvl(n0['level'])}",
             "Price is at a gamma level, but orderflow has not confirmed yet. That is the missing piece.")
    else:
        nearest = min(named, key=lambda x: abs(x[2] - price)) if named else None
        v = ("wait", "Wait: no gamma level nearby",
             (f"The nearest level is the {nearest[1].lower()} at {_n(nearest[2])}, {_ptxt(price - nearest[2])} away. "
              f"Setups only count within {_n(prox)} points.") if nearest else "No levels available.")
    out["verdict"] = {"kind": v[0], "title": v[1], "text": v[2]}
    return out


# ---------------------------------------------------------------- files and entry points
def write_live(d):
    os.makedirs(logger.DATA, exist_ok=True)
    tmp = LIVE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    safeio.replace(tmp, LIVE)


def read_live():
    try:
        with open(LIVE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run_now(bars=None):
    """Fetch fresh prices (unless given), analyze, save, and return the result."""
    if bars is None:
        bars = orderflow.fetch_nq_bars(period="2d", interval="1m")
    b = paper._normalize(bars)
    if b.empty:
        raise RuntimeError("No price data came back.")
    last = b.index[-1]
    row = paper.levels_row(sessions.levels_kind(last), last)   # today's levels by day, overnight levels otherwise
    d = analyze(b, row)
    write_live(d)
    return d
