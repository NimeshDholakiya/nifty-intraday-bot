"""Configuration and environment loader for the NIFTY 50 Intraday Signal Bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(env_path: Path | None = None) -> None:
    """Load key=value pairs from a local .env file into os.environ (stdlib only)."""
    path = env_path or (Path(__file__).resolve().parent / ".env")
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class BotConfig:
    # Capital & position sizing (NSE NIFTY 50 lot size is 65 from Jan 2026)
    capital: float = 5000.0
    lot_size: int = 65
    min_lots: int = 1
    max_lots: int = 2
    charge_buffer: float = 150.0  # Keep small buffer for brokerage/STT/slippage

    # Option selection guardrails
    min_option_premium: float = 20.0  # Avoid cheap far-OTM options that decay fast
    max_strike_distance_pts: int = 800  # Max points OTM from NIFTY spot (allows affordable strikes for ₹5k across 0-7 DTE)

    # Strict intraday risk rules
    max_trades_per_day: int = 1  # 1 high-probability trade per morning session
    max_daily_loss: float = 800.0  # 16% max daily loss on ₹5,000 capital
    stop_loss_pct: float = 0.18  # 18% initial stop-loss on option premium
    breakeven_trigger_pct: float = 0.12  # Fast breakeven: move SL to cost+charges at +12%
    trail_trigger_pct: float = 0.20  # Start trailing SL as soon as trade is up +20%
    trail_offset_pct: float = 0.10  # Trail 10% below peak option price (lock in gains)
    target_pct: float = 0.50  # Primary target at +50%

    # Strategy timing (Multi-window intraday scanning from 09:20 AM to 14:45 PM)
    orb_start: str = "09:15"
    orb_end: str = "09:30"
    first_entry_time: str = "09:20"  # Allows first 5-min momentum candle entries
    last_entry_time: str = "14:45"  # Allows trades across morning, midday & afternoon momentum windows
    square_off_time: str = "15:15"
    candle_interval: str = "FIVE_MINUTE"
    supertrend_period: int = 10
    supertrend_multiplier: float = 3.0
    max_breakout_extension_pct: float = 0.0035  # Max 0.35% extension (avoids chasing peaks)
    enable_rolling_breakouts: bool = True  # Allows rolling 30-min breakouts and VWAP continuation at ANY hour
    rolling_lookback_bars: int = 6  # 6 bars = 30-minute rolling consolidation window

    # High-accuracy confluence filters (filter out sideways/choppy traps)
    enable_adx_filter: bool = True
    adx_period: int = 14
    min_adx: float = 20.0  # Skip sideways choppy markets when ADX < 20

    enable_htf_filter: bool = True
    htf_minutes: int = 15  # Require 15-min trend alignment with 5-min entry

    enable_volume_filter: bool = True
    volume_lookback: int = 10
    min_volume_ratio: float = 1.25  # Breakout candle volume >= 1.25x recent avg

    enable_orb_range_filter: bool = True
    max_orb_range_pct: float = 0.0075  # Skip exhausted opens (>0.75% 15-min range)

    # Lemonn-style AI Bolt Scalp Engine (Multi-Factor Scoring & Anti-Theta Time-Stop)
    enable_ai_bolt_mode: bool = True
    min_ai_score: int = 80  # Enter only when Institutional AI Confidence Score >= 80/100
    enable_bolt_time_stop: bool = True
    max_hold_minutes: int = 35  # Max 35 mins in trade: exits flat if no momentum (saves from theta decay)

    # Execution & polling
    paper_trading: bool = True
    poll_interval_seconds: int = 15
    brokerage_per_order: float = 20.0  # Angel One flat ₹20/order

    # Broker selection ("groww" or "angel")
    broker: str = "groww"
    groww_api_token: str = ""  # Required only if using Groww's ₹499/mo Trading API

    # Angel One SmartAPI credentials (fallback free data feed)
    angel_api_key: str = ""
    angel_client_code: str = ""
    angel_pin: str = ""
    angel_totp_secret: str = ""

    # Optional Telegram notifications
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Paths
    base_dir: Path = Path(__file__).resolve().parent

    @property
    def logs_dir(self) -> Path:
        return self.base_dir / "logs"

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def max_premium_for_one_lot(self) -> float:
        usable = max(self.capital - self.charge_buffer, 0.0)
        return usable / self.lot_size

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> "BotConfig":
        load_dotenv(env_path)
        return cls(
            capital=_env_float("BOT_CAPITAL", 5000.0),
            lot_size=_env_int("NIFTY_LOT_SIZE", 65),
            min_lots=_env_int("MIN_LOTS", 1),
            max_lots=_env_int("MAX_LOTS", 2),
            charge_buffer=_env_float("CHARGE_BUFFER", 150.0),
            min_option_premium=_env_float("MIN_OPTION_PREMIUM", 25.0),
            max_strike_distance_pts=_env_int("MAX_STRIKE_DISTANCE_PTS", 800),
            max_trades_per_day=_env_int("MAX_TRADES_PER_DAY", 1),
            max_daily_loss=_env_float("MAX_DAILY_LOSS", 800.0),
            stop_loss_pct=_env_float("STOP_LOSS_PCT", 0.18),
            breakeven_trigger_pct=_env_float("BREAKEVEN_TRIGGER_PCT", 0.12),
            trail_trigger_pct=_env_float("TRAIL_TRIGGER_PCT", 0.20),
            trail_offset_pct=_env_float("TRAIL_OFFSET_PCT", 0.10),
            target_pct=_env_float("TARGET_PCT", 0.50),
            orb_start=_env_str("ORB_START", "09:15"),
            orb_end=_env_str("ORB_END", "09:30"),
            first_entry_time=_env_str("FIRST_ENTRY_TIME", "09:20"),
            last_entry_time=_env_str("LAST_ENTRY_TIME", "14:45"),
            square_off_time=_env_str("SQUARE_OFF_TIME", "15:15"),
            candle_interval=_env_str("CANDLE_INTERVAL", "FIVE_MINUTE"),
            supertrend_period=_env_int("SUPERTREND_PERIOD", 10),
            supertrend_multiplier=_env_float("SUPERTREND_MULTIPLIER", 3.0),
            max_breakout_extension_pct=_env_float("MAX_BREAKOUT_EXTENSION_PCT", 0.0035),
            enable_rolling_breakouts=_env_bool("ENABLE_ROLLING_BREAKOUTS", True),
            rolling_lookback_bars=_env_int("ROLLING_LOOKBACK_BARS", 6),
            enable_adx_filter=_env_bool("ENABLE_ADX_FILTER", True),
            adx_period=_env_int("ADX_PERIOD", 14),
            min_adx=_env_float("MIN_ADX", 20.0),
            enable_htf_filter=_env_bool("ENABLE_HTF_FILTER", True),
            htf_minutes=_env_int("HTF_MINUTES", 15),
            enable_volume_filter=_env_bool("ENABLE_VOLUME_FILTER", True),
            volume_lookback=_env_int("VOLUME_LOOKBACK", 10),
            min_volume_ratio=_env_float("MIN_VOLUME_RATIO", 1.25),
            enable_orb_range_filter=_env_bool("ENABLE_ORB_RANGE_FILTER", True),
            max_orb_range_pct=_env_float("MAX_ORB_RANGE_PCT", 0.0075),
            enable_ai_bolt_mode=_env_bool("ENABLE_AI_BOLT_MODE", True),
            min_ai_score=_env_int("MIN_AI_SCORE", 80),
            enable_bolt_time_stop=_env_bool("ENABLE_BOLT_TIME_STOP", True),
            max_hold_minutes=_env_int("MAX_HOLD_MINUTES", 35),
            poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 15),
            brokerage_per_order=_env_float("BROKERAGE_PER_ORDER", 20.0),
            broker=_env_str("BROKER", "groww"),
            groww_api_token=_env_str("GROWW_API_TOKEN", ""),
            angel_api_key=_env_str("ANGEL_API_KEY", ""),
            angel_client_code=_env_str("ANGEL_CLIENT_CODE", ""),
            angel_pin=_env_str("ANGEL_PIN", ""),
            angel_totp_secret=_env_str("ANGEL_TOTP_SECRET", ""),
            telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=_env_str("TELEGRAM_CHAT_ID", ""),
        )
