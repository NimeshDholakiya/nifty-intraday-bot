"""Strict risk management, ₹5,000 strike sizing, trailing SL, and charge calculator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from config import BotConfig
from models import DailyStats, OptionContract, OptionType, Position


def _parse_hhmm(hhmm: str) -> time:
    h, m = hhmm.strip().split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class SelectionResult:
    contract: OptionContract | None
    lots: int
    reason: str


@dataclass(frozen=True)
class PositionUpdate:
    should_exit: bool
    exit_reason: str
    sl_updated: bool
    sl_update_reason: str


class RiskManager:
    """Enforces capital limits, max daily trades/loss, and dynamic stop-loss rules."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.square_off_time = _parse_hhmm(config.square_off_time)

    def can_take_new_trade(self, stats: DailyStats) -> tuple[bool, str]:
        if stats.halted:
            return False, f"Trading halted: {stats.halt_reason}"

        if stats.trade_count >= self.config.max_trades_per_day:
            stats.halted = True
            stats.halt_reason = (
                f"Max daily trades reached ({stats.trade_count}/{self.config.max_trades_per_day})"
            )
            return False, stats.halt_reason

        if stats.net_pnl <= -abs(self.config.max_daily_loss):
            stats.halted = True
            stats.halt_reason = (
                f"Daily max loss limit hit (Net P&L ₹{stats.net_pnl:.2f} "
                f"<= -₹{self.config.max_daily_loss:.2f})"
            )
            return False, stats.halt_reason

        usable_capital = self.available_buying_capital(stats)
        min_needed = self.config.min_option_premium * self.config.lot_size
        if usable_capital < min_needed:
            stats.halted = True
            stats.halt_reason = (
                f"Usable capital ₹{usable_capital:.2f} is below minimum 1-lot "
                f"requirement ₹{min_needed:.2f}"
            )
            return False, stats.halt_reason

        return True, "OK"

    def available_buying_capital(self, stats: DailyStats) -> float:
        """Compute current deployable cash (respecting intraday losses & charge buffer)."""
        effective_equity = min(
            self.config.capital,
            self.config.capital + stats.net_pnl,
        )
        return max(effective_equity - self.config.charge_buffer, 0.0)

    def remaining_daily_loss_budget(self, stats: DailyStats) -> float:
        return max(self.config.max_daily_loss + stats.net_pnl, 0.0)

    def estimate_round_trip_charges(
        self,
        entry_price: float,
        exit_price: float,
        quantity: int,
    ) -> float:
        """Estimate realistic NSE F&O option buying charges (Angel One flat ₹20/order).

        Includes:
          - Brokerage: ₹20 buy + ₹20 sell = ₹40
          - STT on options sell value: 0.10% (10 bps)
          - NSE exchange transaction charges: ~0.03503% on total turnover
          - SEBI turnover charges: 0.0001% on total turnover
          - Stamp duty on buy value: 0.003%
          - GST: 18% on (brokerage + exchange + SEBI)
        """
        buy_val = entry_price * quantity
        sell_val = exit_price * quantity
        turnover = buy_val + sell_val

        brokerage = self.config.brokerage_per_order * 2.0
        stt = sell_val * 0.001
        exchange_txn = turnover * 0.0003503
        sebi_fee = turnover * 0.000001
        stamp_duty = buy_val * 0.00003
        gst = 0.18 * (brokerage + exchange_txn + sebi_fee)

        return round(brokerage + stt + exchange_txn + sebi_fee + stamp_duty + gst, 2)

    def select_affordable_option(
        self,
        contracts: list[OptionContract],
        spot_price: float,
        option_type: OptionType,
        stats: DailyStats,
    ) -> SelectionResult:
        """Pick the closest-to-ATM liquid strike that fits within ₹5,000 capital.

        Safety rules:
          - Rejects deep OTM options (> max_strike_distance_pts from spot)
          - Rejects ultra-cheap lottery options (< min_option_premium)
          - Picks closest strike to ATM that fits in 1 lot
          - Scales to 2 lots ONLY if configured, affordable, AND initial SL risk fits
            within the remaining daily loss budget.
        """
        usable = self.available_buying_capital(stats)
        loss_budget = self.remaining_daily_loss_budget(stats)

        candidates: list[OptionContract] = []
        for c in contracts:
            if c.option_type != option_type:
                continue
            if c.ltp < self.config.min_option_premium:
                continue
            if abs(c.strike - spot_price) > self.config.max_strike_distance_pts:
                continue
            one_lot_cost = c.ltp * c.lot_size
            if one_lot_cost <= usable:
                candidates.append(c)

        if not candidates:
            max_ltp = usable / self.config.lot_size if self.config.lot_size else 0.0
            return SelectionResult(
                contract=None,
                lots=0,
                reason=(
                    f"No safe {option_type} strike within ±{self.config.max_strike_distance_pts} pts "
                    f"of spot {spot_price:.2f} fits usable budget ₹{usable:.2f} "
                    f"(affordable LTP range: ₹{self.config.min_option_premium:.2f}–₹{max_ltp:.2f}/unit)"
                ),
            )

        # Prefer closest strike to ATM (highest delta, best liquidity); break ties by higher LTP
        candidates.sort(key=lambda c: (abs(c.strike - spot_price), -c.ltp))
        chosen = candidates[0]

        lots = self.config.min_lots
        if self.config.max_lots >= 2:
            two_lot_cost = chosen.ltp * chosen.lot_size * 2
            two_lot_sl_risk = (
                chosen.ltp * self.config.stop_loss_pct * chosen.lot_size * 2
                + self.estimate_round_trip_charges(
                    chosen.ltp, chosen.ltp * (1.0 - self.config.stop_loss_pct), chosen.lot_size * 2
                )
            )
            if two_lot_cost <= usable and two_lot_sl_risk <= loss_budget:
                lots = 2

        return SelectionResult(
            contract=chosen,
            lots=lots,
            reason=(
                f"Selected {chosen.symbol} @ ₹{chosen.ltp:.2f} x {lots} lot(s) "
                f"({lots * chosen.lot_size} qty = ₹{chosen.ltp * lots * chosen.lot_size:.2f})"
            ),
        )

    def build_position(
        self,
        contract: OptionContract,
        lots: int,
        entry_time: datetime,
        entry_spot: float,
        entry_reason: str,
    ) -> Position:
        qty = lots * contract.lot_size
        sl = round(contract.ltp * (1.0 - self.config.stop_loss_pct), 2)
        target = round(contract.ltp * (1.0 + self.config.target_pct), 2)
        return Position(
            contract=contract,
            lots=lots,
            quantity=qty,
            entry_time=entry_time,
            entry_price=contract.ltp,
            entry_spot=entry_spot,
            stop_loss=sl,
            target=target,
            highest_price=contract.ltp,
            entry_reason=entry_reason,
        )

    def evaluate_position(
        self,
        position: Position,
        current_ltp: float,
        now_time: datetime,
        trend_exit_reason: str | None = None,
    ) -> PositionUpdate:
        """Update trailing/breakeven SL and check all exit conditions."""
        sl_updated = False
        sl_update_reason = ""

        # 1. Mandatory intraday square-off (never carry options overnight)
        if now_time.time() >= self.square_off_time:
            return PositionUpdate(
                should_exit=True,
                exit_reason=f"Mandatory intraday square-off ({self.config.square_off_time} IST)",
                sl_updated=False,
                sl_update_reason="",
            )

        # 2. Update peak price and check dynamic SL upgrades
        if current_ltp > position.highest_price:
            position.highest_price = current_ltp

        gain_pct = (position.highest_price - position.entry_price) / position.entry_price

        # Breakeven upgrade at +20% (includes small charge buffer per unit)
        if not position.breakeven_active and gain_pct >= self.config.breakeven_trigger_pct:
            charges_per_unit = (
                self.estimate_round_trip_charges(
                    position.entry_price, position.entry_price, position.quantity
                )
                / position.quantity
            )
            breakeven_sl = round(position.entry_price + charges_per_unit, 2)
            if breakeven_sl > position.stop_loss:
                position.stop_loss = breakeven_sl
                position.breakeven_active = True
                sl_updated = True
                sl_update_reason = (
                    f"SL moved to BREAKEVEN ₹{breakeven_sl:.2f} "
                    f"(peak gain +{gain_pct * 100:.1f}%)"
                )

        # Trailing SL upgrade at +35%
        if gain_pct >= self.config.trail_trigger_pct:
            trailed_sl = round(
                position.highest_price * (1.0 - self.config.trail_offset_pct), 2
            )
            if trailed_sl > position.stop_loss:
                position.stop_loss = trailed_sl
                position.trailing_active = True
                sl_updated = True
                sl_update_reason = (
                    f"Trailing SL raised to ₹{trailed_sl:.2f} "
                    f"(peak ₹{position.highest_price:.2f})"
                )

        # 3. Hard target hit
        if current_ltp >= position.target:
            return PositionUpdate(
                should_exit=True,
                exit_reason=f"Target Hit (LTP ₹{current_ltp:.2f} >= Target ₹{position.target:.2f})",
                sl_updated=sl_updated,
                sl_update_reason=sl_update_reason,
            )

        # 4. Stop-Loss / Trailing SL hit
        if current_ltp <= position.stop_loss:
            label = (
                "Trailing SL Hit"
                if position.trailing_active
                else "Breakeven SL Hit"
                if position.breakeven_active
                else "Stop-Loss Hit"
            )
            return PositionUpdate(
                should_exit=True,
                exit_reason=f"{label} (LTP ₹{current_ltp:.2f} <= SL ₹{position.stop_loss:.2f})",
                sl_updated=sl_updated,
                sl_update_reason=sl_update_reason,
            )

        # 5. Lemonn-style Bolt Scalp Time-Stop (Anti-Theta Decay Protection)
        if self.config.enable_bolt_time_stop:
            elapsed_min = (now_time - position.entry_time).total_seconds() / 60.0
            if elapsed_min >= self.config.max_hold_minutes:
                pnl_pct = (current_ltp - position.entry_price) / position.entry_price
                return PositionUpdate(
                    should_exit=True,
                    exit_reason=(
                        f"Bolt Scalp Time-Stop ({int(elapsed_min)}m elapsed, "
                        f"exited at {pnl_pct * 100:+.1f}% to prevent theta decay)"
                    ),
                    sl_updated=sl_updated,
                    sl_update_reason=sl_update_reason,
                )

        # 6. Trend reversal exit (from Supertrend flip)
        if trend_exit_reason:
            return PositionUpdate(
                should_exit=True,
                exit_reason=f"Trend Exit: {trend_exit_reason}",
                sl_updated=sl_updated,
                sl_update_reason=sl_update_reason,
            )

        return PositionUpdate(
            should_exit=False,
            exit_reason="",
            sl_updated=sl_updated,
            sl_update_reason=sl_update_reason,
        )
