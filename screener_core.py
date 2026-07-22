"""
Shared screener logic: multi-period returns, Bybit futures CVD, and
price/CVD divergence.

Data source modes:
- Coinalyze: Bybit futures OHLCV with real buy/sell volume fields, used to
  reconstruct futures CVD from buy volume minus sell volume.
- Binance: true futures CVD from taker-buy volume in klines.
- Bybit: signed futures volume proxy from OHLCV for fallback only.
"""

from __future__ import annotations

import csv
import io
import os
import time
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import certifi
import ccxt
import numpy as np
import pandas as pd
import requests
from urllib3.exceptions import InsecureRequestWarning

# --- Config -----------------------------------------------------------------

TIMEFRAMES_DIV = ("30m", "1h", "2h", "4h", "12h", "1d")
RANK_BUCKET_CHOICES = (
    "strong gainers",
    "mild gainers",
    "neutral",
    "mild losers",
    "strong losers",
)

PERIOD_KEYS = ("1d", "3d", "7d", "30d", "60d")
RETURN_COLUMNS = ("1d", "3d", "7d", "30d", "60d")

PIVOT_LEFT = 2
PIVOT_RIGHT = 2
DIV_CANDLES_PER_TF = 24
CVD_EMA_SPAN = 3
MIN_PRICE_SWING_FRAC = 0.0005  # 0.05%

ENTRY_REACTION_FRAC = 0.01  # 1.0%
ENTRY_NEAR_PIVOT_FRAC = 0.015  # 1.5%
ENTRY_PULLBACK_FRAC = 0.04  # 4.0%

MIN_CVD_DIVERGENCE_FRAC_OF_RANGE = 0.02  # 2% of window range

UNIVERSE_SCAN_LIMIT = 300
MIN_QUOTE_VOLUME_USDT = 1_000_000.0
GAINER_QUAL_RETURN = 0.10
LOSER_QUAL_RETURN = -0.10
OHLCV_SLEEP = 0.12

DEFAULT_SOURCE = os.getenv("SCREENER_SOURCE", "coinalyze").strip().lower()
DEFAULT_EXCHANGE = os.getenv("SCREENER_EXCHANGE", "bybit").strip().lower()
COINALYZE_BASE_URL = "https://api.coinalyze.net/v1"
COINALYZE_BYBIT_CODE = os.getenv("SCREENER_COINALYZE_BYBIT_CODE", "6").strip() or "6"
SSL_NO_VERIFY = os.getenv("SCREENER_SSL_NO_VERIFY", "0").strip() == "1"
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
COINALYZE_INTERVALS = {
    "30m": "30min",
    "1h": "1hour",
    "2h": "2hour",
    "4h": "4hour",
    "12h": "12hour",
    "1d": "daily",
}
SECONDS_PER_TIMEFRAME = {
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d": 24 * 60 * 60,
}


def _clear_proxy_env() -> None:
    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)


def _session_verify() -> bool | str:
    return False if SSL_NO_VERIFY else certifi.where()


def _configure_insecure_warning() -> None:
    if SSL_NO_VERIFY:
        warnings.filterwarnings("ignore", category=InsecureRequestWarning)


def _to_utc_datetime(value: Any) -> pd.Timestamp:
    iv = int(float(value))
    unit = "ms" if iv >= 1_000_000_000_000 else "s"
    return pd.to_datetime(iv, unit=unit, utc=True)


def _now_ts_seconds() -> int:
    return int(datetime.now(UTC).timestamp())


