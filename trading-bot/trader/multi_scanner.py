"""
multi_scanner.py — Scans multiple exchanges in parallel and cross-confirms signals.

A signal is "cross-confirmed" when the same coin fires the same direction
(BUY or SELL) on both Binance AND Bybit. Cross-confirmed signals are
significantly more reliable than single-exchange signals.

Confidence boost for cross-confirmed signals: +0.10
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import ccxt

from trader.scanner import MarketScanner, ScanResult, make_scanner, RateLimiter
from trader.watchlist import WatchlistManager, get_exchange_symbols
from trader.exchange import fetch_ohlcv
from trader.signals import compute_signal

log = logging.getLogger("multi_scanner")

CROSS_CONFIRM_BOOST = 0.10   # confidence bonus for appearing on both exchanges


@dataclass
class MultiScanResult:
    symbol:           str
    signal:           str           # BUY / SELL / HOLD
    confidence:       float
    price:            float
    reasons:          list[str]
    exchanges:        list[str]     # which exchanges fired this signal
    cross_confirmed:  bool = False  # True if signal matches on 2+ exchanges
    mtf_aligned:      bool = False  # True if same direction on the other timeframe
    votes:            dict = field(default_factory=dict)
    error:            str = ""


class MultiExchangeScanner:
    """
    Scans multiple exchanges concurrently.
    Merges results and flags cross-confirmed signals.
    """

    def __init__(
        self,
        exchanges:      dict,           # {name: ccxt.Exchange}
        timeframe:      str   = "1h",
        candle_limit:   int   = 60,
        min_confidence: float = 0.55,
        workers_each:   int   = 10,
    ):
        self.exchanges      = exchanges
        self.timeframe      = timeframe
        self.candle_limit   = candle_limit
        self.min_confidence = min_confidence

        # Create a scanner per exchange
        self.scanners: dict[str, MarketScanner] = {
            name: make_scanner(ex, name, timeframe=timeframe,
                               candle_limit=candle_limit,
                               min_confidence=min_confidence)
            for name, ex in exchanges.items()
        }

    def _scan_exchange(self, name: str, symbols: list[str]) -> list[ScanResult]:
        """Scan one exchange and return its results."""
        scanner = self.scanners[name]
        log.info(f"Starting scan on {name} ({len(symbols)} symbols)...")
        results = scanner.scan(symbols, log_progress=True)
        log.info(f"{name} scan complete: {len(results)} results")
        return results

    def _merge(self, raw: dict[str, list[ScanResult]]) -> list[MultiScanResult]:
        """Merge per-exchange ScanResults into cross-confirmed MultiScanResults."""
        by_symbol: dict[str, dict[str, ScanResult]] = {}
        for ex_name, results in raw.items():
            for r in results:
                by_symbol.setdefault(r.symbol, {})[ex_name] = r

        merged: list[MultiScanResult] = []
        for symbol, ex_results in by_symbol.items():
            signals = {
                name: r for name, r in ex_results.items()
                if r.signal != "HOLD" and r.confidence >= self.min_confidence and not r.error
            }
            if not signals:
                best = max(ex_results.values(), key=lambda r: r.confidence)
                merged.append(MultiScanResult(
                    symbol=symbol, signal="HOLD",
                    confidence=best.confidence, price=best.price,
                    reasons=best.reasons, exchanges=list(ex_results.keys()),
                    votes=getattr(best, "votes", {}),
                ))
                continue

            directions = {r.signal for r in signals.values()}
            cross_confirmed = len(directions) == 1 and len(signals) >= 2
            avg_conf = sum(r.confidence for r in signals.values()) / len(signals)
            if cross_confirmed:
                avg_conf = min(avg_conf + CROSS_CONFIRM_BOOST, 1.0)
            best_result = max(signals.values(), key=lambda r: r.confidence)
            direction = list(directions)[0] if len(directions) == 1 else best_result.signal
            all_reasons = []
            for ex_name, r in signals.items():
                for reason in r.reasons[:2]:
                    all_reasons.append(f"[{ex_name}] {reason}")

            merged.append(MultiScanResult(
                symbol=symbol, signal=direction,
                confidence=round(avg_conf, 3), price=best_result.price,
                reasons=all_reasons, exchanges=list(signals.keys()),
                cross_confirmed=cross_confirmed,
                votes=getattr(best_result, "votes", {}),
            ))

        merged.sort(key=lambda r: (r.cross_confirmed, r.confidence), reverse=True)
        return merged

    def scan(
        self,
        watchlists: dict[str, list[str]],
        on_exchange_done=None,   # callback(exchange_name, partial_raw_copy)
    ) -> list[MultiScanResult]:
        """
        Scan all exchanges in parallel.
        on_exchange_done is called from a worker thread each time one exchange finishes.
        Returns merged MultiScanResult list, cross-confirmed signals first.
        """
        raw: dict[str, list[ScanResult]] = {}
        with ThreadPoolExecutor(max_workers=len(self.exchanges)) as pool:
            futures = {
                pool.submit(self._scan_exchange, name, syms): name
                for name, syms in watchlists.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    raw[name] = future.result()
                except Exception as e:
                    log.error(f"Scan failed for {name}: {e}")
                    raw[name] = []
                if on_exchange_done:
                    on_exchange_done(name, dict(raw))  # snapshot copy

        return self._merge(raw)

    def get_combined_watchlist(
        self,
        wl_managers: dict[str, "WatchlistManager"],
    ) -> dict[str, list[str]]:
        """Get watchlists for all exchanges."""
        return {name: wm.get() for name, wm in wl_managers.items()}
