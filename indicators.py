"""Technical indicators: ORB, VWAP, ATR, Supertrend, ADX, EMA, Volume Ratio & 15m HTF."""

from __future__ import annotations

from datetime import datetime, time

from models import Candle, HTFTrend, IndicatorSnapshot, OpeningRange, OptionType, TrendDirection


def _parse_hhmm(hhmm: str) -> time:
    hour_str, min_str = hhmm.strip().split(":")
    return time(int(hour_str), int(min_str))


def compute_opening_range(
    candles: list[Candle],
    orb_start: str = "09:15",
    orb_end: str = "09:30",
) -> OpeningRange:
    """Compute Opening Range High/Low from intraday candles in [orb_start, orb_end).

    For 5-minute candles, 09:15, 09:20, and 09:25 candles represent 09:15-09:30.
    The range is considered complete once a candle with timestamp >= orb_end exists.
    """
    if not candles:
        return OpeningRange(high=0.0, low=0.0, complete=False, candle_count=0)

    start_t = _parse_hhmm(orb_start)
    end_t = _parse_hhmm(orb_end)
    latest_date = candles[-1].timestamp.date()

    session_candles = [c for c in candles if c.timestamp.date() == latest_date]
    orb_candles = [
        c for c in session_candles if start_t <= c.timestamp.time() < end_t
    ]

    if not orb_candles:
        return OpeningRange(high=0.0, low=0.0, complete=False, candle_count=0)

    orb_high = max(c.high for c in orb_candles)
    orb_low = min(c.low for c in orb_candles)
    has_post_orb_candle = any(c.timestamp.time() >= end_t for c in session_candles)

    return OpeningRange(
        high=orb_high,
        low=orb_low,
        complete=has_post_orb_candle,
        candle_count=len(orb_candles),
    )


def compute_vwap(candles: list[Candle]) -> tuple[list[float], str]:
    """Compute intraday VWAP (resets daily).

    If volume data is present (e.g., NIFTY Futures), computes true volume-weighted VWAP.
    If volume is zero across the session (e.g., NIFTY 50 Spot index), falls back to
    cumulative typical-price average (TWAP proxy) so the indicator remains functional.
    """
    if not candles:
        return [], "VOLUME_WEIGHTED"

    latest_date = candles[-1].timestamp.date()
    latest_session_has_volume = any(
        c.volume > 0 for c in candles if c.timestamp.date() == latest_date
    )
    mode = "VOLUME_WEIGHTED" if latest_session_has_volume else "TWAP_FALLBACK"

    vwap_values: list[float] = []
    current_day = None
    cum_pv = 0.0
    cum_vol = 0.0
    cum_tp = 0.0
    count = 0

    for c in candles:
        day = c.timestamp.date()
        if day != current_day:
            current_day = day
            cum_pv = 0.0
            cum_vol = 0.0
            cum_tp = 0.0
            count = 0

        tp = c.typical_price
        cum_tp += tp
        count += 1

        if c.volume > 0:
            cum_pv += tp * c.volume
            cum_vol += c.volume

        if cum_vol > 0:
            vwap_values.append(cum_pv / cum_vol)
        else:
            vwap_values.append(cum_tp / count)

    return vwap_values, mode


def compute_ema(values: list[float], period: int) -> list[float]:
    """Compute Exponential Moving Average (EMA)."""
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    ema: list[float] = [values[0]]
    for val in values[1:]:
        ema.append(val * k + ema[-1] * (1.0 - k))
    return ema


def compute_atr(candles: list[Candle], period: int = 10) -> list[float]:
    """Compute Wilder's Average True Range (ATR)."""
    if not candles:
        return []

    true_ranges: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            tr = c.high - c.low
        else:
            prev_close = candles[i - 1].close
            tr = max(
                c.high - c.low,
                abs(c.high - prev_close),
                abs(c.low - prev_close),
            )
        true_ranges.append(tr)

    atr_values: list[float] = []
    for i, tr in enumerate(true_ranges):
        if i == 0:
            atr_values.append(tr)
        elif i < period:
            atr_values.append(sum(true_ranges[: i + 1]) / (i + 1))
        elif i == period:
            atr_values.append(sum(true_ranges[1 : period + 1]) / period)
        else:
            prev_atr = atr_values[-1]
            atr_values.append(((prev_atr * (period - 1)) + tr) / period)

    return atr_values


