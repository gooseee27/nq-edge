"""Save a snapshot of gamma levels + orderflow state to CSV.

Each run:
  - appends one row to data/snapshots.csv
  - saves "overnight levels" to data/overnight_levels.json for the Asia session (see _overnight_row)
  - saves the latest 1-minute NQ bars to data/bars/YYYY-MM-DD.csv
    (Yahoo only keeps ~7 days of 1m data, so we store it ourselves)
  - writes one line to data/run_log.txt (success or the full error)

The dashboard (nq_dashboard.py) calls run_snapshot() directly, and the Windows
scheduled tasks run this file. Both end up in the same place.

Usage:
  python logger.py          # live data
  python logger.py --demo   # synthetic data, to test the setup
"""
import csv
import json
import os

import safeio
import sys
import traceback
from datetime import datetime, timezone

import pandas as pd

import confluence
import gamma
import orderflow

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("NQ_EDGE_DATA") or os.path.join(HERE, "data")
BARS = os.path.join(DATA, "bars")
SNAP = os.path.join(DATA, "snapshots.csv")
PENDING = os.path.join(DATA, "snapshots_pending.csv")
RUNLOG = os.path.join(DATA, "run_log.txt")
OVERNIGHT = os.path.join(DATA, "overnight_levels.json")

FIELDS = [
    "ts_utc", "nq_price", "qqq_spot", "ratio", "net_gex_bn",
    "call_wall", "put_wall", "gamma_flip", "regime", "flip_note", "top_strikes",
    "delta_z", "cvd_change", "divergence", "absorption",
    "vol_ratio", "range_ratio", "setups",
]


def _append_row(path, row):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)


def log_line(text):
    try:
        os.makedirs(DATA, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(RUNLOG, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {text}\n")
    except Exception:
        pass


def _row_from_levels(now, nq_price, spot, nq):
    return {
        "ts_utc": now.isoformat(timespec="seconds"),
        "nq_price": round(nq_price, 2),
        "qqq_spot": round(spot, 2),
        "ratio": round(nq["ratio"], 4),
        "net_gex_bn": round(nq["net_gex_billions_per_1pct"], 3),
        "call_wall": round(nq["call_wall"], 1),
        "put_wall": round(nq["put_wall"], 1),
        "gamma_flip": "" if nq["gamma_flip"] is None else round(nq["gamma_flip"], 1),
        "regime": nq["regime"],
        "flip_note": nq["flip_note"],
        "top_strikes": json.dumps([round(s, 1) for s in nq["top_strikes"]]),
    }


def _overnight_row(chain, spot, nq_price, now):
    """Gamma levels for after the US close (the Asia session).

    Same calculation, but options that expire today are removed, because they are gone at the 4 PM
    close, and time to expiry is measured from the close. Open interest is still the morning's figure
    (Yahoo only updates it once a day), so positions opened during the day are not counted.
    """
    close = now.replace(hour=20, minute=30, second=0, microsecond=0)   # just after the close, in UTC
    at = max(now, close)
    lv = gamma.compute_gex(chain[chain.expiry > at], spot, now=at)
    return _row_from_levels(now, nq_price, spot, lv.to_nq(nq_price))


def read_overnight():
    """{ET date: overnight levels row}. Empty when nothing has been saved or the file is unreadable."""
    try:
        with open(OVERNIGHT, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_overnight(day_iso, row):
    data = read_overnight()
    data[day_iso] = row                      # a later snapshot the same day replaces the earlier one
    for k in sorted(data)[:-40]:
        del data[k]
    tmp = OVERNIGHT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    safeio.replace(tmp, OVERNIGHT)


def run_snapshot(demo=False):
    """Take one snapshot and save it. Returns (row, message). Raises on failure."""
    os.makedirs(BARS, exist_ok=True)

    if demo:
        from brief import demo_inputs
        chain, spot, bars = demo_inputs()
    else:
        chain, spot = gamma.fetch_qqq_chain()
        bars = orderflow.fetch_nq_bars(period="2d", interval="1m")

    lv = gamma.compute_gex(chain, spot)
    nq_price = float(bars.close.iloc[-1])
    nq = lv.to_nq(nq_price)
    flow = orderflow.analyze(bars)
    setups = confluence.score(nq, flow)

    now = datetime.now(timezone.utc)
    row = {
        **_row_from_levels(now, nq_price, spot, nq),
        "delta_z": round(flow["delta_z"], 2),
        "cvd_change": round(flow["cvd_change"], 1),
        "divergence": flow["divergence"] or "",
        "absorption": flow["absorption"],
        "vol_ratio": round(flow["vol_ratio"], 2),
        "range_ratio": round(flow["range_ratio"], 2),
        "setups": json.dumps(setups),
    }

    # If an older version wrote different columns, keep that file aside instead of mixing formats.
    # Tolerates Excel re-saving the file (BOM, quotes, spaces) so that does not trigger a rename.
    if os.path.exists(SNAP):
        with open(SNAP, newline="", encoding="utf-8-sig") as f:
            first = [c.strip().strip('"') for c in f.readline().strip().split(",")]
        if first != FIELDS:
            safeio.replace(SNAP, os.path.join(DATA, "snapshots_old_" + now.strftime("%Y%m%d%H%M%S") + ".csv"))

    note = ""
    try:
        _append_row(SNAP, row)
    except PermissionError:
        # file is open in Excel: save to a side file so the snapshot is not lost
        _append_row(PENDING, row)
        note = " (snapshots.csv was open in Excel, saved to snapshots_pending.csv instead)"

    bars.to_csv(os.path.join(BARS, now.strftime("%Y-%m-%d") + ".csv"))

    if not demo:   # overnight levels for the Asia session. A failure here never loses the snapshot.
        try:
            day = pd.Timestamp(now).tz_convert("America/New_York").date().isoformat()
            _save_overnight(day, _overnight_row(chain, spot, nq_price, now))
        except Exception as e:  # noqa: BLE001
            log_line("OVERNIGHT LEVELS FAILED " + repr(e))
    msg = (f"Logged snapshot | NQ {row['nq_price']} | flip {row['gamma_flip'] or 'none'} | "
           f"setups: {len(setups)}{note}")
    return row, msg


def main():
    try:
        _, msg = run_snapshot(demo="--demo" in sys.argv)
        log_line("OK " + msg)
        print(msg)
    except Exception as e:
        log_line("FAILED " + repr(e) + "\n" + traceback.format_exc())
        print("Snapshot failed:", e)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
