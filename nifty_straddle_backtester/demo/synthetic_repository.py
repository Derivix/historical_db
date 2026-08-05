"""
Drop-in stand-in for `db.repository.MarketDataRepository`, generating
plausible NIFTY spot + option-chain 1-minute data in memory.

This exists purely so the framework can be validated end-to-end here,
without network access to your real TimescaleDB instance. Same method
signatures as the real repository (duck-typed), so `Backtester` doesn't
know or care which one it's talking to. Point `main.py` at your real DSN
and this file is never imported.
"""
from __future__ import annotations

import datetime as dt
import numpy as np
import pandas as pd


def _bs_price(spot, strike, t_years, iv, option_type, r=0.06):
    """Minimal Black-Scholes, just to generate internally-consistent premiums."""
    from scipy.stats import norm
    if t_years <= 0:
        intrinsic = max(spot - strike, 0) if option_type == "CE" else max(strike - spot, 0)
        return max(intrinsic, 0.05)
    d1 = (np.log(spot / strike) + (r + 0.5 * iv ** 2) * t_years) / (iv * np.sqrt(t_years))
    d2 = d1 - iv * np.sqrt(t_years)
    if option_type == "CE":
        price = spot * norm.cdf(d1) - strike * np.exp(-r * t_years) * norm.cdf(d2)
    else:
        price = strike * np.exp(-r * t_years) * norm.cdf(-d2) - spot * norm.cdf(-d1)
    return max(price, 0.05)


class SyntheticMarketDataRepository:
    def __init__(self, start_date: str, end_date: str, seed: int = 42, strike_step: float = 50.0):
        self.rng = np.random.default_rng(seed)
        self.strike_step = strike_step
        self.trading_days = pd.bdate_range(start_date, end_date).date.tolist()
        self._spot_paths: dict[dt.date, pd.DataFrame] = {}
        self._expiries_cache: dict[dt.date, dt.date] = {}
        self._last_close = 24000.0
        self._instrument_lookup: dict = {}
        self._build_spot_paths()

    # -- underlying / instrument metadata --------------------------------
    def get_underlying(self, symbol: str, kind: str = "INDEX") -> dict:
        return {"underlying_id": 1, "symbol": symbol, "kind": kind,
                "exchange": "NSE", "strike_step": self.strike_step, "lot_size": 50}

    def get_index_instrument_id(self, underlying_id: int) -> int:
        return 1

    def get_trading_days(self, instrument_id, start_date, end_date):
        return self.trading_days

    def list_expiries(self, underlying_id, start_date, end_date):
        # weekly Thursday expiry, same convention NIFTY used historically
        start = dt.date.fromisoformat(start_date)
        end = dt.date.fromisoformat(end_date)
        days = pd.date_range(start, end, freq="W-THU").date.tolist()
        return days if days else [end]

    def find_nearest_complete_expiry(self, underlying_id, start_date, end_date):
        expiries = self.list_expiries(underlying_id, start_date, end_date)
        return min(expiries) if expiries else None

    # -- OHLCV ------------------------------------------------------------
    def _build_spot_paths(self):
        for day in self.trading_days:
            n = 376  # 09:15 to 15:30 in 1-min candles
            rets = self.rng.normal(0, 0.0009, n)
            path = self._last_close * np.exp(np.cumsum(rets))
            self._last_close = path[-1]
            times = pd.date_range(
                dt.datetime.combine(day, dt.time(9, 15)), periods=n, freq="1min"
            )
            close = path
            open_ = np.roll(close, 1); open_[0] = close[0]
            high = np.maximum(open_, close) * (1 + self.rng.uniform(0, 0.0006, n))
            low = np.minimum(open_, close) * (1 - self.rng.uniform(0, 0.0006, n))
            df = pd.DataFrame({
                "ts": times, "open": open_, "high": high, "low": low,
                "close": close, "volume": self.rng.integers(1000, 5000, n),
            })
            self._spot_paths[day] = df

    def get_spot_ohlcv(self, instrument_id, start_date, end_date):
        day = dt.date.fromisoformat(start_date)
        return self._spot_paths.get(day, pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])).copy()

    def get_option_ohlcv(self, instrument_id, day):
        # Regenerate the option series deterministically from spot + implied vol,
        # keyed by instrument_id so CE/PE at the same strike/expiry are consistent.
        spot_df = self._spot_paths.get(day)
        if spot_df is None or spot_df.empty:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "open_interest"])

        rng_local = np.random.default_rng(abs(instrument_id) % (2**32 - 1))
        iv = 0.13 + rng_local.uniform(-0.02, 0.02)

        # recover strike/option_type from instrument_id isn't possible generically,
        # so this demo repo instead stores a side-channel map set by get_option_instrument.
        strike, option_type, expiry = self._instrument_lookup.get(instrument_id, (24000.0, "CE", day))

        rows = []
        for _, row in spot_df.iterrows():
            t_years = max((expiry - row["ts"].date()).days, 0) / 365.0 + 1 / (365 * 24)
            px = _bs_price(row["close"], strike, t_years, iv, option_type)
            rows.append(px)
        close = np.array(rows)
        open_ = np.roll(close, 1); open_[0] = close[0]
        high = np.maximum(open_, close) * 1.002
        low = np.minimum(open_, close) * 0.998
        return pd.DataFrame({
            "ts": spot_df["ts"].values, "open": open_, "high": high, "low": low,
            "close": close, "volume": rng_local.integers(50, 500, len(close)),
            "open_interest": rng_local.integers(1000, 20000, len(close)),
        })

    # side-channel so get_option_ohlcv can recover strike/type/expiry
    def get_option_instrument(self, underlying_id, expiry, strike, option_type):
        iid = hash((expiry, strike, option_type)) & 0xFFFFFFFF
        self._instrument_lookup[iid] = (strike, option_type, expiry)
        return {"instrument_id": iid, "raw_ticker": f"NIFTY{expiry}{int(strike)}{option_type}", "lot_size": 50}
