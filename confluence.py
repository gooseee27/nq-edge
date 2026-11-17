"""Deterministic confluence scoring. No LLM here, so it can be backtested."""


def score(nq_levels: dict, flow: dict, proximity_pct=0.0015) -> list:
    """proximity_pct: how close price must be to a level (0.0015 = 0.15%, ~46 pts at NQ 30000)."""
    price = flow["price"]
    prox = price * proximity_pct
    regime = nq_levels.get("regime", "unknown")

    named = [
        ("call_wall", nq_levels["call_wall"]),
        ("put_wall", nq_levels["put_wall"]),
        ("gamma_flip", nq_levels["gamma_flip"]),
    ]
    for i, s in enumerate(nq_levels["top_strikes"]):
        named.append((f"gex_strike_{i+1}", s))

    setups, seen = [], []
    for name, lvl in named:
        if lvl is None or abs(price - lvl) > prox:
            continue
        if any(abs(lvl - x) < 1.0 for x in seen):   # same level listed twice
            continue
        seen.append(lvl)
        above = price >= lvl
        pts, notes = 0, []
        if flow["absorption"]:
            pts += 2
            notes.append("absorption at level")
        if flow["divergence"]:
            pts += 2
            notes.append(f"delta divergence: {flow['divergence']}")
        if abs(flow["delta_z"]) > 1.5:
            pts += 1
            notes.append(f"strong delta (z={flow['delta_z']:.1f})")

        if name == "call_wall":
            lean = ("resistance: fade attempts if flow stalls" if not above else
                    "broke above wall: " + ("moves may accelerate (negative gamma)"
                                            if regime == "negative_gamma" else "watch for failed breakout"))
        elif name == "put_wall":
            lean = ("support: bounce candidate if flow stalls" if above else
                    "broke below wall: " + ("moves may accelerate (negative gamma)"
                                            if regime == "negative_gamma" else "watch for reclaim"))
        elif name == "gamma_flip":
            lean = "regime boundary: below = trending/volatile, above = mean-reverting"
        else:
            lean = "high-gamma strike: possible magnet / pin zone"
        setups.append({"level": name, "price": round(lvl, 1),
                       "distance": round(price - lvl, 1), "score": pts,
                       "lean": lean, "regime": regime, "notes": notes})
    return sorted(setups, key=lambda x: -x["score"])


# ---------------------------------------------------------------- break and retest
# Price breaks through a level, runs past it, pulls back to test it from the new side and holds.
# Long after a break up, short after a break down. All numbers are NQ points and are untested guesses.
RETEST = dict(lookback=90, buffer=20.0, zone=10.0, hold=8.0, below=5.0)


def retest_setups(nq_levels: dict, closes, flow: dict, p=None) -> list:
    """closes: recent 1 minute closes, oldest first. Returns setups shaped like score() plus kind and side."""
    p = {**RETEST, **(p or {})}
    c = [float(x) for x in list(closes)[-int(p["lookback"]):]]
    if len(c) < 20:
        return []
    price = c[-1]
    z = float(flow.get("delta_z") or 0.0)
    div = flow.get("divergence") or ""
    regime = nq_levels.get("regime", "unknown")
    named = [("call_wall", nq_levels.get("call_wall")), ("put_wall", nq_levels.get("put_wall")),
             ("gamma_flip", nq_levels.get("gamma_flip"))]
    out, seen = [], []
    for name, lvl in named:
        if lvl is None or any(abs(lvl - x) < 1.0 for x in seen):
            continue
        seen.append(lvl)
        for side, d in (("long", 1), ("short", -1)):
            s = [(x - lvl) * d for x in c]                       # distance past the level, in the trade direction
            wrong = [i for i, v in enumerate(s) if v < -p["hold"]]
            if not wrong or wrong[-1] >= len(s) - 1:
                continue                                          # never on the other side, or still there
            k = wrong[-1]
            if max(s[k + 1:]) < p["buffer"]:
                continue                                          # the break did not run far enough
            if not (-p["below"] <= s[-1] <= p["zone"]):
                continue                                          # not back at the level yet, or it failed
            if (d == 1 and div.startswith("bearish")) or (d == -1 and div.startswith("bullish")):
                continue                                          # orderflow disagrees
            pts, notes = 2, [f"broke {'above' if d == 1 else 'below'} {lvl:,.0f}, ran {max(s[k + 1:]):,.0f} points, retesting"]
            if z * d > 0.5:
                pts += 1
                notes.append(f"delta agrees (z={z:.1f})")
            if flow.get("absorption"):
                pts += 1
                notes.append("absorption at level")
            out.append({"level": name, "price": round(lvl, 1), "distance": round(price - lvl, 1), "score": pts,
                        "lean": f"break and retest: {side} while it holds {'above' if d == 1 else 'below'} the level",
                        "regime": regime, "notes": notes, "kind": "retest", "side": side})
    return sorted(out, key=lambda x: -x["score"])


# ---------------------------------------------------------------- level touch
# The same rules the "Do the levels matter?" measurement uses (reactions.py): price comes within TOUCH_TOL
# points of a level after being TOUCH_FAR+ points away 15 minutes earlier and staying on that side. Trade the
# bounce: long if it came down to the level, short if it came up. No orderflow requirement.
TOUCH_TOL, TOUCH_FAR, TOUCH_APPROACH, TOUCH_LOOKBACK, TOUCH_MAX_GAP = 8.0, 30.0, 15, 5, 15.0


def touch_setup(nq_levels: dict, closes, highs, lows):
    """A touch in the last TOUCH_LOOKBACK bars (one scan's worth) that price is still close to.
    Returns a setup dict (kind 'touch', with side) or None. Nearest level wins."""
    c, h, lo = [float(x) for x in closes], [float(x) for x in highs], [float(x) for x in lows]
    n = len(c)
    if n < TOUCH_APPROACH + 2:
        return None
    price = c[-1]
    named = [(k, nq_levels.get(k)) for k in ("call_wall", "put_wall", "gamma_flip")]
    named += [(f"gex_strike_{i + 1}", s) for i, s in enumerate(nq_levels.get("top_strikes") or [])]
    seen, best = [], None
    for name, lvl in named:
        if lvl is None or any(abs(lvl - x) < 1.0 for x in seen):
            continue
        seen.append(lvl)
        for k in range(max(TOUCH_APPROACH, n - TOUCH_LOOKBACK), n):
            if h[k] < lvl - TOUCH_TOL or lo[k] > lvl + TOUCH_TOL:
                continue
            ref, path = c[k - TOUCH_APPROACH], c[k - TOUCH_APPROACH:k]
            if ref > lvl + TOUCH_FAR and min(path) > lvl - TOUCH_TOL:
                d = 1
            elif ref < lvl - TOUCH_FAR and max(path) < lvl + TOUCH_TOL:
                d = -1
            else:
                continue
            if abs(price - lvl) > TOUCH_MAX_GAP or (price - lvl) * d < -TOUCH_TOL:
                break                          # price already left the level, or went through it
            st = {"level": name, "price": round(lvl, 1), "distance": round(price - lvl, 1), "score": 0,
                  "lean": "bounce off the level", "regime": nq_levels.get("regime", "unknown"),
                  "notes": [f"first touch after a {abs(ref - lvl):,.0f} point approach from {'above' if d == 1 else 'below'}"],
                  "kind": "touch", "side": "long" if d == 1 else "short"}
            if best is None or abs(st["distance"]) < abs(best["distance"]):
                best = st
            break
    return best