class CoinalyzeClient:
    """Thin Coinalyze client for Bybit futures scans."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        exchange_code: str = COINALYZE_BYBIT_CODE,
    ) -> None:
        self.id = "coinalyze"
        self.exchange_code = exchange_code
        self.api_key = (
            api_key
            or os.getenv("COINALYZE_API_KEY", "")
            or os.getenv("CG_API_KEY", "")
        ).strip()
        if not self.api_key:
            raise ValueError("COINALYZE_API_KEY is required for Coinalyze mode.")
        _clear_proxy_env()
        _configure_insecure_warning()
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = _session_verify()
        self._cache: dict[tuple[Any, ...], Any] = {}
        self.price_exchange = ccxt.bybit(
            {"enableRateLimit": True, "options": {"defaultType": "swap"}}
        )
        self.price_exchange.session.trust_env = False
        self.price_exchange.session.verify = _session_verify()
        if SSL_NO_VERIFY:
            self.price_exchange.verify = False

    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        qp = dict(params or {})
        qp["api_key"] = self.api_key
        key = ("req", path, tuple(sorted(qp.items())))
        if key in self._cache:
            return self._cache[key]
        resp = self.session.get(
            f"{COINALYZE_BASE_URL}{path}",
            params=qp,
            timeout=30,
        )
        time.sleep(OHLCV_SLEEP)
        resp.raise_for_status()
        data = resp.json()
        self._cache[key] = data
        return data

    def future_markets(self) -> list[dict[str, Any]]:
        key = ("future_markets", self.exchange_code)
        if key in self._cache:
            return list(self._cache[key])
        data = self._request("/future-markets")
        markets = [
            row for row in data
            if str(row.get("exchange")) == self.exchange_code
            and bool(row.get("is_perpetual"))
            and bool(row.get("has_ohlcv_data"))
            and bool(row.get("has_buy_sell_data"))
            and str(row.get("quote_asset", "")).upper() == "USDT"
        ]
        self._cache[key] = tuple(markets)
        return markets

    def resolve_symbol(self, symbol: str) -> dict[str, Any] | None:
        key = ("resolve_symbol", symbol)
        if key in self._cache:
            return self._cache[key]
        needle = symbol.upper()
        matches = [
            row for row in self.future_markets()
            if str(row.get("base_asset", "")).upper() == needle
        ]
        if not matches:
            self._cache[key] = None
            return None
        matches.sort(key=lambda row: str(row.get("symbol", "")))
        best = matches[0]
        meta = {
            "coin": needle,
            "symbol": str(best.get("symbol", "")),
            "symbol_on_exchange": str(best.get("symbol_on_exchange", "")),
            "quote_asset": str(best.get("quote_asset", "")),
            "exchange_code": str(best.get("exchange", "")),
            "ccxt_symbol": f"{needle}/USDT:USDT",
        }
        self._cache[key] = meta
        return meta

    def _history_window(self, timeframe: str, limit: int) -> tuple[int, int]:
        sec = SECONDS_PER_TIMEFRAME[timeframe]
        to_ts = _now_ts_seconds()
        from_ts = to_ts - sec * max(limit + 8, limit * 2)
        return from_ts, to_ts

    def fetch_ohlcv_history(
        self,
        market_symbol: str,
        timeframe: str,
        *,
        limit: int,
    ) -> pd.DataFrame:
        interval = COINALYZE_INTERVALS[timeframe]
        from_ts, to_ts = self._history_window(timeframe, limit)
        data = self._request(
            "/ohlcv-history",
            {
                "symbols": market_symbol,
                "interval": interval,
                "from": from_ts,
                "to": to_ts,
            },
        )
        if not data:
            return pd.DataFrame()
        history = data[0].get("history", [])
        df = pd.DataFrame(history)
        if df.empty:
            return df
        df["ts"] = df["t"].map(_to_utc_datetime)
        for col in ("o", "h", "l", "c", "v", "bv"):
            if col not in df:
                df[col] = 0.0
            df[col] = df[col].astype(float)
        df["sell_volume"] = df["v"] - df["bv"]
        df["delta"] = df["bv"] - df["sell_volume"]
        df["cvd"] = df["delta"].cumsum()
        df = df.rename(
            columns={
                "o": "open",
                "h": "high",
                "l": "low",
                "c": "close",
                "v": "volume",
                "bv": "buy_volume",
            }
        )
        df = df[
            ["ts", "open", "high", "low", "close", "volume", "buy_volume", "sell_volume", "cvd"]
        ].sort_values("ts")
        df = df.tail(limit).reset_index(drop=True)
        return df

    def usdt_perp_symbols(
        self,
        *,
        limit: int = UNIVERSE_SCAN_LIMIT,
        min_quote_volume_usdt: float = MIN_QUOTE_VOLUME_USDT,
    ) -> list[str]:
        key = ("usdt_perp_symbols", limit, min_quote_volume_usdt)
        if key in self._cache:
            return list(self._cache[key])
        allowed = {
            str(row.get("base_asset", "")).upper()
            for row in self.future_markets()
        }
        self.price_exchange.load_markets()
        symbols: list[str] = []
        for sym, market in self.price_exchange.markets.items():
            if not market.get("swap") or not market.get("linear"):
                continue
            if market.get("quote") != "USDT":
                continue
            base = str(market.get("base", "")).upper()
            if base not in allowed:
                continue
            symbols.append(sym)
        tickers = self.price_exchange.fetch_tickers(symbols)
        ranked: list[tuple[str, float]] = []
        for sym in symbols:
            ticker = tickers.get(sym)
            if not ticker:
                continue
            qv = float(ticker.get("quoteVolume") or 0)
            if qv < min_quote_volume_usdt:
                continue
            base = str(self.price_exchange.market(sym).get("base", "")).upper()
            ranked.append((base, qv))
        ranked.sort(key=lambda x: x[1], reverse=True)
        seen_out: set[str] = set()
        out: list[str] = []
        for coin, _ in ranked:
            if coin in seen_out:
                continue
            seen_out.add(coin)
            out.append(coin)
            if len(out) >= limit:
                break
        self._cache[key] = tuple(out)
        return out


MarketClient = ccxt.Exchange | CoinalyzeClient


def make_exchange(exchange_id: str = DEFAULT_EXCHANGE) -> MarketClient:
    source = DEFAULT_SOURCE
    if source == "coinalyze":
        return CoinalyzeClient(exchange_code=COINALYZE_BYBIT_CODE)
    exchange_id = exchange_id.strip().lower()
    opts = {
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    }
    if exchange_id == "binance":
        ex = ccxt.binance(opts)
    elif exchange_id == "bybit":
        ex = ccxt.bybit(opts)
    else:
        raise ValueError(f"Unsupported exchange: {exchange_id}")
    _clear_proxy_env()
    _configure_insecure_warning()
    ex.session.trust_env = False
    ex.session.verify = _session_verify()
    if SSL_NO_VERIFY:
        ex.verify = False
    return ex


def usdt_perp_symbols(
    ex: MarketClient,
    limit: int = UNIVERSE_SCAN_LIMIT,
    min_quote_volume_usdt: float = MIN_QUOTE_VOLUME_USDT,
) -> list[str]:
    if isinstance(ex, CoinalyzeClient):
        return ex.usdt_perp_symbols(
            limit=limit,
            min_quote_volume_usdt=min_quote_volume_usdt,
        )
    ex.load_markets()
    symbols: list[str] = []
    for sym, m in ex.markets.items():
        if not m.get("swap") or not m.get("linear"):
            continue
        if m.get("quote") != "USDT":
            continue
        if not m.get("active", True):
            continue
        symbols.append(sym)
    tickers = ex.fetch_tickers(symbols)
    ranked: list[tuple[str, float]] = []
    for sym in symbols:
        t = tickers.get(sym)
        if not t:
            continue
        qv = float(t.get("quoteVolume") or 0)
        if qv < min_quote_volume_usdt:
            continue
        ranked.append((sym, qv))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in ranked[:limit]]


def ohlcv_df(ohlcv: list) -> pd.DataFrame:
    df = pd.DataFrame(
        ohlcv, columns=["ts", "open", "high", "low", "close", "volume"]
    )
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def futures_klines_to_df(raw: list) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "ts": [r[0] for r in raw],
            "open": [float(r[1]) for r in raw],
            "high": [float(r[2]) for r in raw],
            "low": [float(r[3]) for r in raw],
            "close": [float(r[4]) for r in raw],
            "volume": [float(r[5]) for r in raw],
            "taker_buy_base": [float(r[9]) for r in raw],
        }
    )
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    delta = 2.0 * df["taker_buy_base"] - df["volume"]
    df["cvd"] = delta.cumsum()
    return df


def futures_ohlcv_to_signed_volume_df(raw: list) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "ts": [r[0] for r in raw],
            "open": [float(r[1]) for r in raw],
            "high": [float(r[2]) for r in raw],
            "low": [float(r[3]) for r in raw],
            "close": [float(r[4]) for r in raw],
            "volume": [float(r[5]) for r in raw],
        }
    )
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    direction = np.sign(df["close"].diff())
    direction.iloc[0] = np.sign(df["close"].iloc[0] - df["open"].iloc[0])
    direction = direction.replace(0, np.nan).ffill().fillna(0.0)
    df["cvd"] = (df["volume"] * direction).cumsum()
    return df


def pivot_high_indices(
    high: np.ndarray, left: int = PIVOT_LEFT, right: int = PIVOT_RIGHT
) -> list[int]:
    n = len(high)
    idxs: list[int] = []
    for i in range(left, n - right):
        window = high[i - left : i + right + 1]
        if high[i] == np.max(window) and int(np.argmax(window)) == left:
            idxs.append(i)
    return idxs


def pivot_low_indices(
    low: np.ndarray, left: int = PIVOT_LEFT, right: int = PIVOT_RIGHT
) -> list[int]:
    n = len(low)
    idxs: list[int] = []
    for i in range(left, n - right):
        window = low[i - left : i + right + 1]
        if low[i] == np.min(window) and int(np.argmin(window)) == left:
            idxs.append(i)
    return idxs


def last_two_pivots(idxs: list[int], min_sep: int = 3) -> tuple[int, int] | None:
    if len(idxs) < 2:
        return None
    a, b = idxs[-2], idxs[-1]
    if b - a < min_sep:
        if len(idxs) >= 3:
            a, b = idxs[-3], idxs[-1]
        else:
            return None
    return a, b


def bull_div_at_lows(price_low: float, price_low_prev: float, cvd_a: float, cvd_b: float) -> bool:
    return price_low > price_low_prev and cvd_b < cvd_a


def bear_div_at_highs(price_high: float, price_high_prev: float, cvd_a: float, cvd_b: float) -> bool:
    return price_high < price_high_prev and cvd_b > cvd_a


def _divergence_strength_ok(
    p1: float,
    p2: float,
    c1: float,
    c2: float,
    mode: Literal["bull", "bear"],
    pressure: np.ndarray,
) -> bool:
    pr = float(np.nanmax(pressure) - np.nanmin(pressure))
    if not np.isfinite(pr) or pr <= 0:
        return False
    k = MIN_CVD_DIVERGENCE_FRAC_OF_RANGE
    f = MIN_PRICE_SWING_FRAC
    if mode == "bull":
        if p2 <= p1 * (1.0 + f):
            return False
        if c2 >= c1:
            return False
        if (c1 - c2) / pr < k:
            return False
    else:
        if p2 >= p1 * (1.0 - f):
            return False
        if c2 <= c1:
            return False
        if (c2 - c1) / pr < k:
            return False
    return True


def divergence_on_tf(
    df: pd.DataFrame,
    mode: Literal["bull", "bear"],
) -> bool:
    if len(df) < DIV_CANDLES_PER_TF:
        return False
    df = df.tail(DIV_CANDLES_PER_TF).copy()
    pressure = df["cvd"].ewm(span=CVD_EMA_SPAN, adjust=False).mean().to_numpy()
    if mode == "bull":
        price = df["low"].to_numpy()
        pivots = pivot_low_indices(price)
    else:
        price = df["high"].to_numpy()
        pivots = pivot_high_indices(price)
    pair = last_two_pivots(pivots)
    if pair is None:
        return False
    i1, i2 = pair
    p1, p2 = float(price[i1]), float(price[i2])
    c1, c2 = float(pressure[i1]), float(pressure[i2])
    if mode == "bull":
        if not bull_div_at_lows(p2, p1, c1, c2):
            return False
    else:
        if not bear_div_at_highs(p2, p1, c1, c2):
            return False
    return _divergence_strength_ok(p1, p2, c1, c2, mode, pressure)


@dataclass
class ScreenResult:
    symbol: str
    period: str
    ret: float
    returns: dict[str, float | None]
    div_hits: list[str]
    score: int


@dataclass
class EntryWatch:
    symbol: str
    timeframe: str
    mode: Literal["bull", "bear"]
    status: str
    pivot_price: float
    current_price: float
    invalidation_price: float
    distance_pct: float
    reaction_pct: float


def entry_watch_on_tf(
    df: pd.DataFrame,
    mode: Literal["bull", "bear"],
) -> EntryWatch | None:
    if len(df) < DIV_CANDLES_PER_TF:
        return None
    df = df.tail(DIV_CANDLES_PER_TF).copy()
    pressure = df["cvd"].ewm(span=CVD_EMA_SPAN, adjust=False).mean().to_numpy()
    closes = df["close"].to_numpy()
    if mode == "bull":
        price = df["low"].to_numpy()
        pivots = pivot_low_indices(price)
    else:
        price = df["high"].to_numpy()
        pivots = pivot_high_indices(price)
    pair = last_two_pivots(pivots)
    if pair is None:
        return None
    i1, i2 = pair
    p1, p2 = float(price[i1]), float(price[i2])
    c1, c2 = float(pressure[i1]), float(pressure[i2])
    if mode == "bull":
        if not bull_div_at_lows(p2, p1, c1, c2):
            return None
    else:
        if not bear_div_at_highs(p2, p1, c1, c2):
            return None
    if not _divergence_strength_ok(p1, p2, c1, c2, mode, pressure):
        return None
    if i2 >= len(closes) - 1:
        return None

    latest = float(closes[-1])
    post_close = closes[i2 + 1 :]
    if len(post_close) == 0:
        return None

    if mode == "bull":
        reaction_high = float(np.nanmax(post_close))
        reaction_pct = (reaction_high / p2) - 1.0
        if reaction_pct < ENTRY_REACTION_FRAC:
            return None
        if latest <= p2:
            return EntryWatch("", "", mode, "invalidated", p2, latest, p2, (latest / p2) - 1.0, reaction_pct)
        distance_pct = (latest / p2) - 1.0
        if distance_pct <= ENTRY_NEAR_PIVOT_FRAC:
            status = "near next HL"
        elif distance_pct <= ENTRY_PULLBACK_FRAC:
            status = "pullback forming"
        else:
            status = "waiting pullback"
    else:
        reaction_low = float(np.nanmin(post_close))
        reaction_pct = 1.0 - (reaction_low / p2)
        if reaction_pct < ENTRY_REACTION_FRAC:
            return None
        if latest >= p2:
            return EntryWatch("", "", mode, "invalidated", p2, latest, p2, 1.0 - (latest / p2), reaction_pct)
        distance_pct = 1.0 - (latest / p2)
        if distance_pct <= ENTRY_NEAR_PIVOT_FRAC:
            status = "near next LH"
        elif distance_pct <= ENTRY_PULLBACK_FRAC:
            status = "pullback forming"
        else:
            status = "waiting pullback"

    return EntryWatch("", "", mode, status, p2, latest, p2, distance_pct, reaction_pct)


def fetch_ohlcv_safe(
    ex: MarketClient, symbol: str, timeframe: str, limit: int = 500
) -> pd.DataFrame | None:
    try:
        if isinstance(ex, CoinalyzeClient):
            meta = ex.resolve_symbol(symbol)
            if meta is None:
                return None
            raw = ex.price_exchange.fetch_ohlcv(
                meta["ccxt_symbol"], timeframe=timeframe, limit=limit
            )
            time.sleep(OHLCV_SLEEP)
            if not raw:
                return None
            return ohlcv_df(raw)
        raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        time.sleep(OHLCV_SLEEP)
        if not raw:
            return None
        return ohlcv_df(raw)
    except Exception:
        time.sleep(OHLCV_SLEEP)
        return None


def fetch_futures_ohlcv_cvd_safe(
    ex: MarketClient, symbol: str, timeframe: str, limit: int = 500
) -> pd.DataFrame | None:
    try:
        if isinstance(ex, CoinalyzeClient):
            meta = ex.resolve_symbol(symbol)
            if meta is None:
                return None
            return ex.fetch_ohlcv_history(meta["symbol"], timeframe, limit=limit)

        ex.load_markets()
        m = ex.market(symbol)
        if not m.get("swap"):
            return None
        if ex.id == "binance":
            raw = ex.fapiPublicGetKlines(
                {"symbol": m["id"], "interval": timeframe, "limit": limit}
            )
        else:
            raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        time.sleep(OHLCV_SLEEP)
        if not raw:
            return None
        if ex.id == "binance":
            return futures_klines_to_df(raw)
        return futures_ohlcv_to_signed_volume_df(raw)
    except Exception:
        time.sleep(OHLCV_SLEEP)
        return None


def period_return_from_ohlcv(
    df: pd.DataFrame, bars_ago: int
) -> float | None:
    if len(df) <= bars_ago:
        return None
    c0 = float(df["close"].iloc[-1])
    c1 = float(df["close"].iloc[-1 - bars_ago])
    if c1 == 0:
        return None
    return (c0 / c1) - 1.0


def compute_period_return(
    ex: MarketClient, symbol: str, period: str
) -> float | None:
    if period not in PERIOD_KEYS:
        return None
    days = {"1d": 1, "3d": 3, "7d": 7, "30d": 30, "60d": 60}[period]
    limit = days + 5
    df = fetch_ohlcv_safe(ex, symbol, "1d", limit=limit)
    if df is None or len(df) <= days:
        return None
    return period_return_from_ohlcv(df, days)


def compute_all_daily_returns(
    ex: MarketClient, symbol: str
) -> dict[str, float | None]:
    limit = 65
    df = fetch_ohlcv_safe(ex, symbol, "1d", limit=limit)
    out: dict[str, float | None] = {k: None for k in RETURN_COLUMNS}
    if df is None or len(df) < 2:
        return out
    days_map = {"1d": 1, "3d": 3, "7d": 7, "30d": 30, "60d": 60}
    for key, days in days_map.items():
        if len(df) <= days:
            out[key] = None
        else:
            out[key] = period_return_from_ohlcv(df, days)
    return out


def rank_by_return(
    ex: MarketClient,
    symbols: list[str],
    period: str,
    top_n: int,
    losers: bool,
    *,
    gainer_min_return: float = GAINER_QUAL_RETURN,
    loser_max_return: float = LOSER_QUAL_RETURN,
    use_qualification: bool = True,
    neutral_return_band: float | None = None,
    rank_bucket: str | None = None,
) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    bucket_neutral_band = (
        neutral_return_band if neutral_return_band is not None else 0.05
    )
    for sym in symbols:
        r = compute_period_return(ex, sym, period)
        if r is None:
            continue
        if rank_bucket is not None:
            if rank_bucket == "strong gainers":
                if r < gainer_min_return:
                    continue
            elif rank_bucket == "mild gainers":
                if not (bucket_neutral_band < r < gainer_min_return):
                    continue
            elif rank_bucket == "neutral":
                if abs(r) > bucket_neutral_band:
                    continue
            elif rank_bucket == "mild losers":
                if not (loser_max_return < r < -bucket_neutral_band):
                    continue
            elif rank_bucket == "strong losers":
                if r > loser_max_return:
                    continue
            else:
                raise ValueError(f"Unsupported rank bucket: {rank_bucket}")
        elif neutral_return_band is not None:
            if abs(r) > neutral_return_band:
                continue
        elif use_qualification:
            if losers:
                if r > loser_max_return:
                    continue
            else:
                if r < gainer_min_return:
                    continue
        rows.append((sym, r))
    if rank_bucket == "neutral" or (rank_bucket is None and neutral_return_band is not None):
        rows.sort(key=lambda x: abs(x[1]))
    elif rank_bucket in ("strong losers", "mild losers"):
        rows.sort(key=lambda x: x[1])
    else:
        rows.sort(key=lambda x: x[1], reverse=not losers)
    return rows[:top_n]


def scan_divergences(
    ex: MarketClient,
    symbols: list[str],
    mode: Literal["bull", "bear"],
    timeframes: tuple[str, ...] = TIMEFRAMES_DIV,
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for sym in symbols:
        hits: list[str] = []
        for tf in timeframes:
            df = fetch_futures_ohlcv_cvd_safe(
                ex, sym, tf, limit=DIV_CANDLES_PER_TF
            )
            if df is None:
                continue
            if divergence_on_tf(df, mode):
                hits.append(tf)
        if hits:
            out[sym] = hits
    return out


def scan_entry_watches(
    ex: MarketClient,
    rows: list[ScreenResult],
    mode: Literal["bull", "bear"],
    timeframes: tuple[str, ...] = TIMEFRAMES_DIV,
) -> list[EntryWatch]:
    watches: list[EntryWatch] = []
    for row in rows:
        for tf in row.div_hits:
            if tf not in timeframes:
                continue
            df = fetch_futures_ohlcv_cvd_safe(
                ex, row.symbol, tf, limit=DIV_CANDLES_PER_TF
            )
            if df is None:
                continue
            watch = entry_watch_on_tf(df, mode)
            if watch is None or watch.status == "invalidated":
                continue
            watch.symbol = row.symbol
            watch.timeframe = tf
            watches.append(watch)
    watches.sort(key=lambda x: (x.status != "near next HL" and x.status != "near next LH", x.distance_pct))
    return watches


def run_screen(
    ex: MarketClient,
    period: str,
    rank_losers: bool,
    div_mode: Literal["bull", "bear"],
    top_universe: int = 25,
    out_limit: int = 20,
    *,
    min_quote_volume_usdt: float = MIN_QUOTE_VOLUME_USDT,
    universe_scan_limit: int = UNIVERSE_SCAN_LIMIT,
    gainer_min_return: float = GAINER_QUAL_RETURN,
    loser_max_return: float = LOSER_QUAL_RETURN,
    use_qualification: bool = True,
    neutral_return_band: float | None = None,
    div_timeframes: tuple[str, ...] = TIMEFRAMES_DIV,
    rank_bucket: str | None = None,
) -> list[ScreenResult]:
    uni = usdt_perp_symbols(
        ex, limit=universe_scan_limit, min_quote_volume_usdt=min_quote_volume_usdt
    )
    ranked = rank_by_return(
        ex,
        uni,
        period,
        top_n=top_universe,
        losers=rank_losers,
        gainer_min_return=gainer_min_return,
        loser_max_return=loser_max_return,
        use_qualification=use_qualification,
        neutral_return_band=neutral_return_band,
        rank_bucket=rank_bucket,
    )
    candidates = [s for s, _ in ranked]
    div_map = scan_divergences(ex, candidates, div_mode, timeframes=div_timeframes)
    results: list[ScreenResult] = []
    for sym, ret in ranked:
        if sym not in div_map:
            continue
        hits = div_map[sym]
        results.append(
            ScreenResult(
                symbol=sym,
                period=period,
                ret=ret,
                returns=compute_all_daily_returns(ex, sym),
                div_hits=hits,
                score=len(hits),
            )
        )
    results.sort(key=lambda x: (x.score, abs(x.ret)), reverse=True)
    return results[:out_limit]


def _pct_cell(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{x * 100:+.2f}"


def _price_cell(x: float) -> str:
    if x >= 100:
        return f"{x:.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.8f}".rstrip("0").rstrip(".")


def _ascii_table(rows: list[ScreenResult]) -> str:
    head = [
        "#",
        "Symbol",
        "1D %",
        "3D %",
        "7D %",
        "30D %",
        "60D %",
        "TFs",
        "Score",
    ]
    body: list[list[str]] = []
    for i, r in enumerate(rows, 1):
        body.append(
            [
                str(i),
                r.symbol,
                _pct_cell(r.returns.get("1d")),
                _pct_cell(r.returns.get("3d")),
                _pct_cell(r.returns.get("7d")),
                _pct_cell(r.returns.get("30d")),
                _pct_cell(r.returns.get("60d")),
                ",".join(r.div_hits),
                str(r.score),
            ]
        )
    widths = [len(h) for h in head]
    for row in body:
        for j, cell in enumerate(row):
            widths[j] = max(widths[j], len(cell))

    def sep() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def fmt_row(cells: list[str]) -> str:
        return "|" + "|".join(f" {cells[j]:<{widths[j]}} " for j in range(len(cells))) + "|"

    out = [sep(), fmt_row(head), sep()]
    for row in body:
        out.append(fmt_row(row))
    out.append(sep())
    return "\n".join(out)


def _entry_watch_table(rows: list[EntryWatch]) -> str:
    head = [
        "#",
        "Symbol",
        "TF",
        "Status",
        "Pivot",
        "Invalid Close",
        "Current",
        "Dist %",
        "React %",
    ]
    body: list[list[str]] = []
    for i, r in enumerate(rows, 1):
        body.append(
            [
                str(i),
                r.symbol,
                r.timeframe,
                r.status,
                _price_cell(r.pivot_price),
                _price_cell(r.invalidation_price),
                _price_cell(r.current_price),
                f"{r.distance_pct * 100:+.2f}",
                f"{r.reaction_pct * 100:+.2f}",
            ]
        )
    widths = [len(h) for h in head]
    for row in body:
        for j, cell in enumerate(row):
            widths[j] = max(widths[j], len(cell))

    def sep() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def fmt_row(cells: list[str]) -> str:
        return "|" + "|".join(f" {cells[j]:<{widths[j]}} " for j in range(len(cells))) + "|"

    out = [sep(), fmt_row(head), sep()]
    for row in body:
        out.append(fmt_row(row))
    out.append(sep())
    return "\n".join(out)


def format_results(title: str, rows: list[ScreenResult]) -> str:
    lines: list[str] = [title, ""]
    if not rows:
        lines.append("(no matches)")
        return "\n".join(lines)
    lines.append(_ascii_table(rows))
    return "\n".join(lines)


def format_entry_watches(title: str, rows: list[EntryWatch]) -> str:
    lines: list[str] = [title, ""]
    if not rows:
        lines.append("(no entry-watch matches)")
        return "\n".join(lines)
    lines.append(_entry_watch_table(rows))
    return "\n".join(lines)


def format_results_csv(rows: list[ScreenResult]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(
        [
            "rank",
            "symbol",
            "return_1d_pct",
            "return_3d_pct",
            "return_7d_pct",
            "return_30d_pct",
            "return_60d_pct",
            "rank_period_used_for_sort",
            "timeframes",
            "score",
        ]
    )
    for i, r in enumerate(rows, 1):
        def rp(k: str) -> str | float:
            v = r.returns.get(k)
            return "" if v is None else round(v * 100, 4)

        w.writerow(
            [
                i,
                r.symbol,
                rp("1d"),
                rp("3d"),
                rp("7d"),
                rp("30d"),
                rp("60d"),
                r.period,
                ";".join(r.div_hits),
                r.score,
            ]
        )
    return buf.getvalue()
