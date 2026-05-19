"""
Signal engine — 10-indicator confluence system.

Each indicator votes independently. The final signal is only
BUY or SELL when enough indicators agree (confluence).

Indicators:
  1. EMA Crossover    (9/21)      — trend direction trigger
  2. MACD             (12/26/9)   — momentum + histogram direction
  3. Supertrend       (10, 3.0)   — ATR-based trend line (non-repainting)
  4. OBV Trend                    — volume accumulation/distribution
  5. ADX              (14)        — trend strength gate
  6. RSI              (14)        — momentum filter, hard veto at extremes
  7. Stochastic RSI   (14/14/3)   — sensitive momentum timing
  8. Bollinger Bands  (20, 2σ)    — volatility + mean reversion filter
  9. VWAP                         — institutional price reference
 10. Volume Trend                 — confirms or rejects price moves

Scoring:
  Each indicator contributes a weight when it votes with the majority.
  Signal fires when score >= MIN_CONFIDENCE (default 0.55).
  ADX < 20, RSI extreme, or StochRSI extreme = hard VETO.
"""

from dataclasses import dataclass, field
from typing import Literal

Signal = Literal["BUY", "SELL", "HOLD"]

MIN_CONFIDENCE = 0.55


@dataclass
class IndicatorVote:
    name: str
    vote: Signal
    weight: float
    reason: str
    value: float = 0.0


@dataclass
class SignalResult:
    signal: Signal
    confidence: float
    reasons: list[str]
    price: float
    votes: list[IndicatorVote] = field(default_factory=list)
    score_breakdown: dict = field(default_factory=dict)


# ── Math helpers ──────────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _sma(values: list[float], period: int) -> list[float]:
    result = []
    for i in range(len(values)):
        start = max(0, i - period + 1)
        result.append(sum(values[start:i+1]) / (i - start + 1))
    return result


