"""ORB (9:15-9:30) + VWAP + Supertrend + ADX + 15m HTF + Volume intraday strategy."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from config import BotConfig
from indicators import build_latest_snapshot
from models import Candle, OptionType, Position, Signal


def _parse_hhmm(hhmm: str) -> time:
    h, m = hhmm.strip().split(":")
    return time(int(h), int(m))


class ORBVWAPSupertrendStrategy:
    """Generates BUY_CE, BUY_PE, EXIT, or HOLD signals on closed 5-min candles.

    Core entry rules:
      - Opening Range (09:15-09:30) must be complete
      - Current candle time must be in [09:30, last_entry_time]
      - BUY_CE: Close > ORH, Close > VWAP, 5m Supertrend == BULLISH, not over-extended
      - BUY_PE: Close < ORL, Close < VWAP, 5m Supertrend == BEARISH, not over-extended

    High-accuracy confluence filters (each individually configurable in .env):
      - ORB Range Filter: skips exhausted opens where 9:15-9:30 range > max_orb_range_pct
      - ADX Filter: skips sideways choppy markets when ADX < min_adx (plus DI alignment)
      - 15m HTF Filter: requires 15-min EMA + Supertrend trend alignment
      - Volume Filter: requires breakout volume >= min_volume_ratio x recent average

    Exit signal (trend reversal):
      - If holding CE and Supertrend flips BEARISH -> EXIT
      - If holding PE and Supertrend flips BULLISH -> EXIT
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.first_entry_time = _parse_hhmm(getattr(config, "first_entry_time", "09:20"))
        self.orb_end_time = _parse_hhmm(config.orb_end)
        self.last_entry_time = _parse_hhmm(config.last_entry_time)
        self._last_signal_bar: datetime | None = None
        self._last_stopped_direction: OptionType | None = None
        self._cooldown_until_bar: datetime | None = None

    def register_stop_out(self, option_type: OptionType, bar_time: datetime) -> None:
        """Prevent immediately re-entering the same direction on the same candle."""
        self._last_stopped_direction = option_type
        self._cooldown_until_bar = bar_time

    def reset_day(self) -> None:
        self._last_signal_bar = None
        self._last_stopped_direction = None
        self._cooldown_until_bar = None

    def evaluate(
        self,
        candles: list[Candle],
        open_position: Position | None = None,
        expected_trade_date: date | None = None,
    ) -> Signal | None:
        if len(candles) < 4:
            return None

        if expected_trade_date is not None and candles[-1].timestamp.date() != expected_trade_date:
            # Avoid evaluating yesterday's candles before today's first 5-min bar closes at 09:20
            return None

        snapshot = build_latest_snapshot(
            candles,
            orb_start=self.config.orb_start,
            orb_end=self.config.orb_end,
            supertrend_period=self.config.supertrend_period,
            supertrend_multiplier=self.config.supertrend_multiplier,
            adx_period=self.config.adx_period,
            htf_minutes=self.config.htf_minutes,
            volume_lookback=self.config.volume_lookback,
        )
        if snapshot is None:
            return None

        bar_time = snapshot.timestamp
        t = bar_time.time()

        # 1. Check trend-reversal exit if we currently hold a position
        if open_position is not None:
            opt_type = open_position.contract.option_type
            if opt_type == "CE" and snapshot.direction == "BEARISH":
                return Signal(
                    action="EXIT",
                    timestamp=bar_time,
                    spot_price=snapshot.close,
                    reason=(
                        f"Supertrend flipped BEARISH ({snapshot.supertrend:.2f}) "
                        f"while holding CE"
                    ),
                    snapshot=snapshot,
                )
            if opt_type == "PE" and snapshot.direction == "BULLISH":
                return Signal(
                    action="EXIT",
                    timestamp=bar_time,
                    spot_price=snapshot.close,
                    reason=(
                        f"Supertrend flipped BULLISH ({snapshot.supertrend:.2f}) "
                        f"while holding PE"
                    ),
                    snapshot=snapshot,
                )
            return Signal(
                action="HOLD",
                timestamp=bar_time,
                spot_price=snapshot.close,
                reason="Position active; trend intact",
                snapshot=snapshot,
            )

        # 2. Entry timing check (Multi-window intraday scanning)
        first_t = self.first_entry_time
        last_t = self.last_entry_time
        if t < first_t or t > last_t:
            return Signal(
                action="HOLD",
                timestamp=bar_time,
                spot_price=snapshot.close,
                reason=f"Outside trading entry window ({getattr(self.config, 'first_entry_time', '09:20')}-{self.config.last_entry_time})",
                snapshot=snapshot,
            )

        if self._last_signal_bar == bar_time:
            return Signal(
                action="HOLD",
                timestamp=bar_time,
                spot_price=snapshot.close,
                reason="Already evaluated this candle",
                snapshot=snapshot,
            )

        # Dynamic rolling consolidation lookback for anytime trading (e.g. 11:00 AM, 13:00 PM)
        lookback = getattr(self.config, "rolling_lookback_bars", 6)
        recent_bars = candles[-lookback - 1 : -1] if len(candles) > lookback else candles[:-1]
        rolling_high = max(c.high for c in recent_bars) if recent_bars else snapshot.close
        rolling_low = min(c.low for c in recent_bars) if recent_bars else snapshot.close

        # Filter A: Opening Range Exhaustion Filter (active 09:30-10:00 AM)
        if (
            snapshot.orb_complete
            and self.config.enable_orb_range_filter
            and snapshot.orb_range_pct > self.config.max_orb_range_pct
            and t < _parse_hhmm("10:00")
        ):
            return Signal(
                action="HOLD",
                timestamp=bar_time,
                spot_price=snapshot.close,
                reason=(
                    f"Filtered out: Opening range too wide ({snapshot.orb_range_pct * 100:.2f}% "
                    f"> {self.config.max_orb_range_pct * 100:.2f}%). Early exhaustion chasing blocked until 10:00 AM base forms."
                ),
                snapshot=snapshot,
            )

        # Bullish Setups Evaluation (CE)
        # Setup 1: Classic ORB Breakout (09:20 - 10:30 AM)
        orb_ce_breakout = (
            (snapshot.orb_range_pct <= self.config.max_orb_range_pct if self.config.enable_orb_range_filter else True)
            and snapshot.orb_high is not None
            and snapshot.close > snapshot.orb_high
            and ((snapshot.close - snapshot.orb_high) / snapshot.orb_high) <= self.config.max_breakout_extension_pct
        )

        # Setup 2: Rolling 30-min Consolidation Breakout (Anytime 09:20 - 14:45)
        rolling_ce_breakout = (
            getattr(self.config, "enable_rolling_breakouts", True)
            and len(recent_bars) >= lookback
            and snapshot.close > rolling_high
            and ((snapshot.close - rolling_high) / rolling_high) <= self.config.max_breakout_extension_pct
        )

        # Setup 3: Institutional VWAP Pullback Bounce
        vwap_tol = snapshot.vwap * 0.0020
        vwap_ce_bounce = (
            getattr(self.config, "enable_rolling_breakouts", True)
            and snapshot.direction == "BULLISH"
            and snapshot.close > snapshot.vwap
            and len(recent_bars) >= 2
            and any(abs(c.low - snapshot.vwap) <= vwap_tol for c in recent_bars[-3:])
            and snapshot.close > candles[-2].close
        )

        bullish_active = (
            (orb_ce_breakout or rolling_ce_breakout or vwap_ce_bounce)
            and snapshot.close > snapshot.vwap
            and snapshot.direction == "BULLISH"
        )

        if bullish_active:
            if self._last_stopped_direction == "CE" and self._cooldown_until_bar == bar_time:
                return Signal(
                    action="HOLD",
                    timestamp=bar_time,
                    spot_price=snapshot.close,
                    reason="Cooldown active on CE after stop-out",
                    snapshot=snapshot,
                )

            filter_block = self._check_confluence_filters(snapshot, "CE")
            if filter_block:
                return Signal(
                    action="HOLD",
                    timestamp=bar_time,
                    spot_price=snapshot.close,
                    reason=filter_block,
                    snapshot=snapshot,
                )

            self._last_signal_bar = bar_time
            vol_txt = f"{snapshot.volume_ratio:.2f}x" if snapshot.volume_ratio is not None else "N/A"
            setup_name = (
                f"Rolling 30m Breakout ({snapshot.close:.2f} > {rolling_high:.2f})"
                if rolling_ce_breakout
                else f"ORB High Breakout ({snapshot.close:.2f} > {snapshot.orb_high:.2f})"
                if orb_ce_breakout
                else "VWAP Pullback Bounce"
            )
            return Signal(
                action="BUY_CE",
                timestamp=bar_time,
                spot_price=snapshot.close,
                reason=(
                    f"AI BOLT TRADE [Confidence {snapshot.ai_score_ce}%] -> "
                    f"{setup_name} + Above VWAP ({snapshot.vwap:.2f}) "
                    f"+ 5m/15m BULLISH + ADX {snapshot.adx:.1f} + Vol {vol_txt}"
                ),
                snapshot=snapshot,
            )

        # Bearish Setups Evaluation (PE)
        # Setup 1: Classic ORB Breakdown
        orb_pe_breakdown = (
            (snapshot.orb_range_pct <= self.config.max_orb_range_pct if self.config.enable_orb_range_filter else True)
            and snapshot.orb_low is not None
            and snapshot.close < snapshot.orb_low
            and ((snapshot.orb_low - snapshot.close) / snapshot.orb_low) <= self.config.max_breakout_extension_pct
        )

        # Setup 2: Rolling 30-min Consolidation Breakdown
        rolling_pe_breakdown = (
            getattr(self.config, "enable_rolling_breakouts", True)
            and len(recent_bars) >= lookback
            and snapshot.close < rolling_low
            and ((rolling_low - snapshot.close) / rolling_low) <= self.config.max_breakout_extension_pct
        )

        # Setup 3: Institutional VWAP Rejection Breakdown
        vwap_pe_reject = (
            getattr(self.config, "enable_rolling_breakouts", True)
            and snapshot.direction == "BEARISH"
            and snapshot.close < snapshot.vwap
            and len(recent_bars) >= 2
            and any(abs(c.high - snapshot.vwap) <= vwap_tol for c in recent_bars[-3:])
            and snapshot.close < candles[-2].close
        )

        bearish_active = (
            (orb_pe_breakdown or rolling_pe_breakdown or vwap_pe_reject)
            and snapshot.close < snapshot.vwap
            and snapshot.direction == "BEARISH"
        )

        if bearish_active:
            if self._last_stopped_direction == "PE" and self._cooldown_until_bar == bar_time:
                return Signal(
                    action="HOLD",
                    timestamp=bar_time,
                    spot_price=snapshot.close,
                    reason="Cooldown active on PE after stop-out",
                    snapshot=snapshot,
                )

            filter_block = self._check_confluence_filters(snapshot, "PE")
            if filter_block:
                return Signal(
                    action="HOLD",
                    timestamp=bar_time,
                    spot_price=snapshot.close,
                    reason=filter_block,
                    snapshot=snapshot,
                )

            self._last_signal_bar = bar_time
            vol_txt = f"{snapshot.volume_ratio:.2f}x" if snapshot.volume_ratio is not None else "N/A"
            setup_name = (
                f"Rolling 30m Breakdown ({snapshot.close:.2f} < {rolling_low:.2f})"
                if rolling_pe_breakdown
                else f"ORB Low Breakdown ({snapshot.close:.2f} < {snapshot.orb_low:.2f})"
                if orb_pe_breakdown
                else "VWAP Rejection Breakdown"
            )
            return Signal(
                action="BUY_PE",
                timestamp=bar_time,
                spot_price=snapshot.close,
                reason=(
                    f"AI BOLT TRADE [Confidence {snapshot.ai_score_pe}%] -> "
                    f"{setup_name} + Below VWAP ({snapshot.vwap:.2f}) "
                    f"+ 5m/15m BEARISH + ADX {snapshot.adx:.1f} + Vol {vol_txt}"
                ),
                snapshot=snapshot,
            )

        return Signal(
            action="HOLD",
            timestamp=bar_time,
            spot_price=snapshot.close,
            reason=(
                f"Consolidation / No Breakout (Close={snapshot.close:.2f}, VWAP={snapshot.vwap:.2f}, "
                f"RollingH={rolling_high:.2f}, RollingL={rolling_low:.2f}, "
                f"ST={snapshot.direction}, 15m={snapshot.htf_trend}, ADX={snapshot.adx:.1f})"
            ),
            snapshot=snapshot,
        )

    def _check_confluence_filters(self, snapshot, direction: OptionType) -> str | None:
        # Lemonn-style AI Confluence Scoring Engine (Bolt Trade Check)
        if self.config.enable_ai_bolt_mode:
            ai_score = snapshot.ai_score_ce if direction == "CE" else snapshot.ai_score_pe
            if ai_score < self.config.min_ai_score:
                bd = snapshot.ai_breakdown_ce if direction == "CE" else snapshot.ai_breakdown_pe
                return (
                    f"AI Confidence Score {ai_score}/100 (< {self.config.min_ai_score} threshold) - "
                    f"Sub-optimal setup skipped (Score: {bd})"
                )

        # Filter B: ADX sideways-market & directional dominance filter
        if self.config.enable_adx_filter:
            if snapshot.adx < self.config.min_adx:
                return (
                    f"Filtered {direction}: Sideways market (ADX {snapshot.adx:.1f} "
                    f"< {self.config.min_adx:.1f})"
                )
            if direction == "CE" and snapshot.plus_di <= snapshot.minus_di:
                return (
                    f"Filtered CE: Weak bullish DI (+DI {snapshot.plus_di:.1f} "
                    f"<= -DI {snapshot.minus_di:.1f})"
                )
            if direction == "PE" and snapshot.minus_di <= snapshot.plus_di:
                return (
                    f"Filtered PE: Weak bearish DI (-DI {snapshot.minus_di:.1f} "
                    f"<= +DI {snapshot.plus_di:.1f})"
                )

        # Filter C: 15-minute Higher-Timeframe trend alignment
        if self.config.enable_htf_filter:
            expected = "BULLISH" if direction == "CE" else "BEARISH"
            if snapshot.htf_trend != expected:
                return (
                    f"Filtered {direction}: {self.config.htf_minutes}m HTF trend is "
                    f"{snapshot.htf_trend} (need {expected})"
                )

        # Filter D: Volume spike confirmation (when volume data is available)
        if self.config.enable_volume_filter and snapshot.volume_ratio is not None:
            if snapshot.volume_ratio < self.config.min_volume_ratio:
                return (
                    f"Filtered {direction}: Weak volume ({snapshot.volume_ratio:.2f}x "
                    f"< {self.config.min_volume_ratio:.2f}x avg)"
                )

        return None

    def analyze_trade_window(self, candles: list[Candle]) -> dict[str, Any]:
        """Analyze current market window for near-trigger breakout levels & safety diagnostics."""
        if len(candles) < 4:
            return {"status": "INSUFFICIENT_DATA", "message": "Gathering initial candle data..."}

        snapshot = build_latest_snapshot(
            candles,
            orb_start=self.config.orb_start,
            orb_end=self.config.orb_end,
            supertrend_period=self.config.supertrend_period,
            supertrend_multiplier=self.config.supertrend_multiplier,
            adx_period=self.config.adx_period,
            htf_minutes=self.config.htf_minutes,
            volume_lookback=self.config.volume_lookback,
        )
        if not snapshot:
            return {"status": "NO_SNAPSHOT", "message": "No snapshot available"}

        lookback = getattr(self.config, "rolling_lookback_bars", 6)
        recent_bars = candles[-lookback - 1 : -1] if len(candles) > lookback else candles[:-1]
        rolling_high = max(c.high for c in recent_bars) if recent_bars else snapshot.close
        rolling_low = min(c.low for c in recent_bars) if recent_bars else snapshot.close

        ce_level = max(rolling_high, snapshot.orb_high) if snapshot.orb_high else rolling_high
        pe_level = min(rolling_low, snapshot.orb_low) if snapshot.orb_low else rolling_low

        is_choppy = snapshot.adx < self.config.min_adx
        t_start = snapshot.timestamp.strftime("%H:%M")
        t_end = (snapshot.timestamp + timedelta(minutes=10)).strftime("%H:%M")

        status_txt = "CHOPPY_SIDEWAYS" if is_choppy else f"TRENDING_{snapshot.direction}"

        return {
            "status": status_txt,
            "window": f"{t_start} - {t_end} IST",
            "spot": round(snapshot.close, 2),
            "vwap": round(snapshot.vwap, 2),
            "supertrend": snapshot.direction,
            "adx": round(snapshot.adx, 1),
            "is_choppy": is_choppy,
            "ce_trigger": round(ce_level, 2),
            "pe_trigger": round(pe_level, 2),
            "score_ce": snapshot.ai_score_ce,
            "score_pe": snapshot.ai_score_pe,
        }
