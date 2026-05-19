"""
scanner.py — Concurrent market scanner for large watchlists.

Scanning 1000 coins sequentially at 1 call/second = 16 minutes per pass.
This module uses a thread pool with rate limiting to scan the full list
in ~2-4 minutes, respecting exchange API limits.

Rate limits (approximate):
  Binance:  1200 requests/min = 20/sec
  Coinbase: 10 requests/sec
  Kraken:   1 request/sec (use smaller watchlist or longer timeframe)

Tune WORKERS and CALLS_PER_SECOND to your exchange.
"""

import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from threading import Semaphore

import ccxt

from trader.signals import compute_signal, SignalResult
from trader.exchange import fetch_ohlcv

log = logging.getLogger("scanner")

# ── Rate limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    """Token-bucket rate limiter. Thread-safe."""

    def __init__(self, calls_per_second: float = 5.0):
        self.min_interval = 1.0 / calls_per_second
        self._last_call   = 0.0
        self._sem         = Semaphore(1)

    def wait(self):
        with self._sem:
            elapsed = time.monotonic() - self._last_call
            gap     = self.min_interval - elapsed
            if gap > 0:
                time.sleep(gap)
            self._last_call = time.monotonic()


# ── Per-symbol result ─────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    symbol:     str
    signal:     str   # BUY / SELL / HOLD
    confidence: float
    price:      float
    reasons:    list[str]
    error:      str = ""


# ── Scanner ───────────────────────────────────────────────────────────────────

class MarketScanner:
    """
    Scans a list of symbols concurrently and returns all actionable signals.

    Usage:
        scanner = MarketScanner(exchange, workers=10, calls_per_second=8)
        signals = scanner.scan(watchlist, timeframe="1h", candle_limit=50)
        buy_signals  = [s for s in signals if s.signal == "BUY"]
    """

    def __init__(
        self,
        exchange:          ccxt.Exchange,
        workers:           int   = 10,    # parallel threads
        calls_per_second:  float = 5.0,   # stay well under exchange limit
        timeframe:         str   = "1h",
        candle_limit:      int   = 60,
        min_confidence:    float = 0.55,
    ):
        self.exchange        = exchange
        self.workers         = workers
        self.timeframe       = timeframe
        self.candle_limit    = candle_limit
        self.min_confidence  = min_confidence
        self._rate           = RateLimiter(calls_per_second)

    def _scan_one(self, symbol: str) -> ScanResult:
        """Fetch + compute signal for a single symbol."""
        try:
            self._rate.wait()
            candles = fetch_ohlcv(self.exchange, symbol, self.timeframe, self.candle_limit)
            result  = compute_signal(candles)
            return ScanResult(
                symbol     = symbol,
                signal     = result.signal,
                confidence = result.confidence,
                price      = result.price,
                reasons    = result.reasons,
            )
        except ccxt.BadSymbol:
            return ScanResult(symbol, "HOLD", 0, 0, [], error="symbol not found")
        except ccxt.NetworkError as e:
            return ScanResult(symbol, "HOLD", 0, 0, [], error=f"network: {e}")
        except Exception as e:
            return ScanResult(symbol, "HOLD", 0, 0, [], error=str(e))

    def scan(
        self,
        symbols:        list[str],
        batch_size:     int  = 100,   # process in chunks to manage memory
        log_progress:   bool = True,
    ) -> list[ScanResult]:
        """
        Scan all symbols concurrently.
        Returns ALL results (including HOLDs). Filter by .signal afterward.
        """
        total   = len(symbols)
        done    = 0
        results = []

        if log_progress:
            log.info(f"Scanning {total} symbols with {self.workers} workers "
                     f"@ {1/self._rate.min_interval:.0f} req/s...")

        start = time.monotonic()

        # Process in batches to avoid creating thousands of futures at once
        for batch_start in range(0, total, batch_size):
            batch = symbols[batch_start : batch_start + batch_size]

            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = {pool.submit(self._scan_one, sym): sym for sym in batch}
                for future in as_completed(futures):
                    results.append(future.result())
                    done += 1
                    if log_progress and done % 100 == 0:
                        elapsed  = time.monotonic() - start
                        rate     = done / elapsed
                        eta_secs = (total - done) / rate if rate > 0 else 0
                        log.info(
                            f"  Progress: {done}/{total} "
                            f"({done/total*100:.0f}%) "
                            f"ETA: {eta_secs/60:.1f}min"
                        )

        elapsed = time.monotonic() - start
        errors  = [r for r in results if r.error]
        signals = [r for r in results if r.signal != "HOLD" and r.confidence >= self.min_confidence]

        log.info(
            f"Scan complete: {total} symbols in {elapsed:.0f}s | "
            f"{len(signals)} signals | {len(errors)} errors"
        )

        return results

    def top_signals(
        self,
        symbols:    list[str],
        direction:  str = "BUY",
        top_n:      int = 20,
    ) -> list[ScanResult]:
        """
        Convenience: scan and return top N signals for a given direction,
        sorted by confidence descending.
        """
        all_results = self.scan(symbols)
        filtered    = [
            r for r in all_results
            if r.signal == direction and r.confidence >= self.min_confidence
        ]
        return sorted(filtered, key=lambda r: r.confidence, reverse=True)[:top_n]


# ── Exchange-specific presets ─────────────────────────────────────────────────

EXCHANGE_PRESETS = {
    #  exchange    workers  calls/sec
    "binance":    (15,      10.0),
    "coinbase":   (8,       8.0),
    "kraken":     (5,       2.0),
    "bybit":      (12,      8.0),
    "okx":        (10,      8.0),
    "kucoin":     (8,       6.0),
    "gate":       (10,      8.0),
    "mexc":       (8,       5.0),
    "htx":        (10,      8.0),
    "bitget":     (10,      8.0),
    "phemex":     (10,      8.0),
    "cryptocom":  (8,       5.0),
}

_TF_ORDER = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"]


def resolve_timeframe(exchange: ccxt.Exchange, requested: str) -> str:
    """Return requested timeframe if the exchange supports it, else the nearest longer one."""
    supported = getattr(exchange, "timeframes", {})
    if not supported or requested in supported:
        return requested
    try:
        idx = _TF_ORDER.index(requested)
    except ValueError:
        return requested
    for tf in _TF_ORDER[idx:]:
        if tf in supported:
            log.info(f"{exchange.id}: {requested} not supported, using {tf}")
            return tf
    return requested


def make_scanner(exchange: ccxt.Exchange, exchange_name: str = "binance", **kwargs) -> MarketScanner:
    """Create a scanner with exchange-appropriate rate limits."""
    workers, rps = EXCHANGE_PRESETS.get(exchange_name, (8, 5.0))
    if "timeframe" in kwargs:
        kwargs["timeframe"] = resolve_timeframe(exchange, kwargs["timeframe"])
    return MarketScanner(exchange, workers=workers, calls_per_second=rps, **kwargs)
