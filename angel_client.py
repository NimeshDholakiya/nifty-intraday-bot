"""Angel One SmartAPI read-only market data client for NIFTY 50 & NFO options.

Safety: This client intentionally provides read-only data methods (candles & LTPs)
for paper trading and live signal alerts. It never places live orders.

Production hardening for live NSE intraday sessions:
  - Explicit IST (UTC+05:30) timestamps regardless of host OS timezone
  - Startup ScripMaster download & in-memory NIFTY NFO indexing (<1ms strike lookup)
  - Auto-relogin via TOTP if Angel One session/JWT expires mid-day
  - Fast batch LTP fetch via getMarketData("LTP", ...) with per-symbol ltpData fallback
  - Automatic fallback between "Nifty 50" and "NIFTY" spot symbols
"""

from __future__ import annotations

import csv
import json
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from config import BotConfig
from models import Candle, OptionContract, OptionType

IST = timezone(timedelta(hours=5, minutes=30))
SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)
NIFTY_SPOT_TOKEN = "99926000"
NIFTY_SPOT_SYMBOLS = ("Nifty 50", "NIFTY")
AUTH_ERROR_CODES = ("AG8001", "AG8002", "AG8003", "AB1010", "AB8050")


def now_ist() -> datetime:
    """Return current Indian Standard Time (IST) as naive datetime for consistent comparisons."""
    return datetime.now(IST).replace(tzinfo=None)


def _parse_angel_expiry(expiry_str: str) -> date | None:
    if not expiry_str:
        return None
    for fmt in ("%d%b%Y", "%d%B%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(expiry_str.strip().upper(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_angel_strike(raw_strike: Any) -> int:
    """Angel One ScripMaster stores option strikes multiplied by 100 (e.g. 2450000.0)."""
    val = float(raw_strike or 0.0)
    if val > 100000:
        val /= 100.0
    return int(round(val))


def _parse_candle_timestamp(ts_str: str) -> datetime:
    """Parse ISO timestamps returned by SmartAPI getCandleData into naive IST."""
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST).replace(tzinfo=None)
    return dt


