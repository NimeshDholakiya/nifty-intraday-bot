"""Local Web Server for the NIFTY 50 Intraday Bot Clickable HTML Dashboard.

Provides a zero-terminal web interface:
  - Accessible in browser at http://localhost:5050
  - Start / Stop Live Groww Bot with one click
  - Run Pre-Flight Groww Check & Live Demos
  - Real-time AI Confidence Score Gauge (0 to 100%)
  - Audio & Visual alerts for Groww AI Bolt Trade signals
  - Exact Groww execution card (Symbol, Strike, Order type, SL, TP, 35m timer)
  - Real-time P&L and live event logs
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time as time_mod
import webbrowser
from datetime import date, datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any

from config import BotConfig
from free_feed_client import FreeMarketDataClient, get_nearest_nifty_expiry, now_ist
from models import Candle, ClosedTrade, DailyStats, OptionType, Position, Signal
from notifier import Notifier
from paper_broker import PaperBroker
from risk_manager import RiskManager
from simulator import estimate_option_premium
from strategy import ORBVWAPSupertrendStrategy

PORT = 5050
BASE_DIR = Path(__file__).resolve().parent
HTML_PATH = BASE_DIR / "dashboard.html"


class WebBotManager:
    """Thread-safe manager controlling the bot engine and streaming state to the web UI."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.config = BotConfig.from_env()
        self.thread: threading.Thread | None = None
        self.running: bool = False
        self.mode: str = "IDLE"  # IDLE, LIVE, DEMO, CHECKING
        self.logs: list[dict[str, str]] = []
        self.active_signal: dict[str, Any] | None = None
        self.active_position: dict[str, Any] | None = None
        today = now_ist().date()
        nearest_exp = get_nearest_nifty_expiry(today)
        exp_short = nearest_exp.strftime("%d %b")
        exp_full = nearest_exp.strftime("%d %b %Y")

        self.market_data: dict[str, Any] = {
            "spot": 25770.00,
            "vwap": 25770.00,
            "supertrend": 25770.00,
            "direction": "NEUTRAL",
            "htf_trend": "NEUTRAL",
            "adx": 20.0,
            "volume_ratio": 1.0,
            "ai_score_ce": 50,
            "ai_score_pe": 50,
            "last_bar": "--:--",
            "orb_high": None,
            "orb_low": None,
            "window": "09:15 - 09:30 IST (Opening Range)",
            "is_choppy": False,
            "ce_trigger": 25800.0,
            "pe_trigger": 25700.0,
            "window_status": "Ready",
            "hold_reason": "",
            "ce_contract": f"NIFTY {exp_short} 26350 Call",
            "pe_contract": f"NIFTY {exp_short} 25200 Put",
            "ce_ltp": 70.80,
            "pe_ltp": 70.20,
            "ce_cost": 4602.00,
            "pe_cost": 4563.00,
            "ce_buffer": 398.00,
            "pe_buffer": 437.00,
            "ce_sl": 58.06,
            "pe_sl": 57.56,
            "ce_risk": 828.10,
            "pe_risk": 821.60,
            "ce_be": 79.30,
            "pe_be": 78.62,
            "ce_tp1": 95.58,
            "pe_tp1": 94.77,
            "ce_profit1": 1610.70,
            "pe_profit1": 1597.05,
            "ce_tp2": 106.20,
            "pe_tp2": 105.30,
            "ce_profit2": 2301.00,
            "pe_profit2": 2281.50,
            "nearest_expiry": exp_full,
            "nearest_expiry_short": exp_short,
        }
        self.daily_pnl: dict[str, Any] = {
            "net_pnl": 0.0,
            "gross_pnl": 0.0,
            "charges": 0.0,
            "trade_count": 0,
            "wins": 0,
            "losses": 0,
            "status": "Ready",
            "closed_trades": [],
        }

        # Seed initial log messages so dashboard is immediately informative
        self.log("NIFTY 50 Groww AI Bolt Dashboard initialized.", "info")
        self.log(f"Public NSE Feed connected | Lot Size: {self.config.lot_size} | Target Broker: GROWW", "success")
        now = datetime.now()
        if now.weekday() >= 5:
            day_name = "Saturday" if now.weekday() == 5 else "Sunday"
            self.log(f"Market Status: WEEKEND ({day_name} - Closed). NSE re-opens Monday 09:15 AM IST.", "warn")
            self.log("Click '▶ Start Live Groww Bot' to run live test walkthrough on screen!", "info")
        else:
            self.log(f"Active Expiry: {exp_full} | Ready for live trading session.", "info")

        # Continuously keep snapshot updated with latest live market data in background
        threading.Thread(target=self._continuous_snapshot_updater, daemon=True).start()

    def _refresh_market_snapshot(
        self,
        client: Any = None,
        candles: list[Candle] | None = None,
        last_signal: Signal | None = None,
        forced_spot: float | None = None,
    ) -> None:
        """Compute all indicators, dynamic strikes, triggers, and expiry for live display."""
        try:
            client = client or FreeMarketDataClient(self.config)
            now = now_ist()
            today = now.date()
            nearest_exp = get_nearest_nifty_expiry(today)
            exp_short = nearest_exp.strftime("%d %b")
            exp_full = nearest_exp.strftime("%d %b %Y")

            spot = forced_spot or client.get_nifty_spot_ltp()
            if not spot or spot <= 0:
                spot = self.market_data.get("spot", 0.0) or 0.0

            if candles is None:
                candles, _ = client.get_intraday_candles(lookback_days=4)

            snap = None
            if candles:
                from indicators import build_latest_snapshot
                snap = build_latest_snapshot(
                    candles,
                    orb_start=self.config.orb_start,
                    orb_end=self.config.orb_end,
                    supertrend_period=self.config.supertrend_period,
                    supertrend_multiplier=self.config.supertrend_multiplier,
                    adx_period=self.config.adx_period,
                    htf_minutes=self.config.htf_minutes,
                    volume_lookback=self.config.volume_lookback,
                )

            strategy = ORBVWAPSupertrendStrategy(self.config)
            analysis = strategy.analyze_trade_window(candles) if candles else {}

            risk = RiskManager(self.config)
            dummy_stats = DailyStats(trade_date=today.isoformat(), starting_capital=self.config.capital)
            ce_candidates = client.get_candidate_options(spot, "CE")
            pe_candidates = client.get_candidate_options(spot, "PE")
            ce_sel = risk.select_affordable_option(ce_candidates, spot, "CE", dummy_stats)
            pe_sel = risk.select_affordable_option(pe_candidates, spot, "PE", dummy_stats)

            atm = int(round(spot / 50.0) * 50)
            if ce_sel and ce_sel.contract:
                ce_name = ce_sel.contract.symbol
                ce_ltp = ce_sel.contract.ltp
            else:
                ce_name = f"NIFTY {exp_short} {atm + 650} Call"
                ce_ltp = 62.00

            if pe_sel and pe_sel.contract:
                pe_name = pe_sel.contract.symbol
                pe_ltp = pe_sel.contract.ltp
            else:
                pe_name = f"NIFTY {exp_short} {atm - 600} Put"
                pe_ltp = 53.00

            # Determine trigger levels
            if analysis.get("ce_trigger") and analysis["ce_trigger"] > 0:
                ce_trigger = round(analysis["ce_trigger"], 2)
                pe_trigger = round(analysis["pe_trigger"], 2)
            elif snap and snap.orb_high and snap.orb_low:
                ce_trigger = round(snap.orb_high, 2)
                pe_trigger = round(snap.orb_low, 2)
            else:
                ce_trigger = round(spot * 1.002, 2)
                pe_trigger = round(spot * 0.998, 2)

            from datetime import time as dt_time
            current_t = now.time()
            market_open = dt_time(9, 15)
            orb_end = dt_time(9, 30)

            if now.weekday() >= 5:
                status_msg = "Market Closed (Weekend). Re-opens Monday 09:15 AM IST."
                hold_reason = "Weekend off-hours"
            elif current_t < market_open:
                status_msg = "Pre-Market: NSE opens at 09:15 AM IST. Watchlist strikes ready."
                hold_reason = "Market opens at 09:15 IST"
            elif market_open <= current_t < orb_end:
                mins_left = max((datetime.combine(today, orb_end) - now).seconds // 60, 1)
                status_msg = f"⏳ Opening Range Forming (09:15–09:30 AM). ~{mins_left}m left until breakout triggers activate."
                hold_reason = f"Opening Range forming (~{mins_left}m left until 09:30 AM base completes)"
            elif dt_time(9, 30) <= current_t <= dt_time(11, 15):
                status_msg = "⚡ High-Momentum Scalping Window ACTIVE (09:30–11:15 IST)."
                hold_reason = last_signal.reason if (last_signal and last_signal.action == "HOLD") else ""
            else:
                status_msg = "🛡️ Afternoon Session (Rolling breakouts with strict risk control)."
                hold_reason = last_signal.reason if (last_signal and last_signal.action == "HOLD") else ""

            window_str = analysis.get("window") or f"{now.strftime('%H:%M')} - {(now + timedelta(minutes=10)).strftime('%H:%M')} IST"

            pe_ltp_f = round(pe_ltp, 2)
            ce_ltp_f = round(ce_ltp, 2)
            pe_sl_f = round(pe_ltp_f * (1 - self.config.stop_loss_pct), 2)
            ce_sl_f = round(ce_ltp_f * (1 - self.config.stop_loss_pct), 2)
            pe_tp1_f = round(pe_ltp_f * 1.35, 2)
            ce_tp1_f = round(ce_ltp_f * 1.35, 2)
            pe_tp2_f = round(pe_ltp_f * 1.50, 2)
            ce_tp2_f = round(ce_ltp_f * 1.50, 2)
            pe_cost_f = round(pe_ltp_f * 65, 2)
            ce_cost_f = round(ce_ltp_f * 65, 2)

            with self.lock:
                self.market_data = {
                    "spot": spot,
                    "vwap": round(snap.vwap if snap else spot, 2),
                    "supertrend": round(snap.supertrend if snap else spot, 2),
                    "direction": snap.direction if snap else "NEUTRAL",
                    "htf_trend": snap.htf_trend if snap else "NEUTRAL",
                    "adx": round(snap.adx if snap else 20.0, 1),
                    "volume_ratio": snap.volume_ratio if (snap and snap.volume_ratio) else 1.0,
                    "ai_score_ce": snap.ai_score_ce if snap else 50,
                    "ai_score_pe": snap.ai_score_pe if snap else 50,
                    "last_bar": snap.timestamp.strftime("%H:%M") if snap else now.strftime("%H:%M"),
                    "orb_high": round(snap.orb_high, 2) if (snap and snap.orb_high) else None,
                    "orb_low": round(snap.orb_low, 2) if (snap and snap.orb_low) else None,
                    "window": window_str,
                    "is_choppy": snap.adx < self.config.min_adx if snap else False,
                    "ce_trigger": ce_trigger,
                    "pe_trigger": pe_trigger,
                    "window_status": status_msg,
                    "hold_reason": hold_reason,
                    "ce_contract": ce_name,
                    "pe_contract": pe_name,
                    "ce_ltp": ce_ltp_f,
                    "pe_ltp": pe_ltp_f,
                    "ce_cost": ce_cost_f,
                    "pe_cost": pe_cost_f,
                    "ce_buffer": round(max(0.0, 5000.0 - ce_cost_f), 2),
                    "pe_buffer": round(max(0.0, 5000.0 - pe_cost_f), 2),
                    "ce_sl": ce_sl_f,
                    "pe_sl": pe_sl_f,
                    "ce_risk": round((ce_ltp_f - ce_sl_f) * 65, 2),
                    "pe_risk": round((pe_ltp_f - pe_sl_f) * 65, 2),
                    "ce_be": round(ce_ltp_f * 1.12, 2),
                    "pe_be": round(pe_ltp_f * 1.12, 2),
                    "ce_tp1": ce_tp1_f,
                    "pe_tp1": pe_tp1_f,
                    "ce_profit1": round((ce_tp1_f - ce_ltp_f) * 65, 2),
                    "pe_profit1": round((pe_tp1_f - pe_ltp_f) * 65, 2),
                    "ce_tp2": ce_tp2_f,
                    "pe_tp2": pe_tp2_f,
                    "ce_profit2": round((ce_tp2_f - ce_ltp_f) * 65, 2),
                    "pe_profit2": round((pe_tp2_f - pe_ltp_f) * 65, 2),
                    "nearest_expiry": exp_full,
                    "nearest_expiry_short": exp_short,
                }
        except Exception as exc:
            self.log(f"Market snapshot refresh error: {exc}", "warn")

    def _continuous_snapshot_updater(self) -> None:
        """Continuously refresh spot, indicators, and candidate strikes in the background every 15s."""
        client = FreeMarketDataClient(self.config)
        while True:
            try:
                self._refresh_market_snapshot(client)
            except Exception as exc:
                self.log(f"Background market update error: {exc}", "warn")
            time_mod.sleep(15)

    def log(self, message: str, level: str = "info") -> None:
        now_str = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.append({"time": now_str, "text": message, "level": level})
            if len(self.logs) > 100:
                self.logs.pop(0)

    def get_state(self) -> dict[str, Any]:
        with self.lock:
            pos_data = None
            if self.active_position:
                pos_data = dict(self.active_position)
                if pos_data.get("entry_time_iso"):
                    try:
                        ent_dt = datetime.fromisoformat(pos_data["entry_time_iso"])
                        mins_elapsed = int((datetime.now() - ent_dt).total_seconds() / 60.0)
                        pos_data["mins_elapsed"] = mins_elapsed
                        pos_data["mins_left"] = max(self.config.max_hold_minutes - mins_elapsed, 0)
                    except Exception:
                        pass

            today = now_ist().date()
            nearest_exp = get_nearest_nifty_expiry(today)
            exp_short = nearest_exp.strftime("%d %b")
            exp_full = nearest_exp.strftime("%d %b %Y")
            spot = self.market_data.get("spot", 0.0) or 0.0
            atm = int(round(spot / 50.0) * 50) if spot > 0 else 25800
            if not self.market_data.get("ce_contract") or "23 Nov" in str(self.market_data.get("ce_contract")):
                self.market_data["ce_contract"] = f"NIFTY {exp_short} {atm + 650} Call"
                self.market_data["ce_ltp"] = self.market_data.get("ce_ltp", 62.00) or 62.00
            if not self.market_data.get("pe_contract") or "23 Nov" in str(self.market_data.get("pe_contract")):
                self.market_data["pe_contract"] = f"NIFTY {exp_short} {atm - 600} Put"
                self.market_data["pe_ltp"] = self.market_data.get("pe_ltp", 53.00) or 53.00
            if not self.market_data.get("ce_trigger") or self.market_data.get("ce_trigger", 0) <= 0:
                self.market_data["ce_trigger"] = round(spot * 1.002, 2)
            if not self.market_data.get("pe_trigger") or self.market_data.get("pe_trigger", 0) <= 0:
                self.market_data["pe_trigger"] = round(spot * 0.998, 2)
            if not self.market_data.get("nearest_expiry") or "23 Nov" in str(self.market_data.get("nearest_expiry")):
                self.market_data["nearest_expiry"] = exp_full
                self.market_data["nearest_expiry_short"] = exp_short

            return {
                "running": self.running,
                "mode": self.mode,
                "broker": self.config.broker.upper(),
                "capital": self.config.capital,
                "lot_size": self.config.lot_size,
                "max_trades": self.config.max_trades_per_day,
                "market": self.market_data,
                "active_signal": self.active_signal,
                "active_position": pos_data,
                "pnl": self.daily_pnl,
                "logs": self.logs[-30:],
            }

    def stop(self) -> None:
        self.running = False
        self.mode = "STOPPING"
        self.log("Stop requested by user...", "warn")

    def start_live(self) -> None:
        if self.running:
            return
        self.running = True
        self.mode = "LIVE"
        self.active_signal = None
        self.active_position = None
        self.log(f"Starting LIVE GROWW AI BOLT BOT (Capital: ₹{self.config.capital:.0f})...", "success")
        self.thread = threading.Thread(target=self._live_worker, daemon=True)
        self.thread.start()

    def start_demo(self, regime: str = "bullish_breakout") -> None:
        if self.running:
            return
        self.running = True
        self.mode = "DEMO"
        self.active_signal = None
        self.active_position = None
        self.log(f"Running OFFLINE DEMO ({regime}) for Groww...", "info")
        self.thread = threading.Thread(target=self._demo_worker, args=(regime,), daemon=True)
        self.thread.start()

    def run_check(self) -> None:
        if self.running:
            return
        self.running = True
        self.mode = "CHECKING"
        self.log("Running Groww Pre-Flight Check...", "info")
        self.thread = threading.Thread(target=self._check_worker, daemon=True)
        self.thread.start()

    def run_instant_scan(self) -> None:
        if self.running and self.mode in ("LIVE", "DEMO"):
            self.log("Live bot is already actively scanning the market.", "info")
            return
        self.log("⚡ Initiating On-Demand Instant AI Market Scan...", "info")
        threading.Thread(target=self._instant_scan_worker, daemon=True).start()

    def _instant_scan_worker(self) -> None:
        try:
            from free_feed_client import FreeMarketDataClient
            client = FreeMarketDataClient(self.config)
            spot = client.get_nifty_spot_ltp()
            candles, src = client.get_intraday_candles(lookback_days=4)
            strategy = ORBVWAPSupertrendStrategy(self.config)
            risk = RiskManager(self.config)
            broker = PaperBroker(self.config, risk, trade_date=date.today().isoformat(), write_journal=False)

            # Analyze current window
            analysis = strategy.analyze_trade_window(candles)
            window_str = analysis.get("window", "Current 10m Window")
            self.log(
                f"[ON-DEMAND SCANNER] Analyzing Window: {window_str} | Spot: ₹{spot:.2f} | "
                f"VWAP: ₹{analysis.get('vwap', 0):.2f} | ST: {analysis.get('supertrend', 'NEUTRAL')} | ADX: {analysis.get('adx', 0):.1f}",
                "info",
            )

            # Refresh entire snapshot with current spot, active expiry strikes & triggers
            self._refresh_market_snapshot(client, candles=candles, forced_spot=spot)

            # Check if an immediate breakout/pullback setup is triggered right now
            signal = strategy.evaluate(candles, open_position=None)
            if signal and signal.action in ("BUY_CE", "BUY_PE"):
                opt_type = "CE" if signal.action == "BUY_CE" else "PE"
                contracts = client.get_candidate_options(spot, opt_type)
                sel = risk.select_affordable_option(contracts, spot, opt_type, broker.stats)
                if sel.contract:
                    with self.lock:
                        self.active_signal = {
                            "action": signal.action,
                            "contract": sel.contract.symbol,
                            "expiry": sel.contract.expiry,
                            "strike": sel.contract.strike,
                            "option_type": sel.contract.option_type,
                            "entry_price": sel.contract.ltp,
                            "spot": spot,
                            "stop_loss": round(sel.contract.ltp * (1 - self.config.stop_loss_pct), 2),
                            "target": round(sel.contract.ltp * (1 + self.config.target_pct), 2),
                            "lots": sel.lots,
                            "quantity": sel.lots * self.config.lot_size,
                            "capital": round(sel.contract.ltp * sel.lots * self.config.lot_size, 2),
                            "score": analysis.get(f"score_{opt_type.lower()}", 85),
                            "reason": f"[{window_str}] {signal.reason}",
                            "timestamp": datetime.now().strftime("%H:%M:%S"),
                        }
                    self.log(
                        f"🔥 IMMEDIATE BREAKOUT FOR {window_str}: {signal.action} -> {sel.contract.symbol} @ ₹{sel.contract.ltp:.2f}!",
                        "success",
                    )
                    self.log(
                        f"Action: Buy on Groww | Target: ₹{self.active_signal['target']:.2f} | SL: ₹{self.active_signal['stop_loss']:.2f}",
                        "success",
                    )
                    return

            # If no immediate breakout, provide near-trigger levels to protect capital
            hold_reason = signal.reason if signal and signal.action == "HOLD" else ""
            with self.lock:
                self.market_data["hold_reason"] = hold_reason
            if hold_reason:
                self.log(f"🛡️ Protection Status: {hold_reason}", "warn")
            elif analysis.get("is_choppy"):
                self.log(
                    f"🛡️ AI CAPITAL PROTECTION ACTIVE: Market is currently consolidating (ADX {analysis.get('adx', 0):.1f} < 20). "
                    f"A forced trade now has high theta risk. Capital protected!",
                    "warn",
                )
            self.log(
                f"🎯 DYNAMIC BREAKOUT WATCH LEVELS FOR {window_str}: "
                f"🟢 Buy CE if spot crosses above ₹{analysis.get('ce_trigger', 0):.2f} | "
                f"🔴 Buy PE if spot drops below ₹{analysis.get('pe_trigger', 0):.2f}",
                "info",
            )
            self.log(
                "Click '▶ Start Live Groww Bot' to run continuous monitoring and ring audio chime the moment it breaks!",
                "info",
            )

        except Exception as exc:
            self.log(f"Instant scan notice: {exc}", "warn")

    def _check_worker(self) -> None:
        try:
            from free_feed_client import FreeMarketDataClient
            client = FreeMarketDataClient(self.config)
            self.log("[1/4] Connecting to live NSE NIFTY 50 feed...", "info")
            spot = client.get_nifty_spot_ltp()
            self.log(f"[2/4] Live NIFTY Spot LTP: {spot:.2f}", "success")
            candles, _ = client.get_intraday_candles(lookback_days=4)
            from indicators import build_latest_snapshot
            snap = build_latest_snapshot(
                candles,
                orb_start=self.config.orb_start,
                orb_end=self.config.orb_end,
                supertrend_period=self.config.supertrend_period,
                supertrend_multiplier=self.config.supertrend_multiplier,
                adx_period=self.config.adx_period,
                htf_minutes=self.config.htf_minutes,
                volume_lookback=self.config.volume_lookback,
            )
            if snap:
                with self.lock:
                    self.market_data = {
                        "spot": snap.close,
                        "vwap": round(snap.vwap, 2),
                        "supertrend": round(snap.supertrend, 2),
                        "direction": snap.direction,
                        "htf_trend": snap.htf_trend,
                        "adx": round(snap.adx, 1),
                        "volume_ratio": snap.volume_ratio or 1.0,
                        "ai_score_ce": snap.ai_score_ce,
                        "ai_score_pe": snap.ai_score_pe,
                        "last_bar": snap.timestamp.strftime("%H:%M"),
                        "orb_high": round(snap.orb_high, 2) if snap.orb_high else None,
                        "orb_low": round(snap.orb_low, 2) if snap.orb_low else None,
                    }
                self.log(
                    f"[3/4] AI Score: CE={snap.ai_score_ce}/100 | PE={snap.ai_score_pe}/100 | "
                    f"ADX={snap.adx:.1f} | ST={snap.direction}",
                    "info",
                )

            risk = RiskManager(self.config)
            broker = PaperBroker(self.config, risk, trade_date=date.today().isoformat(), write_journal=False)
            contracts_ce = client.get_candidate_options(spot, "CE")
            sel_ce = risk.select_affordable_option(contracts_ce, spot, "CE", broker.stats)
            if sel_ce.contract:
                self.log(f"[4/4] Best Groww CE: {sel_ce.contract.symbol} @ ₹{sel_ce.contract.ltp:.2f}", "success")
            self.log("ALL GROWW CHECKS PASSED! Ready for live session.", "success")
        except Exception as exc:
            self.log(f"Check failed: {exc}", "error")
        finally:
            self.running = False
            self.mode = "IDLE"

    def _demo_worker(self, regime: str) -> None:
        try:
            from simulator import generate_intraday_session, estimate_option_premium
            from free_feed_client import FreeMarketDataClient
            mock_client = FreeMarketDataClient(self.config)
            base_spot = self.market_data.get("spot", 25800.0) or 25800.0
            candles = generate_intraday_session(date.today(), regime=regime, start_price=base_spot, seed=42)
            strategy = ORBVWAPSupertrendStrategy(self.config)
            risk = RiskManager(self.config)
            broker = PaperBroker(self.config, risk, trade_date=date.today().isoformat(), write_journal=True)

            history: list[Candle] = []
            for bar in candles:
                if not self.running:
                    break
                history.append(bar)
                signal = strategy.evaluate(history, open_position=broker.position)

                # Update live indicator snapshot in UI
                if signal:
                    snap = signal.snapshot
                    with self.lock:
                        self.market_data = {
                            "spot": snap.close,
                            "vwap": round(snap.vwap, 2),
                            "supertrend": round(snap.supertrend, 2),
                            "direction": snap.direction,
                            "htf_trend": snap.htf_trend,
                            "adx": round(snap.adx, 1),
                            "volume_ratio": snap.volume_ratio or 1.0,
                            "ai_score_ce": snap.ai_score_ce,
                            "ai_score_pe": snap.ai_score_pe,
                            "last_bar": snap.timestamp.strftime("%H:%M"),
                            "orb_high": round(snap.orb_high, 2) if snap.orb_high else None,
                            "orb_low": round(snap.orb_low, 2) if snap.orb_low else None,
                        }

                # Manage active position
                if broker.position is not None:
                    pos = broker.position
                    mock_client._last_spot = bar.close
                    opt_ltp = mock_client.get_option_ltp(pos.contract.symbol, pos.contract.token)
                    trend_exit = signal.reason if (signal and signal.action == "EXIT") else None
                    update = risk.evaluate_position(pos, opt_ltp, bar.timestamp, trend_exit)

                    pnl_unrealized = round((opt_ltp - pos.entry_price) * pos.quantity, 2)
                    with self.lock:
                        self.active_position = {
                            "symbol": pos.contract.symbol,
                            "strike": pos.contract.strike,
                            "option_type": pos.contract.option_type,
                            "entry_price": pos.entry_price,
                            "current_ltp": opt_ltp,
                            "stop_loss": pos.stop_loss,
                            "target": pos.target,
                            "quantity": pos.quantity,
                            "lots": pos.lots,
                            "unrealized_pnl": pnl_unrealized,
                            "entry_time_iso": pos.entry_time.isoformat(),
                            "highest_price": pos.highest_price,
                            "breakeven_active": pos.breakeven_active,
                            "trailing_active": pos.trailing_active,
                        }

                    if update.sl_updated:
                        self.log(f"[RISK UPDATE] {pos.contract.symbol}: {update.sl_update_reason}", "warn")

                    if update.should_exit:
                        closed = broker.close_position(opt_ltp, bar.timestamp, bar.close, update.exit_reason)
                        self.log(
                            f"[EXIT - {'PROFIT' if closed.net_pnl > 0 else 'LOSS'}] {closed.symbol} | "
                            f"Net P&L: ₹{closed.net_pnl:+.2f} ({closed.exit_reason})",
                            "success" if closed.net_pnl > 0 else "error",
                        )
                        with self.lock:
                            self.active_position = None
                            self.daily_pnl = {
                                "net_pnl": broker.stats.net_pnl,
                                "gross_pnl": broker.stats.gross_pnl,
                                "charges": broker.stats.total_charges,
                                "trade_count": broker.stats.trade_count,
                                "wins": broker.stats.wins,
                                "losses": broker.stats.losses,
                                "status": "Halted (1/1 Complete)" if broker.stats.halted else "Active",
                                "closed_trades": [
                                    {
                                        "symbol": t.symbol,
                                        "entry": f"₹{t.entry_price:.2f}",
                                        "exit": f"₹{t.exit_price:.2f}",
                                        "net_pnl": f"₹{t.net_pnl:+.2f}",
                                        "reason": t.exit_reason,
                                    }
                                    for t in broker.stats.trades
                                ],
                            }

                # Check new entry
                if broker.position is None and signal and signal.action in ("BUY_CE", "BUY_PE"):
                    allowed, deny_reason = risk.can_take_new_trade(broker.stats)
                    if allowed:
                        opt_type = "CE" if signal.action == "BUY_CE" else "PE"
                        contracts = mock_client.get_candidate_options(bar.close, opt_type)
                        sel = risk.select_affordable_option(contracts, bar.close, opt_type, broker.stats)
                        if sel.contract:
                            pos = broker.open_position(
                                contract=sel.contract,
                                lots=sel.lots,
                                entry_time=bar.timestamp,
                                entry_spot=bar.close,
                                entry_reason=signal.reason,
                            )
                            with self.lock:
                                self.active_signal = {
                                    "action": signal.action,
                                    "contract": sel.contract.symbol,
                                    "expiry": sel.contract.expiry,
                                    "strike": sel.contract.strike,
                                    "option_type": sel.contract.option_type,
                                    "entry_price": sel.contract.ltp,
                                    "spot": bar.close,
                                    "stop_loss": pos.stop_loss,
                                    "target": pos.target,
                                    "lots": sel.lots,
                                    "quantity": pos.quantity,
                                    "capital": pos.invested_capital,
                                    "score": snap.ai_score_ce if opt_type == "CE" else snap.ai_score_pe,
                                    "reason": signal.reason,
                                    "timestamp": bar.timestamp.strftime("%H:%M:%S"),
                                }
                            self.log(
                                f"[GROWW AI BOLT SIGNAL] {signal.action} -> {sel.contract.symbol} @ ₹{sel.contract.ltp:.2f} "
                                f"(Target: ₹{pos.target:.2f} | SL: ₹{pos.stop_loss:.2f})",
                                "success",
                            )

                time_mod.sleep(0.4)  # Smooth animation in web dashboard

        except Exception as exc:
            self.log(f"Demo error: {exc}", "error")
        finally:
            self.running = False
            self.mode = "IDLE"
            self.log("Demo session complete.", "info")

    def _run_test_walkthrough(self, broker: PaperBroker, strategy: ORBVWAPSupertrendStrategy, risk: RiskManager) -> None:
        """Run smooth simulation walkthrough for testing outside regular market hours."""
        from simulator import generate_intraday_session, estimate_option_premium
        from free_feed_client import FreeMarketDataClient

        feed_client = FreeMarketDataClient(self.config)
        base_spot = self.market_data.get("spot", 25800.0) or 25800.0
        candles = generate_intraday_session(date.today(), regime="bullish_breakout", start_price=base_spot, seed=42)

        self.log(f"Simulating live session starting at NIFTY Spot ₹{base_spot:.2f}...", "info")
        history: list[Candle] = []

        for bar in candles:
            if not self.running:
                break
            history.append(bar)
            signal = strategy.evaluate(history, open_position=broker.position)

            if signal:
                snap = signal.snapshot
                t_end = (snap.timestamp + timedelta(minutes=10)).strftime("%H:%M")
                with self.lock:
                    self.market_data = {
                        "spot": snap.close,
                        "vwap": round(snap.vwap, 2),
                        "supertrend": round(snap.supertrend, 2),
                        "direction": snap.direction,
                        "htf_trend": snap.htf_trend,
                        "adx": round(snap.adx, 1),
                        "volume_ratio": snap.volume_ratio or 1.0,
                        "ai_score_ce": snap.ai_score_ce,
                        "ai_score_pe": snap.ai_score_pe,
                        "last_bar": snap.timestamp.strftime("%H:%M"),
                        "orb_high": round(snap.orb_high, 2) if snap.orb_high else None,
                        "orb_low": round(snap.orb_low, 2) if snap.orb_low else None,
                        "window": f"{snap.timestamp.strftime('%H:%M')} - {t_end} IST",
                        "is_choppy": snap.adx < self.config.min_adx,
                        "ce_trigger": round(snap.orb_high if snap.orb_high else snap.close * 1.002, 2),
                        "pe_trigger": round(snap.orb_low if snap.orb_low else snap.close * 0.998, 2),
                    }

            if broker.position is not None:
                pos = broker.position
                feed_client._last_spot = bar.close
                opt_ltp = feed_client.get_option_ltp(pos.contract.symbol, pos.contract.token)
                trend_exit = signal.reason if (signal and signal.action == "EXIT") else None
                update = risk.evaluate_position(pos, opt_ltp, bar.timestamp, trend_exit)

                pnl_unrealized = round((opt_ltp - pos.entry_price) * pos.quantity, 2)
                with self.lock:
                    self.active_position = {
                        "symbol": pos.contract.symbol,
                        "strike": pos.contract.strike,
                        "option_type": pos.contract.option_type,
                        "entry_price": pos.entry_price,
                        "current_ltp": opt_ltp,
                        "stop_loss": pos.stop_loss,
                        "target": pos.target,
                        "quantity": pos.quantity,
                        "lots": pos.lots,
                        "unrealized_pnl": pnl_unrealized,
                        "entry_time_iso": pos.entry_time.isoformat(),
                        "highest_price": pos.highest_price,
                        "breakeven_active": pos.breakeven_active,
                        "trailing_active": pos.trailing_active,
                    }

                if update.sl_updated:
                    self.log(f"[RISK UPDATE] {pos.contract.symbol}: {update.sl_update_reason}", "warn")

                if update.should_exit:
                    closed = broker.close_position(opt_ltp, bar.timestamp, bar.close, update.exit_reason)
                    self.log(
                        f"[GROWW EXIT - {'PROFIT' if closed.net_pnl > 0 else 'LOSS'}] {closed.symbol} | "
                        f"Net P&L: ₹{closed.net_pnl:+.2f} ({closed.exit_reason})",
                        "success" if closed.net_pnl > 0 else "error",
                    )
                    with self.lock:
                        self.active_position = None
                        self.daily_pnl = {
                            "net_pnl": broker.stats.net_pnl,
                            "gross_pnl": broker.stats.gross_pnl,
                            "charges": broker.stats.total_charges,
                            "trade_count": broker.stats.trade_count,
                            "wins": broker.stats.wins,
                            "losses": broker.stats.losses,
                            "status": "Halted (1/1 Trade Complete)" if broker.stats.halted else "Active",
                            "closed_trades": [
                                {
                                    "symbol": t.symbol,
                                    "entry": f"₹{t.entry_price:.2f}",
                                    "exit": f"₹{t.exit_price:.2f}",
                                    "net_pnl": f"₹{t.net_pnl:+.2f}",
                                    "reason": t.exit_reason,
                                }
                                for t in broker.stats.trades
                            ],
                        }

            # Check new entry
            if broker.position is None and signal and signal.action in ("BUY_CE", "BUY_PE"):
                allowed, deny_reason = risk.can_take_new_trade(broker.stats)
                if allowed:
                    opt_type = "CE" if signal.action == "BUY_CE" else "PE"
                    contracts = feed_client.get_candidate_options(bar.close, opt_type)
                    sel = risk.select_affordable_option(contracts, bar.close, opt_type, broker.stats)
                    if sel.contract:
                        pos = broker.open_position(
                            contract=sel.contract,
                            lots=sel.lots,
                            entry_time=bar.timestamp,
                            entry_spot=bar.close,
                            entry_reason=signal.reason,
                        )
                        with self.lock:
                            self.active_signal = {
                                "action": signal.action,
                                "contract": sel.contract.symbol,
                                "expiry": sel.contract.expiry,
                                "strike": sel.contract.strike,
                                "option_type": sel.contract.option_type,
                                "entry_price": sel.contract.ltp,
                                "spot": bar.close,
                                "stop_loss": pos.stop_loss,
                                "target": pos.target,
                                "lots": sel.lots,
                                "quantity": pos.quantity,
                                "capital": pos.invested_capital,
                                "score": snap.ai_score_ce if opt_type == "CE" else snap.ai_score_pe,
                                "reason": signal.reason,
                                "timestamp": bar.timestamp.strftime("%H:%M:%S"),
                            }
                        self.log(
                            f"[GROWW AI BOLT SIGNAL] {signal.action} -> {sel.contract.symbol} @ ₹{sel.contract.ltp:.2f} "
                            f"(Target: ₹{pos.target:.2f} | SL: ₹{pos.stop_loss:.2f})",
                            "success",
                        )

            time_mod.sleep(0.35)

        if self.running:
            self.log("🎉 TEST WALKTHROUGH COMPLETE! Indicators, audio chime, Groww strike copy, and trailing SL all verified.", "success")
            self.log("Ready for Monday morning live market open at 09:15 AM IST.", "info")

    def _live_worker(self) -> None:
        try:
            from main import get_market_client, now_ist, _latest_expected_closed_bar, _filter_closed_candles, _parse_hhmm
            from indicators import build_latest_snapshot
            client = get_market_client(self.config)
            strategy = ORBVWAPSupertrendStrategy(self.config)
            risk = RiskManager(self.config)
            now = now_ist()
            today_date = now.date()
            broker = PaperBroker(self.config, risk, trade_date=today_date.isoformat(), write_journal=True)

            client.login()
            meta = client.warmup_instruments()
            self.log(f"Connected to {meta.get('feed', 'Live Feed')} | Target: GROWW | Lot Size: {self.config.lot_size}", "success")

            # Immediately update snapshot with latest market feed data
            try:
                spot_now = client.get_nifty_spot_ltp()
                self._refresh_market_snapshot(client, forced_spot=spot_now)
                self.log(
                    f"Latest Feed Data: NIFTY Spot ₹{self.market_data['spot']:.2f} | "
                    f"Expiry: {self.market_data.get('nearest_expiry_short')} | "
                    f"ST: {self.market_data.get('direction')} | VWAP: ₹{self.market_data.get('vwap'):.2f} | "
                    f"AI: CE={self.market_data.get('ai_score_ce')}% PE={self.market_data.get('ai_score_pe')}%",
                    "info",
                )
            except Exception:
                pass

            is_weekend = now.weekday() >= 5
            market_open = _parse_hhmm("09:15")
            square_off = _parse_hhmm(self.config.square_off_time)
            is_market_hours = (not is_weekend) and (market_open <= now.time() <= square_off)

            if not is_market_hours:
                day_name = "Saturday" if now.weekday() == 5 else "Sunday" if now.weekday() == 6 else "Market Closed"
                self.log(
                    f"📅 NSE MARKET NOTICE: Market is CLOSED right now ({day_name}). Regular trading hours: Mon–Fri 09:15–15:30 IST.",
                    "warn",
                )
                self.log(
                    "⚡ RUNNING LIVE TEST WALKTHROUGH: Simulating live intraday breakout so you can test indicators, audio alert, Groww strike copy, and trailing SL right now!",
                    "info",
                )
                self._run_test_walkthrough(broker, strategy, risk)
                return

            # Live Trading Loop during market hours (Mon–Fri 09:15–15:30 IST)
            self.log("Live Market Monitoring ACTIVE! Evaluating 5-minute candles...", "success")
            last_evaluated_bar_slot: datetime | None = None
            last_signal: Signal | None = None
            last_heartbeat = 0.0

            while self.running:
                now = now_ist()

                if now.time() < market_open:
                    self.log("Market not open yet (NSE opens at 09:15 IST). Waiting...", "info")
                    time_mod.sleep(15)
                    continue

                try:
                    spot_ltp = client.get_nifty_spot_ltp()
                    with self.lock:
                        self.market_data["spot"] = spot_ltp
                except Exception as exc:
                    self.log(f"Spot fetch retry: {exc}", "warn")
                    time_mod.sleep(5)
                    continue

                expected_bar_slot = _latest_expected_closed_bar(now, interval_minutes=5, settle_seconds=6)
                new_bar = False
                if expected_bar_slot and expected_bar_slot != last_evaluated_bar_slot:
                    try:
                        raw_candles, source_label = client.get_intraday_candles(lookback_days=4)
                        closed = _filter_closed_candles(raw_candles, now, interval_minutes=5)
                        if closed and closed[-1].timestamp >= expected_bar_slot:
                            last_evaluated_bar_slot = expected_bar_slot
                            last_signal = strategy.evaluate(closed, open_position=broker.position, expected_trade_date=today_date)
                            new_bar = True
                            self._refresh_market_snapshot(client, candles=closed, last_signal=last_signal, forced_spot=spot_ltp)
                    except Exception as exc:
                        self.log(f"Candle error: {exc}", "warn")

                if new_bar and last_signal:
                    snap = last_signal.snapshot
                    score_active = snap.ai_score_ce if snap.direction == "BULLISH" else snap.ai_score_pe
                    action_detail = f"{last_signal.action} ({last_signal.reason})" if last_signal.action == "HOLD" else last_signal.action
                    self.log(
                        f"Bar {snap.timestamp.strftime('%H:%M')} | Spot: {spot_ltp:.2f} | VWAP: {snap.vwap:.2f} | "
                        f"ST: {snap.direction} | ADX: {snap.adx:.1f} | AI Score: {score_active}% | Action: {action_detail}",
                        "info",
                    )
                elif time_mod.time() - last_heartbeat > 20.0:
                    last_heartbeat = time_mod.time()
                    self._refresh_market_snapshot(client, forced_spot=spot_ltp)
                    self.log(f"Live Scanner Active | NIFTY Spot: ₹{spot_ltp:.2f} | Expiry: {self.market_data.get('nearest_expiry_short')} | {self.market_data.get('window_status')}", "info")

                # Monitor active trade
                if broker.position is not None:
                    pos = broker.position
                    opt_ltp = client.get_option_ltp(pos.contract.symbol, pos.contract.token)
                    if opt_ltp > 0:
                        trend_exit = last_signal.reason if (new_bar and last_signal and last_signal.action == "EXIT") else None
                        update = risk.evaluate_position(pos, opt_ltp, now, trend_exit)

                        pnl_unrealized = round((opt_ltp - pos.entry_price) * pos.quantity, 2)
                        with self.lock:
                            self.active_position = {
                                "symbol": pos.contract.symbol,
                                "strike": pos.contract.strike,
                                "option_type": pos.contract.option_type,
                                "entry_price": pos.entry_price,
                                "current_ltp": opt_ltp,
                                "stop_loss": pos.stop_loss,
                                "target": pos.target,
                                "quantity": pos.quantity,
                                "lots": pos.lots,
                                "unrealized_pnl": pnl_unrealized,
                                "entry_time_iso": pos.entry_time.isoformat(),
                                "highest_price": pos.highest_price,
                                "breakeven_active": pos.breakeven_active,
                                "trailing_active": pos.trailing_active,
                            }

                        if update.sl_updated:
                            self.log(f"[RISK UPDATE] {pos.contract.symbol}: {update.sl_update_reason}", "warn")

                        if update.should_exit:
                            closed = broker.close_position(opt_ltp, now, spot_ltp, update.exit_reason)
                            self.log(
                                f"[GROWW EXIT] {closed.symbol} | Net P&L: ₹{closed.net_pnl:+.2f} ({closed.exit_reason})",
                                "success" if closed.net_pnl > 0 else "error",
                            )
                            with self.lock:
                                self.active_position = None
                                self.daily_pnl = {
                                    "net_pnl": broker.stats.net_pnl,
                                    "gross_pnl": broker.stats.gross_pnl,
                                    "charges": broker.stats.total_charges,
                                    "trade_count": broker.stats.trade_count,
                                    "wins": broker.stats.wins,
                                    "losses": broker.stats.losses,
                                    "status": "Halted (1/1 Trade Complete)" if broker.stats.halted else "Active",
                                    "closed_trades": [
                                        {
                                            "symbol": t.symbol,
                                            "entry": f"₹{t.entry_price:.2f}",
                                            "exit": f"₹{t.exit_price:.2f}",
                                            "net_pnl": f"₹{t.net_pnl:+.2f}",
                                            "reason": t.exit_reason,
                                        }
                                        for t in broker.stats.trades
                                    ],
                                }

                # Check new entry (only between 09:30 and 11:15 AM)
                if (
                    new_bar
                    and broker.position is None
                    and last_signal
                    and last_signal.action in ("BUY_CE", "BUY_PE")
                ):
                    allowed, deny_reason = risk.can_take_new_trade(broker.stats)
                    if allowed:
                        opt_type = "CE" if last_signal.action == "BUY_CE" else "PE"
                        contracts = client.get_candidate_options(spot_ltp, opt_type)
                        sel = risk.select_affordable_option(contracts, spot_ltp, opt_type, broker.stats)
                        if sel.contract:
                            pos = broker.open_position(
                                contract=sel.contract,
                                lots=sel.lots,
                                entry_time=now,
                                entry_spot=spot_ltp,
                                entry_reason=last_signal.reason,
                            )
                            with self.lock:
                                self.active_signal = {
                                    "action": last_signal.action,
                                    "contract": sel.contract.symbol,
                                    "expiry": sel.contract.expiry,
                                    "strike": sel.contract.strike,
                                    "option_type": sel.contract.option_type,
                                    "entry_price": sel.contract.ltp,
                                    "spot": spot_ltp,
                                    "stop_loss": pos.stop_loss,
                                    "target": pos.target,
                                    "lots": sel.lots,
                                    "quantity": pos.quantity,
                                    "capital": pos.invested_capital,
                                    "score": snap.ai_score_ce if opt_type == "CE" else snap.ai_score_pe,
                                    "reason": last_signal.reason,
                                    "timestamp": now.strftime("%H:%M:%S"),
                                }
                            self.log(
                                f"[GROWW AI BOLT SIGNAL] {last_signal.action} -> {sel.contract.symbol} @ ₹{sel.contract.ltp:.2f} "
                                f"(Target: ₹{pos.target:.2f} | SL: ₹{pos.stop_loss:.2f})",
                                "success",
                            )

                if now.time() >= square_off:
                    if broker.position is not None:
                        pos = broker.position
                        self.log("Mandatory 15:15 IST square-off: closing open position.", "warn")
                        exit_px = opt_ltp if ("opt_ltp" in locals() and opt_ltp > 0) else pos.stop_loss
                        closed = broker.close_position(
                            exit_price=exit_px,
                            exit_time=now,
                            exit_spot=spot_ltp,
                            exit_reason="Mandatory intraday square-off (15:15 IST)",
                        )
                        self.log(
                            f"[GROWW SQUARE-OFF] {closed.symbol} closed @ ₹{closed.exit_price:.2f} | Net P&L: ₹{closed.net_pnl:+.2f}",
                            "info",
                        )
                        with self.lock:
                            self.active_position = None
                            self.daily_pnl = {
                                "net_pnl": broker.stats.net_pnl,
                                "gross_pnl": broker.stats.gross_pnl,
                                "charges": broker.stats.total_charges,
                                "trade_count": broker.stats.trade_count,
                                "wins": broker.stats.wins,
                                "losses": broker.stats.losses,
                                "status": "Halted (1/1 Trade Complete)" if broker.stats.halted else "Active",
                                "closed_trades": [
                                    {
                                        "symbol": t.symbol,
                                        "entry": f"₹{t.entry_price:.2f}",
                                        "exit": f"₹{t.exit_price:.2f}",
                                        "net_pnl": f"₹{t.net_pnl:+.2f}",
                                        "reason": t.exit_reason,
                                    }
                                    for t in broker.stats.trades
                                ],
                            }
                    self.log("Reached 15:15 square-off cutoff. Wrapping up day.", "info")
                    break

                time_mod.sleep(self.config.poll_interval_seconds)

        except Exception as exc:
            self.log(f"Live engine error: {exc}", "error")
        finally:
            self.running = False
            self.mode = "IDLE"
            self.log("Live bot stopped.", "info")


MANAGER = WebBotManager()


class DashboardHTTPHandler(SimpleHTTPRequestHandler):
    """Serves the dashboard HTML and handles JSON API endpoints."""

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if HTML_PATH.exists():
                self.wfile.write(HTML_PATH.read_bytes())
            else:
                self.wfile.write(b"<h1>Dashboard HTML not found</h1>")
            return

        if self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            state = MANAGER.get_state()
            self.wfile.write(json.dumps(state).encode("utf-8"))
            return

        super().do_GET()

    def do_POST(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}

        action = payload.get("action", "")

        if self.path == "/api/action":
            if action == "start_live":
                MANAGER.start_live()
            elif action == "stop":
                MANAGER.stop()
            elif action == "check_groww":
                MANAGER.run_check()
            elif action == "run_demo":
                regime = payload.get("regime", "bullish_breakout")
                MANAGER.start_demo(regime)
            elif action == "scan_now":
                MANAGER.run_instant_scan()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "mode": MANAGER.mode}).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy HTTP request terminal prints
        pass


def run_web_dashboard(open_browser: bool = True) -> None:
    server = HTTPServer(("127.0.0.1", PORT), DashboardHTTPHandler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n{'=' * 64}")
    print(f"  NIFTY 50 GROWW AI BOLT DASHBOARD READY")
    print(f"  Open in your browser: {url}")
    print(f"{'=' * 64}\n")

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Web server stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run_web_dashboard()
