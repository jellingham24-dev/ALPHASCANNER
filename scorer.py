"""Trend scoring.

For each coin, we have % changes over 5 timeframes. We want a single score
in roughly [-1, +1] where:
    +1.0 = strong, sustained uptrend across all timeframes
    -1.0 = strong, sustained downtrend across all timeframes
     0.0 = mixed signals or flat

How it works:
    1. Each % change is squashed through tanh(pct / scale), giving a
       bounded, smooth signal in (-1, +1). The scale defines what counts
       as "extreme" for that timeframe.
    2. We take a weighted average across timeframes (longer = more weight).
    3. If every timeframe agrees in sign, we apply a 1.25x alignment bonus
       (clamped to ±1). This is the "high-conviction" signal.

This file is the most opinionated part of the project. Tweak the SCALES,
WEIGHTS, and bonus to taste. Good things to add later:
    - RSI / MACD from real candles (not just % change)
    - Volume confirmation (rising price + rising volume = stronger)
    - Volatility adjustment (penalize highly volatile coins)
    - Market cap rank weighting (small caps move more, normalize for that)
"""
import math

# What % move on each timeframe maps to ~tanh(1) ≈ 0.76? (i.e., "extreme")
# Calibrated for crypto: short timeframes need smaller scales because moves
# of even a few percent in an hour are notable; 1y can swing hundreds of %.
SCALES = {
    "pct_1h": 3.0,
    "pct_24h": 8.0,
    "pct_7d": 20.0,
    "pct_30d": 50.0,
    "pct_1y": 150.0,
}

# Sum of weights = 1.0. Heavier weight on longer timeframes biases toward
# "established trend" rather than noise. Flip these if you want a more
# momentum-chasing scanner.
WEIGHTS = {
    "pct_1h": 0.05,
    "pct_24h": 0.15,
    "pct_7d": 0.20,
    "pct_30d": 0.30,
    "pct_1y": 0.30,
}

ALIGNMENT_BONUS = 1.25  # multiplier when all timeframes agree in sign


def score_coin(coin: dict) -> tuple[float, bool]:
    """Return (score, aligned) for a coin. See module docstring."""
    components: dict[str, float] = {}
    for field, scale in SCALES.items():
        pct = coin.get(field)
        components[field] = math.tanh(pct / scale) if pct is not None else 0.0

    score = sum(components[k] * WEIGHTS[k] for k in WEIGHTS)

    nonzero = [v for v in components.values() if v != 0.0]
    aligned = False
    if nonzero and all(v > 0 for v in nonzero):
        score = min(score * ALIGNMENT_BONUS, 1.0)
        aligned = True
    elif nonzero and all(v < 0 for v in nonzero):
        score = max(score * ALIGNMENT_BONUS, -1.0)
        aligned = True

    return score, aligned


def score_all(coins: list[dict]) -> list[dict]:
    """Add 'score' and 'aligned' fields to each coin (in-place + return)."""
    for coin in coins:
        score, aligned = score_coin(coin)
        coin["score"] = round(score, 4)
        coin["aligned"] = 1 if aligned else 0
    return coins
