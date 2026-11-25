"""Offline intraday simulator, realistic option pricer, and multi-day backtester."""

from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from config import BotConfig
from models import Candle, ClosedTrade, DailyStats, OptionContract, OptionType
from notifier import Notifier
from paper_broker import PaperBroker
from risk_manager import RiskManager
from strategy import ORBVWAPSupertrendStrategy


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def estimate_option_premium(
    spot: float,
    strike: int,
    option_type: OptionType,
    minutes_to_close: float,
    iv_annual: float = 0.168,
    days_to_expiry: float = 2.0,
) -> float:
    """Black-Scholes option pricer (stdlib math.erf) for realistic offline simulation.

    Models true delta, gamma, and intraday theta decay on NIFTY 50 weekly expiries.
    """
    # Convert remaining trading days + intraday fraction to years (252 trading days/yr)
    if days_to_expiry <= 1.0:
        t_days = max(minutes_to_close / 375.0, 0.04)
    else:
        t_days = max((days_to_expiry - 1.0) + (minutes_to_close / 375.0), 0.04)
    t_years = t_days / 252.0
    sigma = max(iv_annual, 0.05)
    sqrt_t = math.sqrt(t_years)

    if spot <= 0 or strike <= 0:
        return 1.0

    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t

    if option_type == "CE":
        price = spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    else:
        price = strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)

    return round(max(price, 1.0), 2)


def build_simulated_option_chain(
    spot: float,
    option_type: OptionType,
    lot_size: int,
    bar_time: datetime,
    days_to_expiry: float = 2.0,
) -> list[OptionContract]:
    """Generate a NIFTY 50 option chain around spot in 50-point strike increments."""
    atm = int(round(spot / 50.0) * 50)
    close_dt = bar_time.replace(hour=15, minute=30, second=0)
    mins_left = max((close_dt - bar_time).total_seconds() / 60.0, 5.0)
    expiry_str = (bar_time.date() + timedelta(days=int(math.ceil(days_to_expiry)))).isoformat()

    contracts: list[OptionContract] = []
    for offset in range(-250, 300, 50):
        strike = atm + offset
        ltp = estimate_option_premium(
            spot=spot,
            strike=strike,
            option_type=option_type,
            minutes_to_close=mins_left,
            days_to_expiry=days_to_expiry,
        )
        contracts.append(
            OptionContract(
                symbol=f"NIFTY{expiry_str.replace('-', '')}{strike}{option_type}",
                token=f"SIM-{strike}-{option_type}",
                exchange="NFO",
                strike=strike,
                option_type=option_type,
                expiry=expiry_str,
                lot_size=lot_size,
                ltp=ltp,
            )
        )
    return contracts