def _stdev(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5


def _smooth_rma(values: list[float], period: int, avg_seed: bool = False) -> list[float]:
    if len(values) < period:
        return [sum(values) / len(values)] * len(values)
    seed = sum(values[:period]) / (period if avg_seed else 1)
    result = [seed]
    for v in values[period:]:
        if avg_seed:
            result.append(result[-1] * (period - 1) / period + v / period)
        else:
            result.append(result[-1] - result[-1] / period + v)
    return result


# ── Indicator 1: EMA Crossover ────────────────────────────────────────────────

def _ema_crossover(closes: list[float]) -> IndicatorVote:
    ema9  = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    prev_gap = ema9[-2] - ema21[-2]
    curr_gap = ema9[-1] - ema21[-1]

    if prev_gap < 0 and curr_gap > 0:
        return IndicatorVote("EMA Cross", "BUY",  0.14, "EMA9 freshly crossed above EMA21", curr_gap)
    if prev_gap > 0 and curr_gap < 0:
        return IndicatorVote("EMA Cross", "SELL", 0.14, "EMA9 freshly crossed below EMA21", curr_gap)
    if curr_gap > 0:
        return IndicatorVote("EMA Cross", "BUY",  0.07, "EMA9 above EMA21 — uptrend in progress", curr_gap)
    return     IndicatorVote("EMA Cross", "SELL", 0.07, "EMA9 below EMA21 — downtrend in progress", curr_gap)


# ── Indicator 2: MACD ─────────────────────────────────────────────────────────

def _macd_vote(closes: list[float]) -> IndicatorVote:
    if len(closes) < 35:
        return IndicatorVote("MACD", "HOLD", 0.0, "Not enough data")

    ema12       = _ema(closes, 12)
    ema26       = _ema(closes, 26)
    macd_line   = [ema12[i] - ema26[i] for i in range(len(closes))]
    signal_line = _ema(macd_line, 9)
    histogram   = [macd_line[i] - signal_line[i] for i in range(len(macd_line))]

    above            = macd_line[-1] > signal_line[-1]
    hist_accel_up    = histogram[-1] > histogram[-2] > histogram[-3]
    hist_accel_down  = histogram[-1] < histogram[-2] < histogram[-3]
    hist_turning_up  = histogram[-2] < 0 and histogram[-1] > histogram[-2]
    hist_turning_down= histogram[-2] > 0 and histogram[-1] < histogram[-2]

    if above and hist_accel_up:
        return IndicatorVote("MACD", "BUY",  0.14, "MACD above signal, histogram accelerating up", macd_line[-1])
    if not above and hist_accel_down:
        return IndicatorVote("MACD", "SELL", 0.14, "MACD below signal, histogram accelerating down", macd_line[-1])
    if hist_turning_up:
        return IndicatorVote("MACD", "BUY",  0.10, "MACD histogram turning up — early momentum shift", macd_line[-1])
    if hist_turning_down:
        return IndicatorVote("MACD", "SELL", 0.10, "MACD histogram turning down — early momentum shift", macd_line[-1])
    if above:
        return IndicatorVote("MACD", "BUY",  0.06, "MACD above signal (histogram flat/slowing)", macd_line[-1])
    return     IndicatorVote("MACD", "SELL", 0.06, "MACD below signal (histogram flat/slowing)", macd_line[-1])


# ── Indicator 3: Supertrend ───────────────────────────────────────────────────

def _supertrend_vote(candles: list[dict], period: int = 10, multiplier: float = 3.0) -> IndicatorVote:
    """
    ATR-based trend line. Non-repainting.
    Fresh flip (trend reversal) scores higher than ongoing trend.
    """
    if len(candles) < period + 5:
        return IndicatorVote("Supertrend", "HOLD", 0.0, "Not enough data")

    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    closes = [c["close"] for c in candles]
    n      = len(candles)

    # ATR via Wilder smoothing
    tr = [max(highs[i] - lows[i],
              abs(highs[i] - closes[i-1]),
              abs(lows[i]  - closes[i-1])) for i in range(1, n)]
    atr = [0.0] * n
    atr[period] = sum(tr[:period]) / period
    for i in range(period + 1, n):
        atr[i] = (atr[i-1] * (period - 1) + tr[i-1]) / period

    upper  = [0.0] * n
    lower  = [0.0] * n
    trend  = [1]   * n  # 1 = bullish, -1 = bearish

    for i in range(period, n):
        hl2         = (highs[i] + lows[i]) / 2
        basic_upper = hl2 + multiplier * atr[i]
        basic_lower = hl2 - multiplier * atr[i]

        if i == period:
            upper[i] = basic_upper
            lower[i] = basic_lower
        else:
            upper[i] = basic_upper if (basic_upper < upper[i-1] or closes[i-1] > upper[i-1]) else upper[i-1]
            lower[i] = basic_lower if (basic_lower > lower[i-1] or closes[i-1] < lower[i-1]) else lower[i-1]

        if closes[i] > upper[i]:
            trend[i] = 1
        elif closes[i] < lower[i]:
            trend[i] = -1
        else:
            trend[i] = trend[i-1] if i > period else 1

    curr = trend[-1]
    prev = trend[-2]

    if curr == 1 and prev == -1:
        return IndicatorVote("Supertrend", "BUY",  0.13, "Supertrend flipped bullish — trend reversal confirmed", closes[-1])
    if curr == -1 and prev == 1:
        return IndicatorVote("Supertrend", "SELL", 0.13, "Supertrend flipped bearish — trend reversal confirmed", closes[-1])
    if curr == 1:
        return IndicatorVote("Supertrend", "BUY",  0.09, "Supertrend bullish — price above trend line", closes[-1])
    return     IndicatorVote("Supertrend", "SELL", 0.09, "Supertrend bearish — price below trend line", closes[-1])


# ── Indicator 4: OBV Trend ────────────────────────────────────────────────────

def _obv_series(closes: list[float], volumes: list[float]) -> list[float]:
    obv = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i-1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])
    return obv


def _obv_vote(closes: list[float], volumes: list[float], lookback: int = 10) -> IndicatorVote:
    if len(closes) < lookback + 2:
        return IndicatorVote("OBV", "HOLD", 0.0, "Not enough data")

    obv         = _obv_series(closes, volumes)
    obv_rising  = obv[-1] > obv[-lookback]
    price_rising= closes[-1] > closes[-lookback]

    if obv_rising and price_rising:
        return IndicatorVote("OBV", "BUY",  0.11, "OBV rising with price — volume confirming uptrend",         obv[-1])
    if not obv_rising and not price_rising:
        return IndicatorVote("OBV", "SELL", 0.11, "OBV falling with price — volume confirming downtrend",      obv[-1])
    if obv_rising and not price_rising:
        return IndicatorVote("OBV", "BUY",  0.09, "Bullish OBV divergence — buyers absorbing the sell-off",   obv[-1])
    return         IndicatorVote("OBV", "SELL", 0.09, "Bearish OBV divergence — price rising on shrinking conviction", obv[-1])


