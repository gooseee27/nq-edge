"""Small helpers that make the NQ Edge scans survive the two most common hiccups:

1. Windows says a file is busy (the dashboard is reading it, antivirus is scanning it, a sync tool
   has it open) at the exact moment a scan saves it.  replace() waits a moment and tries again.
2. Yahoo returns nothing or errors out for a second.  retry() asks again a couple of times.
"""
import os
import time


def replace(tmp, dst, tries=8, delay=0.25):
    """Same as os.replace, but waits and retries while Windows reports the target is in use."""
    last = None
    for i in range(tries):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError as e:
            last = e
            time.sleep(delay * (i + 1))      # 0.25s, 0.5s, 0.75s ... about 9 seconds in total
    raise last


def retry(fn, ok=lambda r: True, tries=3, delay=2.0):
    """Call fn() until ok(result) is true. Returns the last result if it never is.

    If every attempt raised an exception, the last exception is raised so the problem is still logged.
    """
    result, error = None, None
    for i in range(tries):
        try:
            result, error = fn(), None
            if ok(result):
                return result
        except Exception as e:  # noqa: BLE001
            error = e
        if i < tries - 1:
            time.sleep(delay * (i + 1))      # 2s, then 4s
    if error is not None:
        raise error
    return result
