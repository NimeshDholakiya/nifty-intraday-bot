"""Core data structures for candles, options, signals, positions, and trades."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

OptionType = Literal["CE", "PE"]
SignalAction = Literal["BUY_CE", "BUY_PE", "EXIT", "HOLD"]
TrendDirection = Literal["BULLISH", "BEARISH"]
HTFTrend = Literal["BULLISH", "BEARISH", "NEUTRAL"]


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float
    complete: bool
    candle_count: int


@dataclass(frozen=True)
class IndicatorSnapshot:
    timestamp: datetime
    close: float
    vwap: float
    vwap_mode: str  # "VOLUME_WEIGHTED" or "TWAP_FALLBACK"
    atr: float
    supertrend: float
    direction: TrendDirection
    orb_high: float | None
    orb_low: float | None
    orb_complete: bool
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    htf_trend: HTFTrend = "NEUTRAL"
    volume_ratio: float | None = None
    orb_range_pct: float = 0.0
    ai_score_ce: int = 0
    ai_score_pe: int = 0
    ai_breakdown_ce: dict[str, int] = field(default_factory=dict)
    ai_breakdown_pe: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    token: str
    exchange: str  # "NFO"
    strike: int
    option_type: OptionType
    expiry: str
    lot_size: int
    ltp: float = 0.0


@dataclass(frozen=True)
class Signal:
    action: SignalAction
    timestamp: datetime
    spot_price: float
    reason: str
    snapshot: IndicatorSnapshot


@dataclass
class Position:
    contract: OptionContract
    lots: int
    quantity: int
    entry_time: datetime
    entry_price: float
    entry_spot: float
    stop_loss: float
    target: float
    highest_price: float
    breakeven_active: bool = False
    trailing_active: bool = False
    entry_reason: str = ""

    @property
    def invested_capital(self) -> float:
        return self.entry_price * self.quantity

    def unrealized_gross_pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.quantity


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    option_type: OptionType
    strike: int
    lots: int
    quantity: int
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    entry_spot: float
    exit_spot: float
    gross_pnl: float
    estimated_charges: float
    net_pnl: float
    exit_reason: str
    entry_reason: str = ""


@dataclass
class DailyStats:
    trade_date: str
    starting_capital: float
    trades: list[ClosedTrade] = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades)

    @property
    def total_charges(self) -> float:
        return sum(t.estimated_charges for t in self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.net_pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.net_pnl <= 0)
