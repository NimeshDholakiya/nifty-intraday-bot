"""Unit tests for indicators, ORB+VWAP+Supertrend strategy, risk manager, and broker helpers."""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from angel_client import AngelOneClient, _is_auth_error, _parse_angel_expiry, _parse_angel_strike
from config import BotConfig
from indicators import (
    compute_adx,
    compute_ai_confluence_score,
    compute_htf_trend,
    compute_opening_range,
    compute_supertrend,
    compute_volume_ratio,
    compute_vwap,
    resample_candles,
)
from main import _filter_closed_candles, _latest_expected_closed_bar
from models import Candle, DailyStats, OptionContract
from paper_broker import PaperBroker
from risk_manager import RiskManager
from simulator import generate_intraday_session, run_backtest
from strategy import ORBVWAPSupertrendStrategy


def _make_candles(prices: list[tuple[float, float, float, float, float]], start_hour: int = 9, start_min: int = 15) -> list[Candle]:
    dt = datetime(2026, 11, 18, start_hour, start_min)
    out: list[Candle] = []
    for o, h, l, c, v in prices:
        out.append(Candle(timestamp=dt, open=o, high=h, low=l, close=c, volume=v))
        dt += timedelta(minutes=5)
    return out


class TestIndicators(unittest.TestCase):
    def test_opening_range_marks_complete_after_0930(self) -> None:
        candles = _make_candles(
            [
                (24500, 24520, 24490, 24510, 1000),  # 09:15
                (24510, 24535, 24505, 24525, 1200),  # 09:20
                (24525, 24530, 24485, 24500, 1100),  # 09:25
            ]
        )
        orb_incomplete = compute_opening_range(candles)
        self.assertFalse(orb_incomplete.complete)
        self.assertEqual(orb_incomplete.high, 24535)
        self.assertEqual(orb_incomplete.low, 24485)

        candles.append(
            Candle(datetime(2026, 11, 18, 9, 30), 24500, 24550, 24500, 24545, 1500)
        )
        orb_complete = compute_opening_range(candles)
        self.assertTrue(orb_complete.complete)
        self.assertEqual(orb_complete.high, 24535)
        self.assertEqual(orb_complete.low, 24485)

    def test_vwap_volume_weighted_and_zero_volume_fallback(self) -> None:
        candles_with_vol = _make_candles(
            [
                (100, 110, 90, 100, 100),  # tp=100, vol=100
                (110, 130, 110, 120, 300),  # tp=120, vol=300 -> vwap=(10000+36000)/400=115
            ]
        )
        vwap, mode = compute_vwap(candles_with_vol)
        self.assertEqual(mode, "VOLUME_WEIGHTED")
        self.assertAlmostEqual(vwap[0], 100.0)
        self.assertAlmostEqual(vwap[1], 115.0)

        candles_no_vol = _make_candles(
            [
                (100, 110, 90, 100, 0),
                (110, 130, 110, 120, 0),
            ]
        )
        vwap_fb, mode_fb = compute_vwap(candles_no_vol)
        self.assertEqual(mode_fb, "TWAP_FALLBACK")
        self.assertAlmostEqual(vwap_fb[1], 110.0)

    def test_supertrend_detects_uptrend_and_reversal(self) -> None:
        # Steadily rising candles -> BULLISH
        rising = [(24000 + i * 25, 24015 + i * 25, 23990 + i * 25, 24010 + i * 25, 1000) for i in range(15)]
        st, dirs, _ = compute_supertrend(_make_candles(rising), period=5, multiplier=2.0)
        self.assertEqual(dirs[-1], "BULLISH")
        self.assertLess(st[-1], rising[-1][3])

        # Sharp drop reverses to BEARISH
        falling = rising + [
            (24350, 24355, 24180, 24190, 2000),
            (24190, 24200, 24050, 24060, 2000),
        ]
        st2, dirs2, _ = compute_supertrend(_make_candles(falling), period=5, multiplier=2.0)
        self.assertEqual(dirs2[-1], "BEARISH")
        self.assertGreater(st2[-1], falling[-1][3])

    def test_adx_htf_resample_and_volume_ratio(self) -> None:
        # Steadily trending up candles should produce high ADX, +DI > -DI, and BULLISH 15m HTF
        trending = [
            (24400 + i * 18, 24415 + i * 18, 24395 + i * 18, 24412 + i * 18, 50000)
            for i in range(12)
        ]
        trending.append((24616, 24645, 24610, 24640, 120000))  # Strong volume breakout bar
        candles = _make_candles(trending)

        adx, pdi, mdi = compute_adx(candles, period=6)
        self.assertGreater(adx[-1], 25.0)
        self.assertGreater(pdi[-1], mdi[-1])

        htf = resample_candles(candles, minutes=15)
        self.assertEqual(len(htf), 5)  # 13 five-min bars = 4 full 15m + 1 partial 15m
        self.assertEqual(compute_htf_trend(candles, htf_minutes=15), "BULLISH")

        vol_ratio = compute_volume_ratio(candles, lookback=10)
        self.assertIsNotNone(vol_ratio)
        assert vol_ratio is not None
        self.assertGreater(vol_ratio, 2.0)