def compute_supertrend(
    candles: list[Candle],
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[list[float], list[TrendDirection], list[float]]:
    """Compute TradingView-aligned Supertrend values, directions, and ATR."""
    if not candles:
        return [], [], []

    atr_values = compute_atr(candles, period=period)
    final_upper: list[float] = []
    final_lower: list[float] = []
    supertrend: list[float] = []
    directions: list[TrendDirection] = []

    for i, c in enumerate(candles):
        hl2 = (c.high + c.low) / 2.0
        basic_upper = hl2 + multiplier * atr_values[i]
        basic_lower = hl2 - multiplier * atr_values[i]

        if i == 0:
            fu = basic_upper
            fl = basic_lower
            direction: TrendDirection = "BULLISH" if c.close >= hl2 else "BEARISH"
            st = fl if direction == "BULLISH" else fu
        else:
            prev_fu = final_upper[i - 1]
            prev_fl = final_lower[i - 1]
            prev_close = candles[i - 1].close
            prev_dir = directions[i - 1]

            fu = basic_upper if (basic_upper < prev_fu or prev_close > prev_fu) else prev_fu
            fl = basic_lower if (basic_lower > prev_fl or prev_close < prev_fl) else prev_fl

            if prev_dir == "BEARISH" and c.close > prev_fu:
                direction = "BULLISH"
            elif prev_dir == "BULLISH" and c.close < prev_fl:
                direction = "BEARISH"
            else:
                direction = prev_dir

            st = fl if direction == "BULLISH" else fu

        final_upper.append(fu)
        final_lower.append(fl)
        supertrend.append(st)
        directions.append(direction)

    return supertrend, directions, atr_values


def compute_adx(
    candles: list[Candle],
    period: int = 14,
) -> tuple[list[float], list[float], list[float]]:
    """Compute Wilder's ADX, +DI, and -DI to detect trending vs sideways markets.

    High ADX (>= 20-25) indicates directional trend strength; low ADX (< 20) indicates
    a choppy, sideways market where option buyers suffer theta decay and whipsaws.
    """
    n = len(candles)
    if n == 0:
        return [], [], []
    if n == 1:
        return [0.0], [0.0], [0.0]

    tr_list: list[float] = [candles[0].high - candles[0].low]
    plus_dm_list: list[float] = [0.0]
    minus_dm_list: list[float] = [0.0]

    for i in range(1, n):
        cur = candles[i]
        prev = candles[i - 1]
        up_move = cur.high - prev.high
        down_move = prev.low - cur.low

        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr = max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))

        tr_list.append(tr)
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    adx_vals: list[float] = [0.0]
    plus_di_vals: list[float] = [0.0]
    minus_di_vals: list[float] = [0.0]

    # Wilder's smoothed sums (running warmup for early bars so early-session signals work)
    smooth_tr = 0.0
    smooth_plus = 0.0
    smooth_minus = 0.0
    dx_history: list[float] = []

    for i in range(1, n):
        if i <= period:
            smooth_tr += tr_list[i]
            smooth_plus += plus_dm_list[i]
            smooth_minus += minus_dm_list[i]
        else:
            smooth_tr = smooth_tr - (smooth_tr / period) + tr_list[i]
            smooth_plus = smooth_plus - (smooth_plus / period) + plus_dm_list[i]
            smooth_minus = smooth_minus - (smooth_minus / period) + minus_dm_list[i]

        pdi = (100.0 * smooth_plus / smooth_tr) if smooth_tr > 0 else 0.0
        mdi = (100.0 * smooth_minus / smooth_tr) if smooth_tr > 0 else 0.0
        di_sum = pdi + mdi
        dx = (100.0 * abs(pdi - mdi) / di_sum) if di_sum > 0 else 0.0
        dx_history.append(dx)

        if len(dx_history) <= period:
            adx = sum(dx_history) / len(dx_history)
        else:
            adx = ((adx_vals[-1] * (period - 1)) + dx) / period

        plus_di_vals.append(round(pdi, 2))
        minus_di_vals.append(round(mdi, 2))
        adx_vals.append(round(adx, 2))

    return adx_vals, plus_di_vals, minus_di_vals