# ── Indicator 5: ADX ──────────────────────────────────────────────────────────

def _adx_vote(candles: list[dict], period: int = 14) -> IndicatorVote:
    """ADX < 20 = hard VETO. ADX ≥ 25 = strong trend."""
    if len(candles) < period * 2 + 2:
        return IndicatorVote("ADX", "HOLD", 0.0, "Not enough data for ADX — VETO active", 0.0)

    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    closes = [c["close"] for c in candles]

    tr_list, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        tr     = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        h_move = highs[i] - highs[i-1]
        l_move = lows[i-1] - lows[i]
        tr_list.append(tr)
        plus_dm.append(h_move if h_move > l_move and h_move > 0 else 0.0)
        minus_dm.append(l_move if l_move > h_move and l_move > 0 else 0.0)

    atr_s   = _smooth_rma(tr_list, period, avg_seed=True)
    plus_s  = _smooth_rma(plus_dm,  period, avg_seed=True)
    minus_s = _smooth_rma(minus_dm, period, avg_seed=True)

    plus_di  = [100 * plus_s[i]  / atr_s[i] if atr_s[i] > 0 else 0 for i in range(len(atr_s))]
    minus_di = [100 * minus_s[i] / atr_s[i] if atr_s[i] > 0 else 0 for i in range(len(atr_s))]
    dx       = [100 * abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i])
                if (plus_di[i] + minus_di[i]) > 0 else 0 for i in range(len(plus_di))]
    adx_vals = _smooth_rma(dx, period, avg_seed=True)

    adx, pdi, mdi = adx_vals[-1], plus_di[-1], minus_di[-1]

    if adx < 20:
        return IndicatorVote("ADX", "HOLD", 0.0, f"ADX {adx:.1f} — ranging/choppy market VETO", adx)

    strength = "strong" if adx > 25 else "developing"
    w        = 0.11 if adx > 25 else 0.08

    if pdi > mdi:
        return IndicatorVote("ADX", "BUY",  w, f"ADX {adx:.1f} ({strength}) — bulls in control (+DI {pdi:.1f} > -DI {mdi:.1f})", adx)
    return     IndicatorVote("ADX", "SELL", w, f"ADX {adx:.1f} ({strength}) — bears in control (-DI {mdi:.1f} > +DI {pdi:.1f})", adx)


# ── Indicator 6: RSI ──────────────────────────────────────────────────────────

