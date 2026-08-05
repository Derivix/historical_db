import datetime as dt
from utils.datatype import _Leg
import pandas as pd

def _last_price(frame: pd.DataFrame, when: dt.datetime) -> float | None:
    """Last available close at ``when`` using a fast sorted timestamp lookup."""
    index = int(frame["ts"].searchsorted(when, side="right")) - 1
    return None if index < 0 else float(frame["close"].iloc[index])

def _candidate_strikes(spot: float, strike_step: float, cfg) -> list[float]:
        center = round(spot / strike_step) * strike_step
        return [round(center + offset * strike_step, 2) for offset in range(-cfg.strike_search_steps, cfg.strike_search_steps + 1)]

def _option_data(repo, _option_cache: dict, underlying_id: str, expiry: dt.date, strike: float, option_type: str, day: dt.date):
        key = (expiry, strike, option_type, day)
        if key not in _option_cache:
            instrument = repo.get_option_instrument(underlying_id, expiry, strike, option_type)
            if instrument is None:
                _option_cache[key] = None
            else:
                prices = repo.get_option_ohlcv(instrument["instrument_id"], day).sort_values("ts")
                _option_cache[key] = None if prices.empty else (instrument, prices)
        return _option_cache[key]


def _premium_close_to(repo, _option_cache: dict, underlying_id: str, day: dt.date, expiry: dt.date, option_type: str, when: dt.datetime, spot: float, target: float, strike_step: float, cfg) -> _Leg | None:
        # The SQL repository can return the full nearby chain and its prices in
        # one query.  Keep the per-strike fallback for lightweight/demo repos.
        candidate_lookup = getattr(repo, "get_option_candidates_at", None) #checks get_option_candidates_at method in repo object
        if candidate_lookup is not None:
            strikes = _candidate_strikes(spot, strike_step, cfg)
            candidates = candidate_lookup(
                underlying_id, 
                expiry, 
                option_type, 
                min(strikes), 
                max(strikes), 
                day, 
                when,
            )
            if candidates:
                candidate = min(
                    (row for row in candidates if row["price"] is not None and float(row["price"]) > 0),
                    key=lambda row: abs(float(row["price"]) - target), default=None,
                )
                if candidate is not None:
                    found = _option_data(repo, _option_cache, underlying_id, expiry, float(candidate["strike"]), option_type, day)
                    if found is not None:
                        instrument, prices = found
                        price = _last_price(prices, when)
                        if price is not None and price > 0:
                            quantity = cfg.lots * int(instrument.get("lot_size") or 65)
                            return _Leg(option_type, instrument["instrument_id"], instrument.get("raw_ticker", ""), float(candidate["strike"]), when, price, quantity, prices)

        best: tuple[float, _Leg] | None = None
        for strike in _candidate_strikes(spot, strike_step, cfg):
            found = _option_data(repo, _option_cache, underlying_id, expiry, strike, option_type, day)
            if found is None:
                continue
            instrument, prices = found
            price = _last_price(prices, when)
            if price is None or price <= 0:
                continue
            quantity = cfg.lots * int(instrument.get("lot_size") or 65)
            leg = _Leg(option_type, instrument["instrument_id"], instrument.get("raw_ticker", ""), strike, when, price, quantity, prices)
            candidate = (abs(price - target), leg)
            if best is None or candidate[0] < best[0]:
                best = candidate
        return None if best is None else best[1]

def _ATM_strike(spot: float, strike_step: float) -> float:
    return round(spot / strike_step) * strike_step

def _premium_on_atm(repo, _option_cache: dict, underlying_id: str, day: dt.date, expiry: dt.date, option_type: str, when: dt.datetime, spot: float, strike_step: float, cfg) -> _Leg | None:
    
    candidate_lookup = getattr(repo, "get_option_candidates_at", None)

    if candidate_lookup is not None:       
        atm_strike = _ATM_strike(spot, strike_step)
        candidate = candidate_lookup(
            underlying_id, 
            expiry, 
            option_type, 
            atm_strike,
            atm_strike, 
            day, 
            when,
        )
        if candidate:
            candidate = candidate[0]
            found = _option_data(repo, _option_cache, underlying_id, expiry, float(candidate["strike"]), option_type, day)
            if found is not None:
                instrument, prices = found
                price = _last_price(prices, when)
                if price is not None and price > 0:
                    quantity = cfg.lots * int(instrument.get("lot_size") or 65)
                    return _Leg(option_type, instrument["instrument_id"], instrument.get("raw_ticker", ""), float(candidate["strike"]), when, price, quantity, prices)

    