def resample_candles(candles: list[Candle], minutes: int = 15) -> list[Candle]:
    """Resample 5-min candles into higher-timeframe buckets aligned to NSE 09:15 open."""
    if not candles:
        return []

    buckets: list[Candle] = []
    current_key: tuple | None = None
    b_ts: datetime | None = None
    b_open = b_high = b_low = b_close = b_vol = 0.0

    for c in candles:
        # Align buckets to 09:15 AM (555 minutes from midnight)
        mins_from_midnight = c.timestamp.hour * 60 + c.timestamp.minute
        offset = max(mins_from_midnight - 555, 0)
        bucket_idx = offset // minutes
        bucket_start_mins = 555 + bucket_idx * minutes
        key = (c.timestamp.date(), bucket_idx)

        if key != current_key:
            if current_key is not None and b_ts is not None:
                buckets.append(
                    Candle(
                        timestamp=b_ts,
                        open=b_open,
                        high=b_high,
                        low=b_low,
                        close=b_close,
                        volume=b_vol,
                    )
                )
            current_key = key
            b_ts = datetime(
                c.timestamp.year,
                c.timestamp.month,
                c.timestamp.day,
                bucket_start_mins // 60,
                bucket_start_mins % 60,
            )
            b_open = c.open
            b_high = c.high
            b_low = c.low
            b_close = c.close
            b_vol = c.volume
        else:
            b_high = max(b_high, c.high)
            b_low = min(b_low, c.low)
            b_close = c.close
            b_vol += c.volume

    if current_key is not None and b_ts is not None:
        buckets.append(
            Candle(
                timestamp=b_ts,
                open=b_open,
                high=b_high,
                low=b_low,
                close=b_close,
                volume=b_vol,
            )
        )

    return buckets


def compute_htf_trend(
    candles: list[Candle],
    htf_minutes: int = 15,
    supertrend_period: int = 7,
    supertrend_multiplier: float = 2.5,
) -> HTFTrend:
    """Determine Higher-Timeframe (15-min) trend using 15m Supertrend + EMA(9).

    Uses 15m Supertrend direction confirmed by close above/below 15m EMA(9) so
    intraday trend shifts are caught promptly without 5-hour prior-day lag.
    """
    htf_candles = resample_candles(candles, minutes=htf_minutes)
    if len(htf_candles) < 2:
        return "NEUTRAL"

    closes = [c.close for c in htf_candles]
    ema_9 = compute_ema(closes, period=min(9, len(closes)))[-1]
    _, st_dirs, _ = compute_supertrend(
        htf_candles,
        period=min(supertrend_period, max(len(htf_candles) - 1, 2)),
        multiplier=supertrend_multiplier,
    )
    st_dir = st_dirs[-1]
    last_close = closes[-1]

    if st_dir == "BULLISH" and last_close >= ema_9:
        return "BULLISH"
    if st_dir == "BEARISH" and last_close <= ema_9:
        return "BEARISH"
    return "NEUTRAL"


def compute_volume_ratio(candles: list[Candle], lookback: int = 10) -> float | None:
    """Compute latest candle volume divided by average volume of prior candles.

    Returns None when the feed has no volume (e.g., NIFTY 50 Spot index).
    """
    if len(candles) < 2:
        return None
    latest_vol = candles[-1].volume
    prev_vols = [c.volume for c in candles[-(lookback + 1) : -1] if c.volume > 0]
    if latest_vol <= 0 or not prev_vols:
        return None
    avg_vol = sum(prev_vols) / len(prev_vols)
    if avg_vol <= 0:
        return None
    return round(latest_vol / avg_vol, 2)


def compute_ai_confluence_score(
    close: float,
    vwap: float,
    orb_high: float | None,
    orb_low: float | None,
    supertrend: float,
    direction: TrendDirection,
    htf_trend: HTFTrend,
    adx: float,
    plus_di: float,
    minus_di: float,
    volume_ratio: float | None,
    orb_range_pct: float,
    option_type: OptionType,
) -> tuple[int, dict[str, int]]:
    """Compute Institutional AI Confluence Score (0 to 100%) for CE or PE setups.

    Pillars:
      1. Price Action vs ORB & VWAP (25 pts)
      2. 5-min Supertrend Momentum (25 pts)
      3. 15-min Multi-Timeframe Trend Alignment (20 pts)
      4. ADX Directional Trend Strength (15 pts)
      5. Volume Expansion & Range Health (15 pts)
    """
    pts: dict[str, int] = {}

    # Pillar 1: Price Action vs ORB & VWAP (25 pts)
    if option_type == "CE":
        orb_pts = 15 if (orb_high is not None and close > orb_high) else 0
        vwap_pts = 10 if close > vwap else 0
    else:
        orb_pts = 15 if (orb_low is not None and close < orb_low) else 0
        vwap_pts = 10 if close < vwap else 0
    pts["price_action"] = orb_pts + vwap_pts

    # Pillar 2: 5m Supertrend Momentum (25 pts)
    if option_type == "CE":
        if direction == "BULLISH":
            st_pts = 25 if close >= supertrend * 1.001 else 20
        else:
            st_pts = 0
    else:
        if direction == "BEARISH":
            st_pts = 25 if close <= supertrend * 0.999 else 20
        else:
            st_pts = 0
    pts["supertrend_5m"] = st_pts

    # Pillar 3: 15-Minute Multi-Timeframe Alignment (20 pts)
    expected_htf = "BULLISH" if option_type == "CE" else "BEARISH"
    if htf_trend == expected_htf:
        htf_pts = 20
    elif htf_trend == "NEUTRAL":
        htf_pts = 10
    else:
        htf_pts = 0
    pts["htf_15m"] = htf_pts

    # Pillar 4: ADX Trend Strength & Directional Index (15 pts)
    di_valid = (plus_di > minus_di) if option_type == "CE" else (minus_di > plus_di)
    if adx >= 22.0 and di_valid:
        adx_pts = 15
    elif adx >= 18.0 and di_valid:
        adx_pts = 10
    elif di_valid:
        adx_pts = 5
    else:
        adx_pts = 0
    pts["adx_momentum"] = adx_pts

    # Pillar 5: Volume Surge & Range Health (15 pts)
    if volume_ratio is not None:
        if volume_ratio >= 1.30:
            vol_pts = 10
        elif volume_ratio >= 1.10:
            vol_pts = 7
        else:
            vol_pts = 3
    else:
        vol_pts = 7  # Neutral score if feed has no volume

    range_pts = 5 if (orb_range_pct > 0 and orb_range_pct <= 0.0050) else (3 if orb_range_pct <= 0.0075 else 0)
    pts["volume_and_range"] = vol_pts + range_pts

    total_score = sum(pts.values())
    return total_score, pts