class TestStrategyAndRisk(unittest.TestCase):
    def test_strategy_generates_buy_ce_on_bullish_confluence(self) -> None:
        cfg = BotConfig(supertrend_period=3, supertrend_multiplier=1.5)
        strategy = ORBVWAPSupertrendStrategy(cfg)
        candles = _make_candles(
            [
                (24500, 24515, 24495, 24505, 50000),  # 09:15
                (24505, 24520, 24500, 24515, 50000),  # 09:20
                (24515, 24525, 24510, 24520, 50000),  # 09:25 (ORH=24525)
                (24520, 24555, 24518, 24550, 90000),  # 09:30 breakout > ORH, > VWAP, BULLISH
            ]
        )
        signal = strategy.evaluate(candles)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.action, "BUY_CE")

    def test_strategy_blocks_weak_volume_and_wide_orb_filters(self) -> None:
        # Weak breakout volume (< 1.25x average) should be filtered out
        cfg = BotConfig(
            supertrend_period=3,
            supertrend_multiplier=1.5,
            enable_volume_filter=True,
            min_volume_ratio=1.5,
            enable_adx_filter=False,
            enable_htf_filter=False,
        )
        strategy = ORBVWAPSupertrendStrategy(cfg)
        weak_vol_candles = _make_candles(
            [
                (24500, 24515, 24495, 24505, 80000),
                (24505, 24520, 24500, 24515, 80000),
                (24515, 24525, 24510, 24520, 80000),
                (24520, 24555, 24518, 24550, 50000),  # Weak volume 0.62x
            ]
        )
        sig_weak = strategy.evaluate(weak_vol_candles)
        self.assertIsNotNone(sig_weak)
        assert sig_weak is not None
        self.assertEqual(sig_weak.action, "HOLD")
        self.assertIn("Weak volume", sig_weak.reason)

        # Exhausted huge opening range (> 0.75%) should be filtered out
        cfg_orb = BotConfig(enable_orb_range_filter=True, max_orb_range_pct=0.0075)
        strategy_orb = ORBVWAPSupertrendStrategy(cfg_orb)
        huge_orb_candles = _make_candles(
            [
                (24500, 24720, 24480, 24650, 80000),  # ~0.98% range in 15 min
                (24650, 24710, 24600, 24680, 80000),
                (24680, 24715, 24640, 24700, 80000),
                (24700, 24760, 24695, 24750, 140000),
            ]
        )
        sig_orb = strategy_orb.evaluate(huge_orb_candles)
        self.assertIsNotNone(sig_orb)
        assert sig_orb is not None
        self.assertEqual(sig_orb.action, "HOLD")
        self.assertIn("Opening range too wide", sig_orb.reason)

    def test_strike_selection_respects_5000_capital_and_lot_size_65(self) -> None:
        cfg = BotConfig(capital=5000.0, lot_size=65, charge_buffer=150.0, max_lots=2)
        risk = RiskManager(cfg)
        stats = DailyStats(trade_date="2026-11-18", starting_capital=5000.0)

        contracts = [
            OptionContract("NIFTY24500CE", "1", "NFO", 24500, "CE", "2026-11-26", 65, ltp=115.0),  # Too expensive (₹7,475)
            OptionContract("NIFTY24550CE", "2", "NFO", 24550, "CE", "2026-11-26", 65, ltp=72.0),   # Fits 1 lot (₹4,680)
            OptionContract("NIFTY24650CE", "3", "NFO", 24650, "CE", "2026-11-26", 65, ltp=34.0),   # Further OTM
            OptionContract("NIFTY24850CE", "4", "NFO", 24850, "CE", "2026-11-26", 65, ltp=12.0),   # Too cheap & far
        ]
        sel = risk.select_affordable_option(contracts, spot_price=24510.0, option_type="CE", stats=stats)
        self.assertIsNotNone(sel.contract)
        assert sel.contract is not None
        self.assertEqual(sel.contract.strike, 24550)
        self.assertEqual(sel.lots, 1)

    def test_trailing_sl_breakeven_and_square_off(self) -> None:
        cfg = BotConfig(capital=5000.0, lot_size=65, enable_bolt_time_stop=False)
        risk = RiskManager(cfg)
        c = OptionContract("NIFTY24550CE", "2", "NFO", 24550, "CE", "2026-11-26", 65, ltp=60.0)
        pos = risk.build_position(c, lots=1, entry_time=datetime(2026, 11, 18, 9, 35), entry_spot=24520.0, entry_reason="test")
        self.assertAlmostEqual(pos.stop_loss, 49.2)  # -18% SL (49.20 on 60.0)
        self.assertAlmostEqual(pos.target, 90.0)  # +50% target (90.0 on 60.0)

        # Price moves +15% -> Breakeven SL activates (trigger is +12%)
        u1 = risk.evaluate_position(pos, current_ltp=69.0, now_time=datetime(2026, 11, 18, 10, 0))
        self.assertFalse(u1.should_exit)
        self.assertTrue(u1.sl_updated)
        self.assertTrue(pos.breakeven_active)
        self.assertGreater(pos.stop_loss, 60.0)

        # Price moves +25% (₹75) -> Trailing SL activates (trigger is +20%, trail 10% below peak = ₹67.50)
        u2 = risk.evaluate_position(pos, current_ltp=75.0, now_time=datetime(2026, 11, 18, 10, 30))
        self.assertFalse(u2.should_exit)
        self.assertTrue(pos.trailing_active)
        self.assertAlmostEqual(pos.stop_loss, 67.5)

        # Pullback hits trailing SL -> Exits with profit locked
        u3 = risk.evaluate_position(pos, current_ltp=67.0, now_time=datetime(2026, 11, 18, 10, 45))
        self.assertTrue(u3.should_exit)
        self.assertIn("Trailing SL", u3.exit_reason)

        # 15:15 mandatory square-off always triggers exit
        u4 = risk.evaluate_position(pos, current_ltp=78.0, now_time=datetime(2026, 11, 18, 15, 15))
        self.assertTrue(u4.should_exit)
        self.assertIn("square-off", u4.exit_reason)

    def test_daily_max_loss_and_max_trades_halt(self) -> None:
        cfg = BotConfig(capital=5000.0, lot_size=65, max_daily_loss=1000.0, max_trades_per_day=2)
        risk = RiskManager(cfg)
        broker = PaperBroker(cfg, risk, trade_date="2026-11-18", write_journal=False)

        c = OptionContract("NIFTY24550CE", "2", "NFO", 24550, "CE", "2026-11-26", 65, ltp=70.0)
        broker.open_position(c, lots=1, entry_time=datetime(2026, 11, 18, 9, 35), entry_spot=24520.0, entry_reason="t1")
        # Big loss exceeding ₹1,000 net -> must halt trading immediately
        broker.close_position(exit_price=50.0, exit_time=datetime(2026, 11, 18, 9, 50), exit_spot=24470.0, exit_reason="SL")

        allowed, reason = risk.can_take_new_trade(broker.stats)
        self.assertFalse(allowed)
        self.assertIn("Daily max loss", reason)

    def test_angel_parsing_helpers_and_backtest_smoke(self) -> None:
        self.assertEqual(_parse_angel_strike("2450000.000000"), 24500)
        self.assertEqual(_parse_angel_strike("24500"), 24500)
        self.assertEqual(_parse_angel_expiry("26NOV2026"), date(2026, 11, 26))
        self.assertTrue(_is_auth_error({"status": False, "errorcode": "AG8001", "message": "Invalid Token"}))
        self.assertFalse(_is_auth_error({"status": True, "data": {"ltp": 24500.0}}))

        cfg = BotConfig()
        report = run_backtest(cfg, days=10, seed=7)
        self.assertEqual(report.days_tested, 10)
        self.assertGreaterEqual(report.total_trades, 1)

    def test_live_closed_bar_timing_and_stale_yesterday_guard(self) -> None:
        # Before 09:20:06 no 5-min bar has closed yet
        self.assertIsNone(_latest_expected_closed_bar(datetime(2026, 11, 19, 9, 19, 59)))
        self.assertEqual(
            _latest_expected_closed_bar(datetime(2026, 11, 19, 9, 20, 6)),
            datetime(2026, 11, 19, 9, 15, 0),
        )
        self.assertEqual(
            _latest_expected_closed_bar(datetime(2026, 11, 19, 9, 35, 10)),
            datetime(2026, 11, 19, 9, 30, 0),
        )

        # Forming candle at 09:30 should be dropped at 09:32, kept at 09:35
        bars = _make_candles(
            [
                (24500, 24510, 24495, 24505, 50000),  # 09:15
                (24505, 24515, 24500, 24510, 50000),  # 09:20
                (24510, 24520, 24505, 24515, 50000),  # 09:25
                (24515, 24540, 24512, 24535, 90000),  # 09:30
            ]
        )
        self.assertEqual(len(_filter_closed_candles(bars, datetime(2026, 11, 18, 9, 32))), 3)
        self.assertEqual(len(_filter_closed_candles(bars, datetime(2026, 11, 18, 9, 35))), 4)

        # Yesterday's candles must be ignored when expected_trade_date is today
        cfg = BotConfig()
        strategy = ORBVWAPSupertrendStrategy(cfg)
        self.assertIsNone(strategy.evaluate(bars, expected_trade_date=date(2026, 11, 19)))

    def test_angel_client_auto_relogin_and_batch_ltp(self) -> None:
        cfg = BotConfig(lot_size=65)
        client = AngelOneClient(cfg)

        class FakeSmartAPI:
            def __init__(self) -> None:
                self.calls = 0

            def ltpData(self, exchange: str, symbol: str, token: str):
                self.calls += 1
                if self.calls == 1:
                    return {"status": False, "errorcode": "AG8001", "message": "Invalid Token"}
                return {"status": True, "data": {"ltp": 24580.5}}

            def getMarketData(self, mode: str, payload: dict):
                return {
                    "status": True,
                    "data": {
                        "fetched": [
                            {"symbolToken": "101", "ltp": 64.5},
                            {"symbolToken": "102", "ltp": 42.0},
                        ]
                    },
                }

        fake_api = FakeSmartAPI()
        relogin_count = {"n": 0}

        def fake_login() -> None:
            relogin_count["n"] += 1
            client._smart_api = fake_api

        client._smart_api = fake_api
        client.login = fake_login  # type: ignore[method-assign]

        # Should detect AG8001, relogin once, and succeed with 24580.5
        spot = client.get_nifty_spot_ltp()
        self.assertAlmostEqual(spot, 24580.5)
        self.assertEqual(relogin_count["n"], 1)

        # Batch NFO option LTP lookup
        batch = client._fetch_batch_nfo_ltps(["101", "102"])
        self.assertEqual(batch, {"101": 64.5, "102": 42.0})

    def test_ai_confluence_scoring_and_bolt_filter(self) -> None:
        # High-momentum bullish alignment should score >= 80 pts
        score_bull, bd_bull = compute_ai_confluence_score(
            close=24550.0,
            vwap=24510.0,
            orb_high=24525.0,
            orb_low=24480.0,
            supertrend=24470.0,
            direction="BULLISH",
            htf_trend="BULLISH",
            adx=26.0,
            plus_di=28.0,
            minus_di=12.0,
            volume_ratio=1.45,
            orb_range_pct=0.0035,
            option_type="CE",
        )
        self.assertGreaterEqual(score_bull, 80)
        self.assertEqual(bd_bull["price_action"], 25)
        self.assertEqual(bd_bull["htf_15m"], 20)

        # Choppy / weak alignment should score < 80 pts
        score_weak, _ = compute_ai_confluence_score(
            close=24526.0,
            vwap=24528.0,  # Below VWAP
            orb_high=24525.0,
            orb_low=24480.0,
            supertrend=24530.0,
            direction="BEARISH",
            htf_trend="NEUTRAL",
            adx=14.0,  # Choppy ADX
            plus_di=15.0,
            minus_di=16.0,
            volume_ratio=0.8,
            orb_range_pct=0.0035,
            option_type="CE",
        )
        self.assertLess(score_weak, 50)

    def test_bolt_time_stop_protection(self) -> None:
        cfg = BotConfig(enable_bolt_time_stop=True, max_hold_minutes=35)
        risk = RiskManager(cfg)
        c = OptionContract("NIFTY24550CE", "2", "NFO", 24550, "CE", "2026-11-26", 65, ltp=60.0)
        entry_time = datetime(2026, 11, 20, 9, 35)
        pos = risk.build_position(c, lots=1, entry_time=entry_time, entry_spot=24520.0, entry_reason="bolt_test")

        # 20 minutes elapsed, no target, no SL -> Should continue holding
        u1 = risk.evaluate_position(pos, current_ltp=62.0, now_time=entry_time + timedelta(minutes=20))
        self.assertFalse(u1.should_exit)

        # 36 minutes elapsed -> Bolt Scalp Time-Stop triggers to prevent theta decay
        u2 = risk.evaluate_position(pos, current_ltp=61.0, now_time=entry_time + timedelta(minutes=36))
        self.assertTrue(u2.should_exit)
        self.assertIn("Bolt Scalp Time-Stop", u2.exit_reason)


class TestLiveDashboardAndExpiry(unittest.TestCase):
    def test_nearest_expiry_rolls_over_past_23_nov(self) -> None:
        from free_feed_client import get_nearest_nifty_expiry
        exp = get_nearest_nifty_expiry(date(2026, 11, 24))
        self.assertEqual(exp, date(2026, 12, 1))

    def test_manager_get_state_populates_active_contracts(self) -> None:
        from web_server import WebBotManager
        mgr = WebBotManager()
        state = mgr.get_state()
        market = state["market"]
        self.assertIn("01 Dec", market["ce_contract"])
        self.assertIn("01 Dec", market["pe_contract"])
        self.assertGreater(market["ce_trigger"], 0.0)
        self.assertGreater(market["pe_trigger"], 0.0)
        self.assertGreater(market["ce_ltp"], 0.0)
        self.assertGreater(market["pe_ltp"], 0.0)
        self.assertNotIn("23 Nov", market["ce_contract"])
        self.assertNotIn("23 Nov", market["pe_contract"])


if __name__ == "__main__":
    unittest.main()
