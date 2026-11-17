"""Prints "yes" when a gamma snapshot is due now, otherwise "no".

Due means: New York time is 9:40 to 10:30 AM or 3:25 to 3:55 PM on a weekday, and there is no snapshot
from the last 2 hours yet. This matches the morning and afternoon snapshots the Windows schedule took.
"""
import csv
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

now = datetime.now(timezone.utc)
et = now.astimezone(ZoneInfo("America/New_York"))
minutes = et.hour * 60 + et.minute
in_window = et.weekday() < 5 and (9 * 60 + 40 <= minutes < 10 * 60 + 30 or 15 * 60 + 25 <= minutes < 15 * 60 + 55)

recent = False
snap = os.path.join(os.environ.get("NQ_EDGE_DATA", "data"), "snapshots.csv")
try:
    with open(snap, newline="", encoding="utf-8-sig") as f:
        last = None
        for row in csv.DictReader(f):
            last = row.get("ts_utc") or last
    if last:
        recent = now - datetime.fromisoformat(last).astimezone(timezone.utc) < timedelta(hours=2)
except (OSError, ValueError):
    pass

print("yes" if in_window and not recent else "no")
