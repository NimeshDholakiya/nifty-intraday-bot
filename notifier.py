"""Console and optional Telegram alert notifier (stdlib only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime

from config import BotConfig
from models import ClosedTrade, DailyStats, Position


class Notifier:
    def __init__(self, config: BotConfig, silent: bool = False) -> None:
        self.config = config
        self.silent = silent
        self.telegram_enabled = bool(
            config.telegram_bot_token and config.telegram_chat_id
        )

    def info(self, message: str, timestamp: datetime | None = None) -> None:
        if self.silent:
            return
        prefix = timestamp.strftime("%H:%M:%S") if timestamp else datetime.now().strftime("%H:%M:%S")
        print(f"[{prefix}] {message}")

    def send_alert(self, title: str, lines: list[str], send_telegram: bool = True) -> None:
        body = "\n".join(lines)
        banner = f"\n{'=' * 62}\n{title}\n{'-' * 62}\n{body}\n{'=' * 62}\n"
        if not self.silent:
            print(banner)

        if send_telegram and self.telegram_enabled and not self.silent:
            self._send_telegram(f"{title}\n\n{body}")

    def notify_entry(self, pos: Position) -> None:
        direction_label = "BUY CALL (CE)" if pos.contract.option_type == "CE" else "BUY PUT (PE)"
        groww_search = f"NIFTY {pos.contract.strike} {pos.contract.option_type}"
        lines = [
            f"Time            : {pos.entry_time.strftime('%Y-%m-%d %H:%M:%S')} IST",
            f"Mode            : AI BOLT TRADE SIGNAL (Broker: {self.config.broker.upper()})",
            f"Search on GROWW : {groww_search} (Expiry: {pos.contract.expiry})",
            f"Order Action    : BUY {pos.lots} lot(s) = {pos.quantity} Qty (Intraday / Regular)",
            f"Entry Price     : ₹{pos.entry_price:.2f} (Required Capital: ₹{pos.invested_capital:.2f})",
            f"Stop-Loss (SL)  : ₹{pos.stop_loss:.2f} (-{self.config.stop_loss_pct * 100:.0f}%) -> Set SL on Groww",
            f"Target (TP)     : ₹{pos.target:.2f} (+{self.config.target_pct * 100:.0f}%) -> Book profit here",
            f"Anti-Theta Rule : Max {self.config.max_hold_minutes} mins (Exit if market stalls)",
            f"AI Confluence   : {pos.entry_reason}",
        ]
        self.send_alert(f"[{self.config.broker.upper()} AI BOLT SIGNAL] {direction_label} -> {groww_search}", lines)

    def notify_sl_update(self, pos: Position, update_reason: str, now_time: datetime) -> None:
        lines = [
            f"Time      : {now_time.strftime('%H:%M:%S')}",
            f"Contract  : {pos.contract.symbol}",
            f"Update    : {update_reason}",
            f"New SL    : ₹{pos.stop_loss:.2f} | Peak LTP: ₹{pos.highest_price:.2f}",
        ]
        self.send_alert(f"[RISK UPDATE] {pos.contract.symbol}", lines)

    def notify_exit(self, trade: ClosedTrade, stats: DailyStats) -> None:
        outcome = "PROFIT" if trade.net_pnl > 0 else "LOSS"
        lines = [
            f"Time       : {trade.exit_time.strftime('%Y-%m-%d %H:%M:%S')} IST",
            f"Action     : SQUARE OFF / EXIT POSITION ON {self.config.broker.upper()}",
            f"Contract   : {trade.symbol} ({trade.lots} lot / {trade.quantity} qty)",
            f"Entry->Exit: ₹{trade.entry_price:.2f} -> ₹{trade.exit_price:.2f}",
            f"Gross P&L  : ₹{trade.gross_pnl:+.2f}",
            f"Est Charges: ₹{trade.estimated_charges:.2f}",
            f"NET P&L    : ₹{trade.net_pnl:+.2f}",
            f"Reason     : {trade.exit_reason}",
            f"Day Total  : ₹{stats.net_pnl:+.2f} ({stats.trade_count}/{self.config.max_trades_per_day} trades)",
        ]
        self.send_alert(f"[{self.config.broker.upper()} EXIT - {outcome}] {trade.symbol}", lines)

    def notify_daily_summary(self, stats: DailyStats) -> None:
        lines = [
            f"Date           : {stats.trade_date}",
            f"Capital        : ₹{stats.starting_capital:.2f}",
            f"Trades Taken   : {stats.trade_count} (Wins: {stats.wins} | Losses: {stats.losses})",
            f"Gross P&L      : ₹{stats.gross_pnl:+.2f}",
            f"Est. Charges   : ₹{stats.total_charges:.2f}",
            f"Net P&L        : ₹{stats.net_pnl:+.2f} ({(stats.net_pnl / stats.starting_capital) * 100:+.2f}%)",
            f"Status         : {'HALTED - ' + stats.halt_reason if stats.halted else 'Session Complete'}",
        ]
        self.send_alert("[DAILY PAPER SUMMARY]", lines)

    def _send_telegram(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": self.config.telegram_chat_id,
                "text": text,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8):
                pass
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"[Telegram Warning] Failed to send alert: {exc}")
