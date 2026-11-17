"""Trading sessions for the paper trader. All times are New York (ET) so US daylight saving
changes never matter to the rules. Only the Windows schedule runs on your computer's clock.

Two sessions:
  us    weekdays, 9:45 AM to 3:30 PM ET for entries, flat at 3:55 PM. Uses gamma levels from today.
  asia  Sunday to Thursday evenings, 8:15 PM to 1:30 AM ET for entries, flat at 1:55 AM.
        The options market is closed then, so it uses overnight levels: the last market hours
        snapshot with the options that expired at that close removed (see logger.py).

A session has a "key", the ET date it started on. The Asia session that starts Monday at 8:15 PM
and runs past midnight into Tuesday morning is one session with Monday's key.
"""
from datetime import datetime, time, timedelta

import pandas as pd

ET = "America/New_York"
US, ASIA = "us", "asia"
LABELS = {US: "US day session", ASIA: "Asia session"}

# Starting guesses, scaled down from the US session because overnight moves are smaller.
# They are untested. The dashboard shows results per session so you can see how each does.
ASIA_DEFAULTS = {
    "asia_enabled": True,
    "asia_stop_pts": 15.0,
    "asia_target_pts": 30.0,
    "asia_min_score": 2,
    "asia_slippage_pts": 1.0,        # thinner books overnight, so assume a worse fill
    "asia_proximity_pct": 0.0010,    # how close to a level, as a share of price (0.10% is about 30 points)
    "asia_first_entry_et": "20:15",  # Tokyo opens at 8 PM ET in summer time
    "asia_last_entry_et": "01:30",
    "asia_flat_et": "01:55",
}
US_PROXIMITY = 0.0015
GAP_MIN = 15  # a bar this close after the flat time still counts as "at" the flat time


def mins(hhmm):
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def fmt12(hhmm):
    m = mins(hhmm)
    h, mm = divmod(m, 60)
    return f"{(h % 12) or 12}:{mm:02d} {'AM' if h < 12 else 'PM'}"


def to_et(ts):
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(ET)


def _at(day, minute_of_day):
    """UTC timestamp for an ET wall clock time on a date."""
    naive = pd.Timestamp(datetime.combine(day, time(minute_of_day // 60, minute_of_day % 60)))
    return naive.tz_localize(ET, ambiguous=True, nonexistent="shift_forward").tz_convert("UTC")


def effective(s, name):
    """The settings with this session's stop, target, minimum score, slippage, proximity and times
    swapped into the usual keys, so the rest of the code does not care which session it is in."""
    e = {**ASIA_DEFAULTS, "proximity_pct": US_PROXIMITY, **s}
    if name == ASIA:
        e.update(stop_pts=e["asia_stop_pts"], target_pts=e["asia_target_pts"], min_score=e["asia_min_score"],
                 slippage_pts=e["asia_slippage_pts"], proximity_pct=e["asia_proximity_pct"],
                 first_entry_et=e["asia_first_entry_et"], last_entry_et=e["asia_last_entry_et"],
                 flat_et=e["asia_flat_et"])
    return e


def asia_on(s):
    return bool({**ASIA_DEFAULTS, **s}["asia_enabled"])


def session_at(ts, s):
    """Which entry window a timestamp falls in: {"name", "key"} or None."""
    e = {**ASIA_DEFAULTS, **s}
    et = to_et(ts)
    m = et.hour * 60 + et.minute
    if et.weekday() < 5 and mins(e["first_entry_et"]) <= m <= mins(e["last_entry_et"]):
        return {"name": US, "key": et.date()}
    if e["asia_enabled"]:
        first, last = mins(e["asia_first_entry_et"]), mins(e["asia_last_entry_et"])
        if first > last:                       # the window runs past midnight
            if m >= first:
                key = et.date()
            elif m <= last:
                key = et.date() - timedelta(days=1)
            else:
                return None
        elif first <= m <= last:
            key = et.date()
        else:
            return None
        if key.weekday() in (6, 0, 1, 2, 3):   # Sunday to Thursday evenings. Friday evening is closed.
            return {"name": ASIA, "key": key}
    return None


def session_key(name, ts):
    """The ET date a trade's session started on."""
    et = to_et(ts)
    if name == ASIA and et.hour < 12:
        return et.date() - timedelta(days=1)
    return et.date()


def deadline(name, key, s):
    """The moment (UTC) everything in this session must be closed."""
    e = effective(s, name)
    flat = mins(e["flat_et"])
    day = key + timedelta(days=1) if (name == ASIA and flat < 12 * 60) else key
    return _at(day, flat)


def levels_kind(ts):
    """Which gamma levels describe this moment: 'day' during US cash hours, otherwise 'overnight'."""
    et = to_et(ts)
    m = et.hour * 60 + et.minute
    return "day" if (et.weekday() < 5 and 9 * 60 + 30 <= m < 16 * 60) else "overnight"


def next_window(now, s):
    """The next entry window that opens after `now`: {"name", "label", "start_utc", "text"}."""
    e = {**ASIA_DEFAULTS, **s}
    now = pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    day0 = to_et(now).date() - timedelta(days=1)
    best = None
    for i in range(9):
        d = day0 + timedelta(days=i)
        cands = []
        if d.weekday() < 5:
            cands.append((US, _at(d, mins(e["first_entry_et"])), e["first_entry_et"]))
        if e["asia_enabled"] and d.weekday() in (6, 0, 1, 2, 3):
            cands.append((ASIA, _at(d, mins(e["asia_first_entry_et"])), e["asia_first_entry_et"]))
        for name, start, hhmm in cands:
            if start > now and (best is None or start < best["start_utc"]):
                best = {"name": name, "label": LABELS[name], "start_utc": start,
                        "text": f"{LABELS[name]} opens at {fmt12(hhmm)} ET"}
    return best


def describe(s):
    e = {**ASIA_DEFAULTS, **s}
    t = (f"US day session {fmt12(e['first_entry_et'])} to {fmt12(e['last_entry_et'])} ET on weekdays")
    if e["asia_enabled"]:
        t += (f"; Asia session {fmt12(e['asia_first_entry_et'])} to {fmt12(e['asia_last_entry_et'])} ET, "
              "Sunday to Thursday evenings")
    return t + "."
