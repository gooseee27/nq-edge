"""Orderflow PROXY from OHLCV bars (free data has no aggressor side).

Delta estimate per bar = volume * (2 * (close - low) / (high - low) - 1).
This is a rough stand-in for real delta. When you get tick data (Databento),
replace estimate_delta() with true buy/sell volume and keep the rest.
"""
import numpy as np
import pandas as pd


def estimate_delta(bars: pd.DataFrame) -> pd.Series:
    rng = (bars.high - bars.low).replace(0, np.nan)
    clv = (2 * (bars.close - bars.low) / rng - 1).fillna(0)
    return clv * bars.volume


def analyze(bars: pd.DataFrame, lookback=20) -> dict:
    """bars columns: open, high, low, close, volume (1-minute, recent session)."""
    b = bars.copy()
    b["delta"] = estimate_delta(b)
    b["cvd"] = b.delta.cumsum()
    w = b.tail(lookback)
    d_mu, d_sd = b.delta.tail(100).mean(), b.delta.tail(100).std() or 1.0
    delta_z = float((w.delta.sum() - d_mu * lookback) / (d_sd * np.sqrt(lookback)))

    price_chg = float(w.close.iloc[-1] - w.close.iloc[0])
    cvd_chg = float(w.cvd.iloc[-1] - w.cvd.iloc[0])
    divergence = None
    if price_chg > 0 and cvd_chg < 0:
        divergence = "bearish (price up, delta down)"
    elif price_chg < 0 and cvd_chg > 0:
        divergence = "bullish (price down, delta up)"

    # absorption: heavy volume, tiny range in the last 5 bars
    last = b.tail(5)
    vol_ratio = float(last.volume.mean() / (b.volume.tail(100).mean() or 1))
    rng_ratio = float((last.high - last.low).mean() / ((b.high - b.low).tail(100).mean() or 1))
    absorption = vol_ratio > 1.5 and rng_ratio < 0.8

    return {
        "price": float(b.close.iloc[-1]),
        "delta_z": delta_z,
        "cvd_change": cvd_chg,
        "divergence": divergence,
        "absorption": bool(absorption),
        "vol_ratio": vol_ratio,
        "range_ratio": rng_ratio,
    }


def fetch_nq_bars(period="2d", interval="1m") -> pd.DataFrame:
    import yfinance as yf
    import safeio
    cols = ["open", "high", "low", "close", "volume"]

    def get():
        df = yf.download("NQ=F", period=period, interval=interval, progress=False)
        if df is None or df.empty:
            return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC"))
        df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
        return df[cols].dropna()

    # Yahoo sometimes answers with nothing for a few seconds. Ask again before giving up.
    return safeio.retry(get, ok=lambda d: len(d) > 0)