def generate_intraday_session(
    session_date: date,
    regime: str = "bullish_breakout",
    start_price: float = 24500.0,
    seed: int = 42,
) -> list[Candle]:
    """Generate 75 five-minute candles (09:15 to 15:25 IST) for realistic testing.

    Regimes:
      - "bullish_breakout": Tight opening range followed by sustained upside trend
      - "bearish_breakdown": Tight opening range followed by downside momentum
      - "choppy": Range-bound whipsaw session (tests stop-loss & daily loss rules)
      - "random": Randomly selects a realistic mix of trending and choppy sessions
    """
    rng = random.Random(seed)
    if regime == "random":
        regime = rng.choices(
            ["bullish_breakout", "bearish_breakdown", "choppy"],
            weights=[0.35, 0.35, 0.30],
            k=1,
        )[0]

    candles: list[Candle] = []
    price = start_price
    current_dt = datetime(session_date.year, session_date.month, session_date.day, 9, 15)

    for i in range(75):  # 09:15 .. 15:25 (5-minute bars)
        if i < 3:
            # 09:15-09:30 opening range: moderate two-way movement
            drift = rng.uniform(-6.0, 6.0)
            vol_scale = 14.0
        elif regime == "bullish_breakout":
            drift = 7.5 if i < 28 else rng.uniform(-2.0, 3.0)
            vol_scale = 13.0
        elif regime == "bearish_breakdown":
            drift = -7.5 if i < 28 else rng.uniform(-3.0, 2.0)
            vol_scale = 13.0
        else:
            # Choppy mean-reverting whipsaw
            pullback = (start_price - price) * 0.18
            drift = pullback + rng.uniform(-16.0, 16.0)
            vol_scale = 18.0

        open_p = price
        close_p = round(open_p + drift + rng.gauss(0.0, vol_scale * 0.45), 2)
        wick_up = abs(rng.gauss(0.0, vol_scale * 0.5))
        wick_dn = abs(rng.gauss(0.0, vol_scale * 0.5))
        high_p = round(max(open_p, close_p) + wick_up, 2)
        low_p = round(min(open_p, close_p) - wick_dn, 2)
        if regime in ("bullish_breakout", "bearish_breakdown") and 3 <= i <= 14:
            # Realistic breakout volume expansion during trend initiation
            volume = round(rng.uniform(110000, 190000), 0)
        elif regime == "choppy":
            volume = round(rng.uniform(40000, 85000), 0)
        else:
            volume = round(rng.uniform(55000, 105000), 0)

        candles.append(
            Candle(
                timestamp=current_dt,
                open=open_p,
                high=high_p,
                low=low_p,
                close=close_p,
                volume=volume,
            )
        )
        price = close_p
        current_dt += timedelta(minutes=5)

    return candles


def load_candles_from_csv(csv_path: Path) -> list[Candle]:
    """Load 5-minute OHLCV candles from a CSV file (columns: timestamp,open,high,low,close,volume)."""
    candles: list[Candle] = []
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_raw = row["timestamp"].strip()
            dt = datetime.fromisoformat(ts_raw).replace(tzinfo=None)
            candles.append(
                Candle(
                    timestamp=dt,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0.0),
                )
            )
    candles.sort(key=lambda c: c.timestamp)
    return candles


def replay_session(
    candles: list[Candle],
    config: BotConfig,
    notifier: Notifier,
    write_journal: bool = True,
    days_to_expiry: float = 2.0,
    warmup_candles: list[Candle] | None = None,
) -> DailyStats:
    """Replay one intraday session candle-by-candle through Strategy + Risk + PaperBroker."""
    trade_date = candles[-1].timestamp.date().isoformat()
    strategy = ORBVWAPSupertrendStrategy(config)
    risk = RiskManager(config)
    broker = PaperBroker(config, risk, trade_date=trade_date, write_journal=write_journal)

    history: list[Candle] = list(warmup_candles[-150:]) if warmup_candles else []
    for bar in candles:
        history.append(bar)
        signal = strategy.evaluate(history, open_position=broker.position)
        if signal is None:
            continue

        # Manage active position first
        if broker.position is not None:
            pos = broker.position
            close_dt = bar.timestamp.replace(hour=15, minute=30, second=0)
            mins_left = max((close_dt - bar.timestamp).total_seconds() / 60.0, 5.0)
            current_ltp = estimate_option_premium(
                spot=bar.close,
                strike=pos.contract.strike,
                option_type=pos.contract.option_type,
                minutes_to_close=mins_left,
                days_to_expiry=days_to_expiry,
            )
            trend_exit = signal.reason if signal.action == "EXIT" else None
            update = risk.evaluate_position(
                position=pos,
                current_ltp=current_ltp,
                now_time=bar.timestamp,
                trend_exit_reason=trend_exit,
            )
            if update.sl_updated:
                notifier.notify_sl_update(pos, update.sl_update_reason, bar.timestamp)
            if update.should_exit:
                closed = broker.close_position(
                    exit_price=current_ltp,
                    exit_time=bar.timestamp,
                    exit_spot=bar.close,
                    exit_reason=update.exit_reason,
                )
                if "Stop-Loss" in update.exit_reason:
                    strategy.register_stop_out(closed.option_type, bar.timestamp)
                notifier.notify_exit(closed, broker.stats)
            continue

        # Flat: check entry signals
        if signal.action in ("BUY_CE", "BUY_PE"):
            allowed, deny_reason = risk.can_take_new_trade(broker.stats)
            if not allowed:
                notifier.info(f"Signal {signal.action} blocked: {deny_reason}", bar.timestamp)
                continue

            option_type: OptionType = "CE" if signal.action == "BUY_CE" else "PE"
            chain = build_simulated_option_chain(
                spot=bar.close,
                option_type=option_type,
                lot_size=config.lot_size,
                bar_time=bar.timestamp,
                days_to_expiry=days_to_expiry,
            )
            selection = risk.select_affordable_option(
                contracts=chain,
                spot_price=bar.close,
                option_type=option_type,
                stats=broker.stats,
            )
            if selection.contract is None:
                notifier.info(f"Signal {signal.action} skipped: {selection.reason}", bar.timestamp)
                continue

            pos = broker.open_position(
                contract=selection.contract,
                lots=selection.lots,
                entry_time=bar.timestamp,
                entry_spot=bar.close,
                entry_reason=signal.reason,
            )
            notifier.notify_entry(pos)

    notifier.notify_daily_summary(broker.stats)
    return broker.stats