def _is_auth_error(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return False
    code = str(resp.get("errorcode") or resp.get("errorCode") or "").upper()
    msg = str(resp.get("message") or "").lower()
    return code in AUTH_ERROR_CODES or "invalid token" in msg or "token expired" in msg


class AngelOneClient:
    """Connects to Angel One SmartAPI using TOTP for live NIFTY 50 & option chain data."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._smart_api: Any = None
        self._scrip_master: list[dict[str, Any]] | None = None
        self._nifty_future: dict[str, Any] | None = None
        self._nifty_options_index: list[tuple[date, int, OptionType, dict[str, Any]]] | None = None
        self._nifty_spot_symbol: str = NIFTY_SPOT_SYMBOLS[0]

    def login(self) -> None:
        missing = [
            name
            for name, val in [
                ("ANGEL_API_KEY", self.config.angel_api_key),
                ("ANGEL_CLIENT_CODE", self.config.angel_client_code),
                ("ANGEL_PIN", self.config.angel_pin),
                ("ANGEL_TOTP_SECRET", self.config.angel_totp_secret),
            ]
            if not val
        ]
        if missing:
            raise ValueError(
                f"Missing Angel One credentials in .env: {', '.join(missing)}. "
                "Copy .env.example to .env and fill in your SmartAPI details."
            )

        try:
            import pyotp
            from SmartApi import SmartConnect
        except ImportError as exc:
            raise ImportError(
                "Angel One dependencies not installed. Run:\n"
                "  pip install -r requirements.txt"
            ) from exc

        smart_api = SmartConnect(api_key=self.config.angel_api_key)
        totp = pyotp.TOTP(self.config.angel_totp_secret.replace(" ", "")).now()
        session = smart_api.generateSession(
            self.config.angel_client_code,
            self.config.angel_pin,
            totp,
        )
        if not session or not session.get("status"):
            raise RuntimeError(f"Angel One login failed: {session}")

        self._smart_api = smart_api

    def warmup_instruments(self) -> dict[str, Any]:
        """Download ScripMaster and pre-index NIFTY Spot, Futures, and Options at startup."""
        master = self.load_scrip_master()
        today = now_ist().date()

        opt_index: list[tuple[date, int, OptionType, dict[str, Any]]] = []
        futures: list[tuple[date, dict[str, Any]]] = []

        for row in master:
            token = str(row.get("token", ""))
            exch = row.get("exch_seg")
            name = row.get("name")
            inst = row.get("instrumenttype")

            if exch == "NSE" and token == NIFTY_SPOT_TOKEN and row.get("symbol"):
                self._nifty_spot_symbol = str(row["symbol"])
            elif exch == "NFO" and name == "NIFTY" and inst == "FUTIDX":
                exp = _parse_angel_expiry(str(row.get("expiry", "")))
                if exp and exp >= today:
                    futures.append((exp, row))
            elif exch == "NFO" and name == "NIFTY" and inst == "OPTIDX":
                sym = str(row.get("symbol", ""))
                if sym.endswith("CE"):
                    opt_type: OptionType = "CE"
                elif sym.endswith("PE"):
                    opt_type = "PE"
                else:
                    continue
                exp = _parse_angel_expiry(str(row.get("expiry", "")))
                if exp and exp >= today:
                    strike = _parse_angel_strike(row.get("strike"))
                    opt_index.append((exp, strike, opt_type, row))

        futures.sort(key=lambda pair: pair[0])
        self._nifty_future = futures[0][1] if futures else None
        self._nifty_options_index = opt_index

        nearest_opt_expiry = min((e for e, _, _, _ in opt_index), default=None)
        detected_lot_size = (
            int(opt_index[0][3].get("lotsize") or self.config.lot_size)
            if opt_index
            else self.config.lot_size
        )
        return {
            "total_scrips": len(master),
            "spot_symbol": self._nifty_spot_symbol,
            "nearest_future": self._nifty_future.get("symbol") if self._nifty_future else None,
            "nearest_option_expiry": nearest_opt_expiry.isoformat() if nearest_opt_expiry else None,
            "indexed_options": len(opt_index),
            "detected_lot_size": detected_lot_size,
        }

    def logout(self) -> None:
        if self._smart_api is not None:
            try:
                self._smart_api.terminateSession(self.config.angel_client_code)
            except Exception:
                pass
            self._smart_api = None

    def load_scrip_master(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        if self._scrip_master is not None and not force_refresh:
            return self._scrip_master

        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self.config.data_dir / f"scrip_master_{now_ist().date().isoformat()}.json"

        if not cache_path.exists() or force_refresh:
            req = urllib.request.Request(
                SCRIP_MASTER_URL,
                headers={"User-Agent": "nifty-intraday-bot/1.0"},
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw_bytes = resp.read()
            cache_path.write_bytes(raw_bytes)

        self._scrip_master = json.loads(cache_path.read_text(encoding="utf-8"))
        return self._scrip_master

    def get_nearest_nifty_future(self) -> dict[str, Any] | None:
        """Find nearest active NIFTY Futures contract (provides real volume for VWAP)."""
        if self._nifty_future is None and self._nifty_options_index is None:
            self.warmup_instruments()
        return self._nifty_future

    def _call_with_relogin(self, fn: Callable[[], Any], max_attempts: int = 2) -> Any:
        """Execute a SmartAPI call and auto-relogin once if session token expired."""
        self._ensure_logged_in()
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                resp = fn()
                if _is_auth_error(resp) and attempt + 1 < max_attempts:
                    self.login()
                    continue
                return resp
            except Exception as exc:
                last_exc = exc
                if attempt + 1 < max_attempts:
                    time.sleep(1.0)
                    try:
                        self.login()
                    except Exception:
                        pass
                    continue
                raise
        if last_exc:
            raise last_exc
        return None

    def get_nifty_spot_ltp(self) -> float:
        self._ensure_logged_in()
        tried: list[str] = []
        symbols_to_try = [self._nifty_spot_symbol] + [
            s for s in NIFTY_SPOT_SYMBOLS if s != self._nifty_spot_symbol
        ]
        last_resp: Any = None
        for sym in symbols_to_try:
            tried.append(sym)
            resp = self._call_with_relogin(
                lambda s=sym: self._smart_api.ltpData("NSE", s, NIFTY_SPOT_TOKEN)
            )
            last_resp = resp
            if resp and resp.get("status") and resp.get("data") and resp["data"].get("ltp"):
                self._nifty_spot_symbol = sym
                return float(resp["data"]["ltp"])

        raise RuntimeError(f"Failed to fetch NIFTY 50 spot LTP (tried {tried}): {last_resp}")

    def get_intraday_candles(
        self,
        lookback_days: int = 4,
        prefer_futures_for_vwap: bool = True,
    ) -> tuple[list[Candle], str]:
        """Fetch 5-minute candles (includes prior days for Supertrend/ADX/15m warmup).

        Uses nearest NIFTY Futures when available so VWAP uses genuine traded volume;
        falls back cleanly to NIFTY 50 Spot index (token 99926000) if futures lookup fails.
        """
        self._ensure_logged_in()

        exchange = "NSE"
        token = NIFTY_SPOT_TOKEN
        source_label = "NIFTY 50 Spot (NSE:99926000)"

        if prefer_futures_for_vwap:
            fut = self.get_nearest_nifty_future()
            if fut:
                exchange = "NFO"
                token = str(fut["token"])
                source_label = f"{fut.get('symbol')} (NFO:{token})"

        now = now_ist()
        from_dt = (now - timedelta(days=lookback_days)).replace(hour=9, minute=15, second=0)
        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": self.config.candle_interval,
            "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate": now.strftime("%Y-%m-%d %H:%M"),
        }

        resp = self._call_with_relogin(lambda: self._smart_api.getCandleData(params))
        if not resp or not resp.get("status") or not resp.get("data"):
            if exchange == "NFO":
                fallback_params = dict(params)
                fallback_params["exchange"] = "NSE"
                fallback_params["symboltoken"] = NIFTY_SPOT_TOKEN
                source_label = "NIFTY 50 Spot (NSE:99926000)"
                resp = self._call_with_relogin(
                    lambda: self._smart_api.getCandleData(fallback_params)
                )

        if not resp or not resp.get("status") or not resp.get("data"):
            raise RuntimeError(f"Failed to fetch candle data: {resp}")

        candles: list[Candle] = []
        for row in resp["data"]:
            candles.append(
                Candle(
                    timestamp=_parse_candle_timestamp(str(row[0])),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5] or 0.0),
                )
            )
        candles.sort(key=lambda c: c.timestamp)
        return candles, source_label

    def fetch_historical_candles_chunked(
        self,
        days: int = 60,
        chunk_days: int = 15,
        prefer_futures: bool = True,
    ) -> tuple[list[Candle], str]:
        """Download multi-day 5-min NSE candles in safe chunks for real backtesting."""
        self._ensure_logged_in()

        exchange = "NSE"
        token = NIFTY_SPOT_TOKEN
        source_label = "NIFTY 50 Spot (NSE:99926000)"

        if prefer_futures:
            fut = self.get_nearest_nifty_future()
            if fut:
                exchange = "NFO"
                token = str(fut["token"])
                source_label = f"{fut.get('symbol')} (NFO:{token})"

        end_dt = now_ist()
        start_dt = (end_dt - timedelta(days=days)).replace(hour=9, minute=15, second=0)

        deduped: dict[datetime, Candle] = {}
        cursor = start_dt

        while cursor < end_dt:
            chunk_end = min(cursor + timedelta(days=chunk_days), end_dt)
            params = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": self.config.candle_interval,
                "fromdate": cursor.strftime("%Y-%m-%d %H:%M"),
                "todate": chunk_end.strftime("%Y-%m-%d %H:%M"),
            }
            resp = self._call_with_relogin(lambda p=params: self._smart_api.getCandleData(p))
            if resp and resp.get("status") and resp.get("data"):
                for row in resp["data"]:
                    ts = _parse_candle_timestamp(str(row[0]))
                    deduped[ts] = Candle(
                        timestamp=ts,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5] or 0.0),
                    )
            cursor = chunk_end + timedelta(minutes=5)
            time.sleep(0.5)  # Respect Angel One SmartAPI historical rate limits

        candles = sorted(deduped.values(), key=lambda c: c.timestamp)
        return candles, source_label

    @staticmethod
    def save_candles_to_csv(candles: list[Candle], csv_path: Path) -> Path:
        """Save downloaded OHLCV candles to CSV for repeatable offline backtesting."""
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for c in candles:
                writer.writerow(
                    [
                        c.timestamp.isoformat(sep=" "),
                        f"{c.open:.2f}",
                        f"{c.high:.2f}",
                        f"{c.low:.2f}",
                        f"{c.close:.2f}",
                        f"{c.volume:.0f}",
                    ]
                )
        return csv_path

    def _fetch_batch_nfo_ltps(self, tokens: list[str]) -> dict[str, float]:
        """Fetch multiple NFO option LTPs in one SmartAPI call if getMarketData is available."""
        if not tokens or not hasattr(self._smart_api, "getMarketData"):
            return {}
        try:
            resp = self._call_with_relogin(
                lambda: self._smart_api.getMarketData("LTP", {"NFO": tokens})
            )
            if not resp or not resp.get("status") or not resp.get("data"):
                return {}
            fetched = resp["data"].get("fetched") or []
            result: dict[str, float] = {}
            for item in fetched:
                tok = str(item.get("symbolToken") or "")
                ltp = float(item.get("ltp") or 0.0)
                if tok and ltp > 0:
                    result[tok] = ltp
            return result
        except Exception:
            return {}

    def get_candidate_options(
        self,
        spot_price: float,
        option_type: OptionType,
        strike_range_pts: int | None = None,
    ) -> list[OptionContract]:
        """Fetch live LTPs for nearest-expiry NIFTY CE/PE strikes near current spot."""
        self._ensure_logged_in()
        if self._nifty_options_index is None:
            self.warmup_instruments()

        index = self._nifty_options_index or []
        today = now_ist().date()
        max_dist = strike_range_pts or self.config.max_strike_distance_pts

        matched = [
            (exp, strike, row)
            for exp, strike, otype, row in index
            if otype == option_type and exp >= today and abs(strike - spot_price) <= max_dist
        ]
        if not matched:
            return []

        nearest_expiry = min(exp for exp, _, _ in matched)
        expiry_rows = [
            (strike, row) for exp, strike, row in matched if exp == nearest_expiry
        ]
        expiry_rows.sort(key=lambda item: abs(item[0] - spot_price))

        tokens = [str(row["token"]) for _, row in expiry_rows]
        batch_ltps = self._fetch_batch_nfo_ltps(tokens)

        contracts: list[OptionContract] = []
        for strike, row in expiry_rows:
            symbol = str(row["symbol"])
            token = str(row["token"])
            lot_size = int(row.get("lotsize") or self.config.lot_size)

            if token in batch_ltps:
                ltp = batch_ltps[token]
            else:
                ltp = self.get_option_ltp(symbol, token)
                time.sleep(0.35)  # Rate-limit guard for fallback per-symbol calls

            if ltp > 0:
                contracts.append(
                    OptionContract(
                        symbol=symbol,
                        token=token,
                        exchange="NFO",
                        strike=strike,
                        option_type=option_type,
                        expiry=nearest_expiry.isoformat(),
                        lot_size=lot_size,
                        ltp=ltp,
                    )
                )

        return contracts

    def get_option_ltp(self, symbol: str, token: str) -> float:
        self._ensure_logged_in()
        try:
            resp = self._call_with_relogin(
                lambda: self._smart_api.ltpData("NFO", symbol, token)
            )
            if not resp or not resp.get("status") or not resp.get("data"):
                return 0.0
            return float(resp["data"].get("ltp") or 0.0)
        except Exception:
            return 0.0

    def _ensure_logged_in(self) -> None:
        if self._smart_api is None:
            raise RuntimeError("AngelOneClient is not logged in. Call login() first.")
