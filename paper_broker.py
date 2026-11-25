"""Paper-trading execution ledger and CSV trade journal."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from config import BotConfig
from models import ClosedTrade, DailyStats, OptionContract, Position
from risk_manager import RiskManager


class PaperBroker:
    """Simulates entries/exits cleanly without sending real orders to any broker."""

    def __init__(
        self,
        config: BotConfig,
        risk_manager: RiskManager,
        trade_date: str,
        write_journal: bool = True,
    ) -> None:
        self.config = config
        self.risk_manager = risk_manager
        self.write_journal = write_journal
        self.position: Position | None = None
        self.stats = DailyStats(
            trade_date=trade_date,
            starting_capital=config.capital,
        )
        self.journal_path = config.logs_dir / f"paper_trades_{trade_date}.csv"

    def open_position(
        self,
        contract: OptionContract,
        lots: int,
        entry_time: datetime,
        entry_spot: float,
        entry_reason: str,
    ) -> Position:
        if self.position is not None:
            raise RuntimeError("Cannot open a new position while another is active.")

        self.position = self.risk_manager.build_position(
            contract=contract,
            lots=lots,
            entry_time=entry_time,
            entry_spot=entry_spot,
            entry_reason=entry_reason,
        )
        return self.position

    def close_position(
        self,
        exit_price: float,
        exit_time: datetime,
        exit_spot: float,
        exit_reason: str,
    ) -> ClosedTrade:
        if self.position is None:
            raise RuntimeError("No active position to close.")

        pos = self.position
        gross_pnl = round((exit_price - pos.entry_price) * pos.quantity, 2)
        charges = self.risk_manager.estimate_round_trip_charges(
            pos.entry_price, exit_price, pos.quantity
        )
        net_pnl = round(gross_pnl - charges, 2)

        trade = ClosedTrade(
            symbol=pos.contract.symbol,
            option_type=pos.contract.option_type,
            strike=pos.contract.strike,
            lots=pos.lots,
            quantity=pos.quantity,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_spot=pos.entry_spot,
            exit_spot=exit_spot,
            gross_pnl=gross_pnl,
            estimated_charges=charges,
            net_pnl=net_pnl,
            exit_reason=exit_reason,
            entry_reason=pos.entry_reason,
        )

        self.stats.trades.append(trade)
        self.position = None

        # Immediately re-check daily loss/trade limits after close
        self.risk_manager.can_take_new_trade(self.stats)

        if self.write_journal:
            self._append_csv(trade)

        return trade

    def _append_csv(self, trade: ClosedTrade) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = self.journal_path.exists()
        with self.journal_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(
                    [
                        "date",
                        "symbol",
                        "type",
                        "strike",
                        "lots",
                        "qty",
                        "entry_time",
                        "exit_time",
                        "entry_price",
                        "exit_price",
                        "entry_spot",
                        "exit_spot",
                        "gross_pnl",
                        "est_charges",
                        "net_pnl",
                        "exit_reason",
                        "entry_reason",
                    ]
                )
            writer.writerow(
                [
                    self.stats.trade_date,
                    trade.symbol,
                    trade.option_type,
                    trade.strike,
                    trade.lots,
                    trade.quantity,
                    trade.entry_time.strftime("%H:%M:%S"),
                    trade.exit_time.strftime("%H:%M:%S"),
                    f"{trade.entry_price:.2f}",
                    f"{trade.exit_price:.2f}",
                    f"{trade.entry_spot:.2f}",
                    f"{trade.exit_spot:.2f}",
                    f"{trade.gross_pnl:.2f}",
                    f"{trade.estimated_charges:.2f}",
                    f"{trade.net_pnl:.2f}",
                    trade.exit_reason,
                    trade.entry_reason,
                ]
            )
