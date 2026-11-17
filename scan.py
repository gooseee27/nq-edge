"""One paper trading scan plus a fresh market read. The Windows schedule runs this every 5 minutes
during market hours.

Only interesting events (a trade opening or closing, or an error) are written to the activity log.
The market read is saved to data/live.json for the dashboard.
"""
import traceback

import analyzer
import logger
import orderflow
import paper
import experiments
import reactions


def main():
    failed, bars = False, None
    try:
        bars = orderflow.fetch_nq_bars(period="2d", interval="1m")
        msg, changed = paper.scan_once(bars=bars)
        if changed:
            logger.log_line("PAPER " + msg)
        print(msg)
    except Exception as e:  # noqa: BLE001
        failed = True
        logger.log_line("SCAN FAILED " + repr(e) + "\n" + traceback.format_exc())
        print("Scan failed:", e)
    if bars is not None:
        try:
            analyzer.run_now(bars)  # after the scan, so it knows about any trade just opened
        except Exception as e:  # noqa: BLE001
            failed = True
            logger.log_line("MARKET READ FAILED " + repr(e) + "\n" + traceback.format_exc())
            print("Market read failed:", e)
    if bars is not None:                    # keep the price history and refresh the "do levels matter" numbers
        try:
            reactions.store_bars(bars)
            if reactions.stale():
                reactions.run()
        except Exception as e:  # noqa: BLE001      this never fails the scan
            logger.log_line("REACTIONS FAILED " + repr(e))
        try:
            if experiments.stale():
                experiments.run()
        except Exception as e:  # noqa: BLE001      this never fails the scan
            logger.log_line("EXPERIMENTS FAILED " + repr(e))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