@dataclass
class BacktestReport:
    days_tested: int
    total_trades: int
    wins: int
    losses: int
    gross_pnl: float
    total_charges: float
    net_pnl: float
    max_drawdown: float
    best_day: float
    worst_day: float
    all_trades: list[ClosedTrade]


def run_backtest(
    config: BotConfig,
    days: int = 30,
    csv_path: Path | None = None,
    seed: int = 101,
) -> BacktestReport:
    """Run a multi-day backtest on historical CSV candles or realistic simulated regimes."""
    silent_notifier = Notifier(config, silent=True)
    sessions: list[list[Candle]] = []

    if csv_path is not None:
        all_candles = load_candles_from_csv(csv_path)
        by_day: dict[date, list[Candle]] = {}
        for c in all_candles:
            by_day.setdefault(c.timestamp.date(), []).append(c)
        for d in sorted(by_day.keys()):
            sessions.append(by_day[d])
    else:
        base_day = date.today() - timedelta(days=days)
        spot = 24500.0
        for idx in range(days):
            d = base_day + timedelta(days=idx)
            # Cycle days-to-expiry between 1 and 4 days (weekly NIFTY expiry realism)
            dte = float(((idx) % 4) + 1)
            session = generate_intraday_session(
                session_date=d,
                regime="random",
                start_price=spot,
                seed=seed + idx * 17,
            )
            sessions.append(session)
            spot = session[-1].close

    all_trades: list[ClosedTrade] = []
    daily_pnls: list[float] = []
    equity = config.capital
    peak_equity = equity
    max_drawdown = 0.0

    warmup: list[Candle] = []
    for idx, session in enumerate(sessions):
        dte = float((idx % 4) + 1)
        stats = replay_session(
            candles=session,
            config=config,
            notifier=silent_notifier,
            write_journal=False,
            days_to_expiry=dte,
            warmup_candles=warmup,
        )
        warmup = (warmup + session)[-150:]
        all_trades.extend(stats.trades)
        daily_pnls.append(stats.net_pnl)
        equity += stats.net_pnl
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, peak_equity - equity)

    wins = sum(1 for t in all_trades if t.net_pnl > 0)
    losses = sum(1 for t in all_trades if t.net_pnl <= 0)
    gross = round(sum(t.gross_pnl for t in all_trades), 2)
    charges = round(sum(t.estimated_charges for t in all_trades), 2)
    net = round(sum(t.net_pnl for t in all_trades), 2)

    return BacktestReport(
        days_tested=len(sessions),
        total_trades=len(all_trades),
        wins=wins,
        losses=losses,
        gross_pnl=gross,
        total_charges=charges,
        net_pnl=net,
        max_drawdown=round(max_drawdown, 2),
        best_day=round(max(daily_pnls) if daily_pnls else 0.0, 2),
        worst_day=round(min(daily_pnls) if daily_pnls else 0.0, 2),
        all_trades=all_trades,
    )