def build_latest_snapshot(
    candles: list[Candle],
    orb_start: str = "09:15",
    orb_end: str = "09:30",
    supertrend_period: int = 10,
    supertrend_multiplier: float = 3.0,
    adx_period: int = 14,
    htf_minutes: int = 15,
    volume_lookback: int = 10,
) -> IndicatorSnapshot | None:
    """Compute all indicators and return the snapshot for the latest closed candle."""
    if not candles:
        return None

    vwap_values, vwap_mode = compute_vwap(candles)
    st_values, st_dirs, atr_values = compute_supertrend(
        candles, period=supertrend_period, multiplier=supertrend_multiplier
    )
    orb = compute_opening_range(candles, orb_start=orb_start, orb_end=orb_end)
    adx_vals, pdi_vals, mdi_vals = compute_adx(candles, period=adx_period)
    htf_trend = compute_htf_trend(candles, htf_minutes=htf_minutes)
    vol_ratio = compute_volume_ratio(candles, lookback=volume_lookback)
    latest = candles[-1]

    orb_range_pct = (
        (orb.high - orb.low) / orb.low
        if (orb.candle_count > 0 and orb.low > 0)
        else 0.0
    )

    orb_high = orb.high if orb.candle_count > 0 else None
    orb_low = orb.low if orb.candle_count > 0 else None

    score_ce, bd_ce = compute_ai_confluence_score(
        close=latest.close,
        vwap=vwap_values[-1],
        orb_high=orb_high,
        orb_low=orb_low,
        supertrend=st_values[-1],
        direction=st_dirs[-1],
        htf_trend=htf_trend,
        adx=adx_vals[-1],
        plus_di=pdi_vals[-1],
        minus_di=mdi_vals[-1],
        volume_ratio=vol_ratio,
        orb_range_pct=orb_range_pct,
        option_type="CE",
    )
    score_pe, bd_pe = compute_ai_confluence_score(
        close=latest.close,
        vwap=vwap_values[-1],
        orb_high=orb_high,
        orb_low=orb_low,
        supertrend=st_values[-1],
        direction=st_dirs[-1],
        htf_trend=htf_trend,
        adx=adx_vals[-1],
        plus_di=pdi_vals[-1],
        minus_di=mdi_vals[-1],
        volume_ratio=vol_ratio,
        orb_range_pct=orb_range_pct,
        option_type="PE",
    )

    return IndicatorSnapshot(
        timestamp=latest.timestamp,
        close=latest.close,
        vwap=vwap_values[-1],
        vwap_mode=vwap_mode,
        atr=atr_values[-1],
        supertrend=st_values[-1],
        direction=st_dirs[-1],
        orb_high=orb_high,
        orb_low=orb_low,
        orb_complete=orb.complete,
        adx=adx_vals[-1],
        plus_di=pdi_vals[-1],
        minus_di=mdi_vals[-1],
        htf_trend=htf_trend,
        volume_ratio=vol_ratio,
        orb_range_pct=orb_range_pct,
        ai_score_ce=score_ce,
        ai_score_pe=score_pe,
        ai_breakdown_ce=bd_ce,
        ai_breakdown_pe=bd_pe,
    )