def _rsi_raw(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas   = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains    = [max(d, 0)    for d in deltas[-period:]]
    losses   = [abs(min(d, 0)) for d in deltas[-period:]]
    avg_gain = sum(gains)  / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def _rsi_vote(closes: list[float]) -> IndicatorVote:
    rsi = _rsi_raw(closes)
    if rsi > 75:
        return IndicatorVote("RSI", "HOLD", 0.0, f"RSI {rsi:.1f} — overbought VETO", rsi)
    if rsi < 25:
        return IndicatorVote("RSI", "HOLD", 0.0, f"RSI {rsi:.1f} — oversold VETO",   rsi)
    if rsi >= 60:
        return IndicatorVote("RSI", "BUY",  0.09, f"RSI {rsi:.1f} — bullish momentum zone", rsi)
    if rsi <= 40:
        return IndicatorVote("RSI", "SELL", 0.09, f"RSI {rsi:.1f} — bearish momentum zone", rsi)
    return     IndicatorVote("RSI", "HOLD", 0.0,  f"RSI {rsi:.1f} — neutral zone (40-60)",  rsi)


# ── Indicator 7: Stochastic RSI ───────────────────────────────────────────────

def _stoch_rsi_vote(closes: list[float], rsi_period: int = 14, stoch_period: int = 14, smooth_k: int = 3) -> IndicatorVote:
    """
    Stochastic applied to RSI values.
    Much more sensitive than plain RSI for catching momentum turns.
    Hard VETO at extreme levels (>85 or <15).
    Crossing out of oversold zone (<20) or overbought zone (>80) = strongest signal.
    """
    min_len = rsi_period + stoch_period + smooth_k + 5
    if len(closes) < min_len:
        return IndicatorVote("StochRSI", "HOLD", 0.0, "Not enough data")

    # Build RSI series
    rsi_series = [_rsi_raw(closes[:i+1], rsi_period) for i in range(rsi_period, len(closes))]

    if len(rsi_series) < stoch_period:
        return IndicatorVote("StochRSI", "HOLD", 0.0, "Not enough data")

    # Apply stochastic to RSI
    stoch_k = []
    for i in range(stoch_period - 1, len(rsi_series)):
        window = rsi_series[i - stoch_period + 1: i + 1]
        lo, hi = min(window), max(window)
        stoch_k.append((rsi_series[i] - lo) / (hi - lo) * 100 if hi != lo else 50.0)

    if len(stoch_k) < smooth_k + 1:
        return IndicatorVote("StochRSI", "HOLD", 0.0, "Not enough data")

    smoothed = _sma(stoch_k, smooth_k)
    k_curr   = smoothed[-1]
    k_prev   = smoothed[-2]

    if k_curr > 85:
        return IndicatorVote("StochRSI", "HOLD", 0.0, f"StochRSI {k_curr:.0f} — extremely overbought VETO", k_curr)
    if k_curr < 15:
        return IndicatorVote("StochRSI", "HOLD", 0.0, f"StochRSI {k_curr:.0f} — extremely oversold VETO",   k_curr)

    if k_prev <= 20 and k_curr > 20:
        return IndicatorVote("StochRSI", "BUY",  0.09, f"StochRSI crossing out of oversold ({k_curr:.0f}) — momentum reversal", k_curr)
    if k_prev >= 80 and k_curr < 80:
        return IndicatorVote("StochRSI", "SELL", 0.09, f"StochRSI crossing out of overbought ({k_curr:.0f}) — momentum reversal", k_curr)
    if k_curr > 50 and k_curr > k_prev:
        return IndicatorVote("StochRSI", "BUY",  0.06, f"StochRSI {k_curr:.0f} — bullish momentum building", k_curr)
    if k_curr < 50 and k_curr < k_prev:
        return IndicatorVote("StochRSI", "SELL", 0.06, f"StochRSI {k_curr:.0f} — bearish momentum building", k_curr)
    return IndicatorVote("StochRSI", "HOLD", 0.0, f"StochRSI {k_curr:.0f} — neutral", k_curr)


# ── Indicator 8: Bollinger Bands ──────────────────────────────────────────────

def _bollinger_vote(closes: list[float], period: int = 20, mult: float = 2.0) -> IndicatorVote:
    if len(closes) < period + 2:
        return IndicatorVote("BB", "HOLD", 0.0, "Not enough data")

    mid      = _sma(closes, period)[-1]
    std      = _stdev(closes[-period:])
    upper    = mid + mult * std
    lower    = mid - mult * std
    price    = closes[-1]
    pct_b    = (price - lower) / (upper - lower) if upper != lower else 0.5
    squeeze  = (upper - lower) / mid < 0.04 if mid > 0 else False

    if pct_b < 0.2:
        note = " (squeeze: breakout setup)" if squeeze else ""
        return IndicatorVote("BB", "BUY",  0.08, f"Near lower band — oversold within range %B={pct_b:.2f}{note}", pct_b)
    if pct_b > 0.8:
        note = " (squeeze: breakout setup)" if squeeze else ""
        return IndicatorVote("BB", "SELL", 0.08, f"Near upper band — overbought within range %B={pct_b:.2f}{note}", pct_b)
    if squeeze:
        return IndicatorVote("BB", "HOLD", 0.0, f"BB squeeze — breakout pending (%B={pct_b:.2f})", pct_b)
    return     IndicatorVote("BB", "HOLD", 0.0, f"Price mid-range (%B={pct_b:.2f})", pct_b)


# ── Indicator 9: VWAP ────────────────────────────────────────────────────────

def _vwap_vote(candles: list[dict]) -> IndicatorVote:
    """
    Volume-Weighted Average Price.
    Price above VWAP = institutional buy bias (market paying up).
    Price below VWAP = distribution bias.
    """
    cum_pv = sum((c["high"] + c["low"] + c["close"]) / 3 * c["volume"] for c in candles)
    cum_v  = sum(c["volume"] for c in candles)

    if cum_v == 0:
        return IndicatorVote("VWAP", "HOLD", 0.0, "No volume data")

    vwap     = cum_pv / cum_v
    price    = candles[-1]["close"]
    pct_diff = (price - vwap) / vwap * 100

    if price > vwap:
        return IndicatorVote("VWAP", "BUY",  0.08, f"Price {pct_diff:+.1f}% above VWAP — institutional buy bias", vwap)
    return     IndicatorVote("VWAP", "SELL", 0.08, f"Price {pct_diff:+.1f}% below VWAP — distribution bias",     vwap)


# ── Indicator 10: Volume Trend ────────────────────────────────────────────────

def _volume_trend_vote(volumes: list[float], closes: list[float], lookback: int = 5) -> IndicatorVote:
    if len(volumes) < lookback * 2 + 1:
        return IndicatorVote("Vol Trend", "HOLD", 0.0, "Not enough data")

    vol_recent   = sum(volumes[-lookback:])  / lookback
    vol_prior    = sum(volumes[-lookback*2:-lookback]) / lookback
    vol_expanding= vol_recent > vol_prior * 1.1
    price_rising = closes[-1] > closes[-lookback]
    ratio        = vol_recent / vol_prior if vol_prior > 0 else 1.0

    if vol_expanding and price_rising:
        return IndicatorVote("Vol Trend", "BUY",  0.08, f"Volume expanding {(ratio-1)*100:+.0f}% with rising price",   ratio)
    if vol_expanding and not price_rising:
        return IndicatorVote("Vol Trend", "SELL", 0.08, f"Volume expanding {(ratio-1)*100:+.0f}% with falling price",  ratio)
    if not vol_expanding and price_rising:
        return IndicatorVote("Vol Trend", "SELL", 0.05, "Price rising on shrinking volume — weak move",                 ratio)
    return         IndicatorVote("Vol Trend", "BUY",  0.05, "Selling on shrinking volume — exhaustion signal",          ratio)


# ── Confluence engine ─────────────────────────────────────────────────────────

def compute_signal(candles: list[dict]) -> SignalResult:
    """
    Runs all 10 indicators and aggregates votes into a final signal.

    Hard VETO conditions (any one blocks the signal):
      - ADX < 20        (choppy/ranging)
      - RSI > 75 / < 25 (extreme extension)
      - StochRSI > 85 / < 15 (extreme momentum)

    Max score ≈ 1.05 (capped at 1.0).
    Need ~6-7 indicators aligned to reach 0.70 threshold.
    Need ~8-9 to reach 0.90 trade threshold.
    """
    if len(candles) < 35:
        return SignalResult("HOLD", 0.0, ["Need 35+ candles"], candles[-1]["close"])

    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]
    price   = closes[-1]

    ema_v   = _ema_crossover(closes)
    macd_v  = _macd_vote(closes)
    st_v    = _supertrend_vote(candles)
    obv_v   = _obv_vote(closes, volumes)
    adx_v   = _adx_vote(candles)
    rsi_v   = _rsi_vote(closes)
    srsi_v  = _stoch_rsi_vote(closes)
    bb_v    = _bollinger_vote(closes)
    vwap_v  = _vwap_vote(candles)
    vol_v   = _volume_trend_vote(volumes, closes)

    all_votes = [ema_v, macd_v, st_v, obv_v, adx_v, rsi_v, srsi_v, bb_v, vwap_v, vol_v]

    vetoes = [v for v in all_votes if v.vote == "HOLD" and v.weight == 0.0 and "VETO" in v.reason]
    if vetoes:
        reasons = [v.reason for v in vetoes]
        other   = [v.reason for v in all_votes if v.vote != "HOLD"]
        return SignalResult("HOLD", 0.0, reasons + other, price, all_votes,
                            {v.name: {"vote": v.vote, "weight": v.weight, "reason": v.reason} for v in all_votes})

    buy_score  = sum(v.weight for v in all_votes if v.vote == "BUY")
    sell_score = sum(v.weight for v in all_votes if v.vote == "SELL")
    breakdown  = {v.name: {"vote": v.vote, "weight": v.weight, "reason": v.reason} for v in all_votes}

    if buy_score >= MIN_CONFIDENCE and buy_score > sell_score:
        direction: Signal = "BUY"
        score = buy_score
    elif sell_score >= MIN_CONFIDENCE and sell_score > buy_score:
        direction = "SELL"
        score = sell_score
    else:
        direction = "HOLD"
        score = max(buy_score, sell_score)

    aligned  = [v.reason for v in all_votes if v.vote == direction]
    opposing = [f"[against] {v.reason}" for v in all_votes if v.vote not in (direction, "HOLD")]

    return SignalResult(
        signal=direction,
        confidence=min(score, 1.0),
        reasons=aligned + opposing,
        price=price,
        votes=all_votes,
        score_breakdown=breakdown,
    )
