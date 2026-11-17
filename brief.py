"""Run the full pipeline and print a session brief.

Usage:
  python brief.py                 # live free data (yfinance)
  python brief.py --demo          # synthetic data, no network
  ANTHROPIC_API_KEY=... python brief.py   # adds a Claude-written narrative
"""
import json
import os
import sys

import confluence
import gamma
import orderflow


def demo_inputs():
    import numpy as np
    import pandas as pd
    from datetime import datetime, timedelta, timezone
    rng = np.random.default_rng(7)
    now = datetime.now(timezone.utc)
    spot = 520.0
    rows = []
    for dte in (0, 1, 3):
        exp = now + timedelta(days=dte, hours=6)
        for k in np.arange(490, 551, 1.0):
            for typ in "CP":
                oi = max(0, int(rng.normal(3000, 1500) * np.exp(-((k - spot) / 15) ** 2)))
                rows.append(dict(strike=k, type=typ, oi=oi, iv=0.18, expiry=exp))
    chain = pd.DataFrame(rows)
    n = 300
    close = 25000 + np.cumsum(rng.normal(0, 3, n))
    bars = pd.DataFrame({"open": close - rng.normal(0, 1, n), "close": close,
                         "high": close + abs(rng.normal(2, 1, n)),
                         "low": close - abs(rng.normal(2, 1, n)),
                         "volume": rng.integers(500, 3000, n).astype(float)})
    return chain, spot, bars


def main():
    if "--demo" in sys.argv:
        chain, spot, bars = demo_inputs()
    else:
        chain, spot = gamma.fetch_qqq_chain()
        bars = orderflow.fetch_nq_bars()

    lv = gamma.compute_gex(chain, spot)
    nq_price = float(bars.close.iloc[-1])
    nq_lv = lv.to_nq(nq_price)
    flow = orderflow.analyze(bars)
    setups = confluence.score(nq_lv, flow)

    state = {"nq_price": nq_price, "gamma_levels_nq": nq_lv, "orderflow_proxy": flow,
             "active_setups": setups}
    print(json.dumps(state, indent=2, default=float))

    if os.getenv("ANTHROPIC_API_KEY"):
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-sonnet-5-5", max_tokens=900,
            system=("You are an NQ futures session analyst. Given gamma levels (converted "
                    "from QQQ options) and a bar-based orderflow proxy, write a brief: "
                    "regime (above/below gamma flip), key levels, bias, 2 scenarios with "
                    "invalidation. Be explicit about uncertainty. Orderflow is a proxy, "
                    "not true delta. No em dashes."),
            messages=[{"role": "user", "content": json.dumps(state, default=float)}])
        print("\n" + msg.content[0].text)


if __name__ == "__main__":
    main()
