"""Gamma exposure (GEX) levels from the QQQ options chain, mapped to NQ price.

Assumption (standard, but only an assumption): dealers are long calls and
short puts, so call gamma counts positive and put gamma counts negative.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd


def bs_gamma(spot, strike, t_years, iv):
    if t_years <= 0 or iv <= 0 or spot <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t_years) / (iv * math.sqrt(t_years))
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return pdf / (spot * iv * math.sqrt(t_years))


def bs_gamma_vec(spot, strikes, t_years, ivs):
    """Vectorized Black-Scholes gamma for arrays of strikes/times/IVs at one spot."""
    sqt = ivs * np.sqrt(t_years)
    d1 = (np.log(spot / strikes) + 0.5 * ivs * ivs * t_years) / sqt
    return np.exp(-0.5 * d1 * d1) / np.sqrt(2 * np.pi) / (spot * sqt)


@dataclass
class GammaLevels:
    spot: float
    call_wall: float
    put_wall: float
    gamma_flip: float | None
    top_strikes: list  # strikes with largest absolute GEX
    net_gex: float
    by_strike: pd.DataFrame
    regime: str = "unknown"      # negative_gamma / positive_gamma / mixed
    flip_note: str = ""

    def to_nq(self, nq_price: float) -> dict:
        r = nq_price / self.spot
        out = {
            "call_wall": self.call_wall * r,
            "put_wall": self.put_wall * r,
            "gamma_flip": None if self.gamma_flip is None else self.gamma_flip * r,
            "top_strikes": [s * r for s in self.top_strikes],
            "net_gex_billions_per_1pct": self.net_gex / 1e9,
            "ratio": r,
            "regime": self.regime,
            "flip_note": self.flip_note,
        }
        return out


def compute_gex(chain: pd.DataFrame, spot: float, now=None, band=0.08) -> GammaLevels:
    """chain columns: strike, type ('C'/'P'), oi, iv, expiry (datetime, tz-aware UTC)."""
    now = now or datetime.now(timezone.utc)
    df = chain.copy()
    n_raw = len(df)
    df["oi"] = pd.to_numeric(df["oi"], errors="coerce").fillna(0)
    df["iv"] = pd.to_numeric(df["iv"], errors="coerce").fillna(0)
    df = df[(df.strike > spot * (1 - band)) & (df.strike < spot * (1 + band))]
    n_band = len(df)
    n_oi = int((df.oi > 0).sum())
    n_iv = int((df.iv > 0.01).sum())
    df = df[(df.oi > 0) & (df.iv > 0.01)]  # Yahoo shows IV near 0 when market is closed
    if df.empty:
        raise ValueError(
            f"No usable options rows. Raw rows: {n_raw}, within strike band: {n_band}, "
            f"with open interest > 0: {n_oi}, with IV > 1%: {n_iv}. "
            "If IV or OI is 0, Yahoo data is likely blank because the market is closed. "
            "Try again between 9:35 AM and 4:00 PM ET (6:35 AM to 1:00 PM Arizona time)."
        )
    # floor time at ~half a day so 0DTE does not blow up
    df["t"] = ((df.expiry - now).dt.total_seconds() / (365 * 24 * 3600)).clip(lower=0.5 / 365)
    df["gamma"] = [bs_gamma(spot, k, t, v) for k, t, v in zip(df.strike, df.t, df.iv)]
    # dollar gamma per 1% move
    df["gex"] = df.gamma * df.oi * 100 * spot * spot * 0.01
    df.loc[df.type == "P", "gex"] *= -1

    g = df.groupby("strike").gex.sum().sort_index()
    calls = df[df.type == "C"].groupby("strike").gex.sum()
    puts = df[df.type == "P"].groupby("strike").gex.sum()
    call_wall = float(calls.idxmax()) if len(calls) else float(g.idxmax())
    put_wall = float(puts.idxmin()) if len(puts) else float(g.idxmin())

    # Gamma flip (standard method): recompute TOTAL GEX at a grid of hypothetical
    # spot prices and find where it crosses zero, nearest to the current spot.
    K = df.strike.to_numpy(float)
    T = df.t.to_numpy(float)
    V = df.iv.to_numpy(float)
    W = (df.oi.to_numpy(float) * np.where(df.type.to_numpy() == "P", -1.0, 1.0))
    grid = np.linspace(spot * 0.94, spot * 1.06, 241)
    profile = np.array([
        float((bs_gamma_vec(x, K, T, V) * W).sum() * 100 * x * x * 0.01) for x in grid
    ])
    flip = None
    cross = np.where(np.diff(np.sign(profile)) != 0)[0]
    if len(cross):
        cands = []
        for i in cross:
            x0, x1, y0, y1 = grid[i], grid[i + 1], profile[i], profile[i + 1]
            xz = x0 - y0 * (x1 - x0) / (y1 - y0)
            cands.append((abs(xz - spot), float(xz)))
        flip = min(cands)[1]
        regime = "positive_gamma" if profile[np.argmin(abs(grid - spot))] > 0 else "negative_gamma"
        flip_note = ""
    elif (profile < 0).all():
        regime = "negative_gamma"
        flip_note = "no flip within +/-6% of spot: net gamma negative across the whole range"
    elif (profile > 0).all():
        regime = "positive_gamma"
        flip_note = "no flip within +/-6% of spot: net gamma positive across the whole range"
    else:
        regime = "mixed"
        flip_note = ""

    top = g.abs().sort_values(ascending=False).head(5).index.tolist()
    return GammaLevels(spot, call_wall, put_wall, flip, [float(x) for x in top],
                       float(g.sum()), g.reset_index(), regime, flip_note)


def fetch_qqq_chain(max_days=7):
    """Free data via yfinance. Delayed, and open interest updates once daily."""
    import yfinance as yf
    import safeio
    t = yf.Ticker("QQQ")
    hist = safeio.retry(lambda: t.history(period="1d"), ok=lambda h: len(h) > 0)
    if len(hist) == 0:
        raise RuntimeError("Yahoo returned no QQQ price. This is usually temporary; the next scheduled run tries again.")
    spot = float(hist["Close"].iloc[-1])
    now = datetime.now(timezone.utc)
    rows = []
    for exp in t.options:
        exp_dt = datetime.strptime(exp, "%Y-%m-%d").replace(hour=20, tzinfo=timezone.utc)
        if (exp_dt - now).days > max_days:
            break
        ch = safeio.retry(lambda e=exp: t.option_chain(e))
        for typ, d in (("C", ch.calls), ("P", ch.puts)):
            for _, r in d.iterrows():
                rows.append(dict(strike=r.strike, type=typ, oi=r.openInterest,
                                 iv=r.impliedVolatility, expiry=exp_dt))
    return pd.DataFrame(rows), spot
