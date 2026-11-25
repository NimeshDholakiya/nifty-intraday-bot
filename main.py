"""CLI entrypoint for the NIFTY 50 Intraday Options Signal & Paper-Trading Bot."""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time as time_mod
from datetime import date, datetime, time, timedelta
from pathlib import Path

from angel_client import AngelOneClient, now_ist
from config import BotConfig
from models import OptionType
from notifier import Notifier
from paper_broker import PaperBroker
from risk_manager import RiskManager
from simulator import generate_intraday_session, replay_session, run_backtest
from strategy import ORBVWAPSupertrendStrategy

# Ensure UTF-8 printing (for ₹ symbol) works smoothly on Windows PowerShell/CMD
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _parse_hhmm(hhmm: str) -> time:
    h, m = hhmm.strip().split(":")
    return time(int(h), int(m))


def _latest_expected_closed_bar(now_dt: datetime, interval_minutes: int = 5, settle_seconds: int = 6) -> datetime | None:
    """Return the timestamp of the most recent 5-min candle that should be fully closed.

    For example, at 09:20:06 IST, the 09:15 bar has closed and settled on Angel One servers.
    Before 09:20:06 IST, no intraday bar for today has closed yet -> returns None.
    """
    market_open = now_dt.replace(hour=9, minute=15, second=0, microsecond=0)
    elapsed_sec = (now_dt - market_open).total_seconds() - settle_seconds
    if elapsed_sec < interval_minutes * 60:
        return None
    closed_bars = int(elapsed_sec // (interval_minutes * 60))
    last_bar_start_offset_min = (closed_bars - 1) * interval_minutes
    return market_open + timedelta(minutes=last_bar_start_offset_min)


def run_demo(config: BotConfig, regime: str = "bullish_breakout") -> None:
    notifier = Notifier(config)
    notifier.info(
        f"Running OFFLINE DEMO ({regime}) | Capital=₹{config.capital:.0f} | "
        f"Lot Size={config.lot_size} | Max Affordable 1-Lot LTP=₹{config.max_premium_for_one_lot:.2f}"
    )
    candles = generate_intraday_session(
        session_date=date.today(),
        regime=regime,
        start_price=24500.0,
        seed=42,
    )
    replay_session(candles=candles, config=config, notifier=notifier, write_journal=True)
    notifier.info(f"Paper trade journal saved to: {config.logs_dir}")


def _print_backtest_block(title: str, config: BotConfig, report, source: str) -> None:
    win_rate = (report.wins / report.total_trades * 100.0) if report.total_trades else 0.0
    filters_desc = (
        f"ADX>={config.min_adx:.0f}" if config.enable_adx_filter else "ADX=OFF"
    ) + " | " + (
        f"{config.htf_minutes}m-HTF=ON" if config.enable_htf_filter else "HTF=OFF"
    ) + " | " + (
        f"Vol>={config.min_volume_ratio:.2f}x" if config.enable_volume_filter else "Vol=OFF"
    ) + " | " + (
        f"ORBRange<={config.max_orb_range_pct * 100:.2f}%"
        if config.enable_orb_range_filter
        else "ORBRange=OFF"
    )

    print("\n" + "=" * 68)
    print(title)
    print("-" * 68)
    print(f"Data Source       : {source}")
    print(f"Active Filters    : {filters_desc}")
    print(f"Capital / LotSize : ₹{config.capital:.2f} / {config.lot_size} qty")
    print(f"Risk Rules        : Max {config.max_trades_per_day} trades/day | Max Loss ₹{config.max_daily_loss:.0f}/day")
    print(f"Days Tested       : {report.days_tested}")
    print(f"Total Trades      : {report.total_trades} (Wins: {report.wins} | Losses: {report.losses})")
    print(f"Win Rate          : {win_rate:.1f}%")
    print(f"Gross P&L         : ₹{report.gross_pnl:+.2f}")
    print(f"Est. Charges      : ₹{report.total_charges:.2f}")
    print(f"NET P&L           : ₹{report.net_pnl:+.2f}")
    print(f"Max Drawdown      : ₹{report.max_drawdown:.2f}")
    print(f"Best / Worst Day  : ₹{report.best_day:+.2f} / ₹{report.worst_day:+.2f}")
    print("=" * 68)


def run_backtest_cli(
    config: BotConfig,
    days: int,
    csv_path: Path | None,
    compare_filters: bool = False,
) -> None:
    source = str(csv_path) if csv_path else f"{days} simulated mixed market sessions"
    enhanced = run_backtest(config=config, days=days, csv_path=csv_path)

    if compare_filters:
        base_cfg = dataclasses.replace(
            config,
            enable_adx_filter=False,
            enable_htf_filter=False,
            enable_volume_filter=False,
            enable_orb_range_filter=False,
        )
        base = run_backtest(config=base_cfg, days=days, csv_path=csv_path)
        _print_backtest_block("1) BASE STRATEGY (ORB + VWAP + Supertrend ONLY)", base_cfg, base, source)
        _print_backtest_block("2) ENHANCED FILTERS (+ ADX + 15m HTF + Volume + ORB Range)", config, enhanced, source)
        print()
    else:
        _print_backtest_block("NIFTY 50 ORB + VWAP + SUPERTREND BACKTEST REPORT (PAPER)", config, enhanced, source)
        print()


def run_fetch_history(config: BotConfig, days: int, output_path: Path | None) -> None:
    notifier = Notifier(config)
    client = AngelOneClient(config)
    target_csv = output_path or (config.data_dir / f"nifty_5m_{days}d.csv")
    try:
        notifier.info("Logging into Angel One SmartAPI to download real NSE 5-min candles...")
        client.login()
        candles, source_label = client.fetch_historical_candles_chunked(days=days)
        if not candles:
            notifier.info("No historical candles returned by Angel One API.")
            return
        saved = client.save_candles_to_csv(candles, target_csv)
        notifier.info(
            f"Downloaded {len(candles)} candles from {source_label} -> {saved}"
        )
        notifier.info(
            f"Now run a real-data backtest:\n  python main.py --mode backtest --csv \"{saved}\" --compare-filters"
        )
    finally:
        client.logout()


def get_market_client(config: BotConfig):
    """Return live market feed: Angel One if credentials exist, otherwise Free Public Feed."""
    if config.angel_api_key and config.angel_client_code and config.angel_totp_secret:
        from angel_client import AngelOneClient
        return AngelOneClient(config)
    from free_feed_client import FreeMarketDataClient
    return FreeMarketDataClient(config)


def _filter_closed_candles(candles: list, now_dt: datetime, interval_minutes: int = 5) -> list:
    """Keep only fully closed candles (exclude the currently forming bar)."""
    if not candles:
        return candles
    latest = candles[-1]
    candle_end = latest.timestamp + timedelta(minutes=interval_minutes)
    if now_dt < candle_end:
        return candles[:-1]
    return candles


def run_check_feed(config: BotConfig) -> None:
    notifier = Notifier(config)
    client = get_market_client(config)
    feed_name = "Angel One SmartAPI" if hasattr(client, "load_scrip_master") else "Free Public NSE Feed (Groww Option 1)"
    try:
        notifier.info(f"[1/5] Initializing live market feed: {feed_name}...")
        client.login()
        notifier.info("      Connection OK!")

        notifier.info("[2/5] Warming up market instruments & weekly expiry...")
        meta = client.warmup_instruments()
        notifier.info(
            f"      Target Broker: {config.broker.upper()} | Expiry: {meta.get('nearest_option_expiry')} | "
            f"Lot Size: {config.lot_size}"
        )

        notifier.info("[3/5] Fetching live NIFTY 50 Spot price...")
        spot = client.get_nifty_spot_ltp()
        notifier.info(f"      NIFTY 50 Spot LTP: {spot:.2f}")

        notifier.info("[4/5] Fetching 5-min candles & computing AI Confluence Score...")
        candles, source_label = client.get_intraday_candles(lookback_days=4)
        from indicators import build_latest_snapshot

        snap = build_latest_snapshot(
            candles,
            orb_start=config.orb_start,
            orb_end=config.orb_end,
            supertrend_period=config.supertrend_period,
            supertrend_multiplier=config.supertrend_multiplier,
            adx_period=config.adx_period,
            htf_minutes=config.htf_minutes,
            volume_lookback=config.volume_lookback,
        )
        if snap:
            notifier.info(
                f"      Source={source_label} ({len(candles)} bars) | Spot={snap.close:.2f} | "
                f"VWAP={snap.vwap:.2f} | ST={snap.supertrend:.2f} ({snap.direction}) | "
                f"15m={snap.htf_trend} | ADX={snap.adx:.1f} | "
                f"AI Score: CE={snap.ai_score_ce}/100, PE={snap.ai_score_pe}/100"
            )

        notifier.info(f"[5/5] Scanning affordable option strikes on Groww for ₹{config.capital:.0f} budget...")
        risk = RiskManager(config)
        broker = PaperBroker(config, risk, trade_date=now_ist().date().isoformat(), write_journal=False)
        for opt_type in ("CE", "PE"):
            contracts = client.get_candidate_options(spot_price=spot, option_type=opt_type)  # type: ignore[arg-type]
            sel = risk.select_affordable_option(
                contracts=contracts,
                spot_price=spot,
                option_type=opt_type,  # type: ignore[arg-type]
                stats=broker.stats,
            )
            notifier.info(f"      Best Groww {opt_type}: {sel.reason}")

        notifier.info(f"ALL CHECKS PASSED! Ready to send live {config.broker.upper()} AI Bolt trade alerts on Monday.")
    finally:
        client.logout()


def run_live_paper_bot(config: BotConfig) -> None:
    notifier = Notifier(config)
    now = now_ist()
    today_date = now.date()

    # Guard against accidental weekend launch
    if now.weekday() >= 5:
        notifier.info(
            f"Today is {now.strftime('%A')} (NSE markets are closed on weekends). "
            "Use --mode demo or --mode backtest when markets are closed."
        )
        return

    square_off = _parse_hhmm(config.square_off_time)
    if now.time() >= square_off:
        notifier.info(
            f"Current IST time is {now.strftime('%H:%M:%S')} (past {config.square_off_time} square-off). "
            "Launch between 08:45 AM and 02:30 PM IST on a trading day."
        )
        return

    client = get_market_client(config)
    strategy = ORBVWAPSupertrendStrategy(config)
    risk = RiskManager(config)
    broker = PaperBroker(
        config=config,
        risk_manager=risk,
        trade_date=today_date.isoformat(),
        write_journal=True,
    )

    market_open = _parse_hhmm("09:15")
    last_evaluated_bar_slot: datetime | None = None
    last_signal = None
    last_logged_bar: datetime | None = None
    source_label = "NIFTY"
    consecutive_errors = 0

    try:
        notifier.info("Logging into Angel One SmartAPI & pre-indexing NSE/NFO contracts...")
        client.login()
        meta = client.warmup_instruments()
        if meta["detected_lot_size"] and meta["detected_lot_size"] != config.lot_size:
            notifier.info(
                f"Auto-synced NIFTY lot size to NSE live value: {meta['detected_lot_size']}"
            )
            config.lot_size = meta["detected_lot_size"]

        notifier.send_alert(
            "[BOT STARTED] NIFTY 50 Intraday Signal & Paper-Trading Bot",
            [
                f"Date          : {today_date.isoformat()} (IST)",
                f"Mode          : PAPER TRADING + LIVE SIGNALS (Read-Only Angel One SmartAPI)",
                f"Instrument    : {meta['nearest_future'] or meta['spot_symbol']} | Expiry {meta['nearest_option_expiry']}",
                f"Strategy      : ORB ({config.orb_start}-{config.orb_end}) + VWAP + Supertrend + ADX>={config.min_adx:.0f} + 15m HTF + Vol>={config.min_volume_ratio:.2f}x",
                f"Capital       : ₹{config.capital:.2f} | Lot Size: {config.lot_size} | Max 1-Lot LTP: ₹{config.max_premium_for_one_lot:.2f}",
                f"Risk Limits   : Max {config.max_trades_per_day} trades/day | Max Loss ₹{config.max_daily_loss:.0f} | Square-off {config.square_off_time} IST",
                f"Telegram      : {'ENABLED' if notifier.telegram_enabled else 'Disabled (console only)'}",
            ],
        )

        while True:
            now = now_ist()

            # Wait cleanly if launched before 09:15 AM IST
            if now.time() < market_open:
                secs_to_open = int(
                    (now.replace(hour=9, minute=15, second=0, microsecond=0) - now).total_seconds()
                )
                notifier.info(
                    f"Waiting for NSE open (09:15 IST) — {max(secs_to_open, 0)}s remaining...",
                    now,
                )
                time_mod.sleep(min(30, max(secs_to_open, 1)))
                continue

            # Lightweight live spot price on every poll cycle
            try:
                spot_ltp = client.get_nifty_spot_ltp()
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                notifier.info(f"Temporary spot fetch error (#{consecutive_errors}): {exc}", now)
                time_mod.sleep(min(30, 5 * consecutive_errors))
                continue

            # Fetch 5-min historical candles ONLY when a new 5-min bar has closed & settled
            expected_bar_slot = _latest_expected_closed_bar(now, interval_minutes=5, settle_seconds=6)
            new_bar_evaluated = False
            if expected_bar_slot is not None and expected_bar_slot != last_evaluated_bar_slot:
                try:
                    raw_candles, source_label = client.get_intraday_candles(lookback_days=4)
                    closed_candles = _filter_closed_candles(raw_candles, now, interval_minutes=5)
                    if closed_candles and closed_candles[-1].timestamp >= expected_bar_slot:
                        last_evaluated_bar_slot = expected_bar_slot
                        last_signal = strategy.evaluate(
                            closed_candles,
                            open_position=broker.position,
                            expected_trade_date=today_date,
                        )
                        new_bar_evaluated = True
                    else:
                        notifier.info(
                            f"Waiting for Angel One to publish {expected_bar_slot.strftime('%H:%M')} bar...",
                            now,
                        )
                except Exception as exc:
                    notifier.info(f"Candle fetch retry next poll: {exc}", now)

            # Log indicator status once per closed 5-min bar
            if new_bar_evaluated and last_signal and last_signal.timestamp != last_logged_bar:
                last_logged_bar = last_signal.timestamp
                snap = last_signal.snapshot
                orb_txt = (
                    f"ORH={snap.orb_high:.1f}/ORL={snap.orb_low:.1f}"
                    if snap.orb_high is not None and snap.orb_low is not None
                    else "ORB=forming"
                )
                vol_txt = f"{snap.volume_ratio:.2f}x" if snap.volume_ratio is not None else "N/A"
                notifier.info(
                    f"[{source_label}] Bar {snap.timestamp.strftime('%H:%M')} | "
                    f"Close={snap.close:.2f} | Spot={spot_ltp:.2f} | {orb_txt} | "
                    f"VWAP={snap.vwap:.2f} | ST={snap.supertrend:.2f} ({snap.direction}) | "
                    f"15m={snap.htf_trend} | ADX={snap.adx:.1f} | Vol={vol_txt} | "
                    f"Signal={last_signal.action} ({last_signal.reason})",
                    now,
                )

            # 1. Monitor active paper position on EVERY poll cycle (15s live SL/Trailing SL/Target)
            if broker.position is not None:
                pos = broker.position
                try:
                    opt_ltp = client.get_option_ltp(pos.contract.symbol, pos.contract.token)
                    if opt_ltp > 0:
                        trend_exit = (
                            last_signal.reason
                            if (new_bar_evaluated and last_signal and last_signal.action == "EXIT")
                            else None
                        )
                        update = risk.evaluate_position(
                            position=pos,
                            current_ltp=opt_ltp,
                            now_time=now,
                            trend_exit_reason=trend_exit,
                        )
                        if update.sl_updated:
                            notifier.notify_sl_update(pos, update.sl_update_reason, now)
                        if update.should_exit:
                            closed = broker.close_position(
                                exit_price=opt_ltp,
                                exit_time=now,
                                exit_spot=spot_ltp,
                                exit_reason=update.exit_reason,
                            )
                            if "Stop-Loss" in update.exit_reason and last_signal:
                                strategy.register_stop_out(closed.option_type, last_signal.timestamp)
                            notifier.notify_exit(closed, broker.stats)
                    else:
                        notifier.info(
                            f"Warning: could not fetch LTP for {pos.contract.symbol}, retrying...",
                            now,
                        )
                except Exception as exc:
                    notifier.info(f"Position monitoring error (will retry): {exc}", now)

            # 2. If past square-off time (15:15 IST), force close any open position and exit cleanly
            if now.time() >= square_off:
                if broker.position is not None:
                    pos = broker.position
                    notifier.info("Mandatory 15:15 IST square-off: closing open position.", now)
                    exit_px = opt_ltp if ("opt_ltp" in locals() and opt_ltp > 0) else pos.stop_loss
                    closed = broker.close_position(
                        exit_price=exit_px,
                        exit_time=now,
                        exit_spot=spot_ltp,
                        exit_reason="Mandatory intraday square-off (15:15 IST)",
                    )
                    notifier.notify_exit(closed, broker.stats)
                notifier.info("Intraday session reached square-off cutoff. Wrapping up.", now)
                break

            # 3. Check for new entry signal ONLY on a freshly closed 5-min bar when flat
            if (
                new_bar_evaluated
                and broker.position is None
                and last_signal
                and last_signal.action in ("BUY_CE", "BUY_PE")
            ):
                allowed, deny_reason = risk.can_take_new_trade(broker.stats)
                if not allowed:
                    notifier.info(f"Signal {last_signal.action} blocked: {deny_reason}", now)
                else:
                    try:
                        opt_type: OptionType = "CE" if last_signal.action == "BUY_CE" else "PE"
                        contracts = client.get_candidate_options(
                            spot_price=spot_ltp,
                            option_type=opt_type,
                        )
                        selection = risk.select_affordable_option(
                            contracts=contracts,
                            spot_price=spot_ltp,
                            option_type=opt_type,
                            stats=broker.stats,
                        )
                        if selection.contract is None:
                            notifier.info(
                                f"Signal {last_signal.action} skipped: {selection.reason}",
                                now,
                            )
                        else:
                            pos = broker.open_position(
                                contract=selection.contract,
                                lots=selection.lots,
                                entry_time=now,
                                entry_spot=spot_ltp,
                                entry_reason=last_signal.reason,
                            )
                            notifier.notify_entry(pos)
                    except Exception as exc:
                        notifier.info(f"Option chain scan failed (trade skipped safely): {exc}", now)

            # Stop polling early if daily risk halt triggered and no open position
            if broker.stats.halted and broker.position is None:
                notifier.info(f"Stopping session early: {broker.stats.halt_reason}", now)
                break

            time_mod.sleep(config.poll_interval_seconds)

    except KeyboardInterrupt:
        notifier.info("Stopped by user (Ctrl+C).")
        if broker.position is not None:
            pos = broker.position
            notifier.info(
                f"REMINDER: Active paper position still open: {pos.contract.symbol} "
                f"({pos.lots} lot / Entry ₹{pos.entry_price:.2f} / SL ₹{pos.stop_loss:.2f})"
            )
    finally:
        notifier.notify_daily_summary(broker.stats)
        client.logout()
        feed_name = "Angel One SmartAPI" if hasattr(client, "load_scrip_master") else "Free Market Data Feed"
        notifier.info(f"Logged out cleanly from {feed_name}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="NIFTY 50 Intraday Options Signal & Paper-Trading Bot (Groww / Angel One)"
    )
    parser.add_argument(
        "--mode",
        choices=["dashboard", "web", "demo", "backtest", "check-groww", "check-angel", "fetch-history", "live"],
        default="dashboard",
        help="Execution mode (default: dashboard)",
    )
    parser.add_argument(
        "--regime",
        choices=["bullish_breakout", "bearish_breakdown", "choppy", "random"],
        default="bullish_breakout",
        help="Simulated market regime for --mode demo",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days for --mode backtest or --mode fetch-history (default: 30)",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Optional historical 5-min CSV file (timestamp,open,high,low,close,volume) for backtest",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output CSV path for --mode fetch-history",
    )
    parser.add_argument(
        "--compare-filters",
        action="store_true",
        help="Compare Base strategy vs ADX + 15m HTF + Volume + ORB-range filters in backtest",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        help="Override starting capital in INR (default from .env or 5000)",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config = BotConfig.from_env()
    if args.capital is not None:
        config.capital = args.capital

    if args.mode in ("dashboard", "web"):
        from web_server import run_web_dashboard
        run_web_dashboard(open_browser=True)
    elif args.mode == "demo":
        run_demo(config, regime=args.regime)
    elif args.mode == "backtest":
        run_backtest_cli(
            config,
            days=args.days,
            csv_path=args.csv,
            compare_filters=args.compare_filters,
        )
    elif args.mode in ("check-groww", "check-angel"):
        run_check_feed(config)
    elif args.mode == "fetch-history":
        run_fetch_history(config, days=args.days, output_path=args.output)
    elif args.mode == "live":
        run_live_paper_bot(config)


if __name__ == "__main__":
    main()
