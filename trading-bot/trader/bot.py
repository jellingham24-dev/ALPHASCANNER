"""
Bot engine — scans top 1000 coins across Binance + Bybit every candle close.
Cross-confirmed signals (same direction on both exchanges) are prioritised.
"""
from dotenv import load_dotenv
load_dotenv()
import asyncio
import logging
import os
from datetime import datetime

from trader.exchange import get_exchange, get_all_exchanges
from trader.watchlist import WatchlistManager
from trader.scanner import make_scanner, ScanResult
from trader.multi_scanner import MultiExchangeScanner, MultiScanResult
from trader.risk import RiskManager, RiskConfig
from trader.paper_trader import PaperTrader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# ── Scan result stores ────────────────────────────────────────────────────────

_last_scan_store: dict = {}                    # default (bot auto-loop)
_timeframe_scan_stores: dict[str, dict] = {}   # keyed by timeframe for manual scans


def store_scan_results(all_results: list, executed: list, stats: dict, timeframe: str = None) -> None:
    """Persist the latest scan so GET /bot/last-scan can serve it."""
    from datetime import datetime, timezone

    # Mirror the same confidence filter used when deciding trades
    actionable = [
        r for r in all_results
        if r.signal != "HOLD" and r.confidence >= MIN_CONFIDENCE and not r.error
    ]
    cross_confirmed = [r for r in actionable if r.cross_confirmed]
    single_exchange = [r for r in actionable if not r.cross_confirmed]

    def _s(r):
        return {
            "symbol":          r.symbol,
            "signal":          r.signal,
            "confidence":      round(r.confidence, 3),
            "price":           r.price,
            "cross_confirmed": r.cross_confirmed,
            "mtf_aligned":     r.mtf_aligned,
            "exchanges":       list(r.exchanges),
            "reasons":         list(r.reasons[:4]),
        }

    payload = {
        "scanned_at":      datetime.now(timezone.utc).isoformat(),
        "total_scanned":   len(all_results),
        "cross_confirmed": [_s(r) for r in cross_confirmed],
        "single_exchange": [_s(r) for r in single_exchange],
        "executed_count":  len(executed),
        "stats":           stats,
    }
    if timeframe:
        _timeframe_scan_stores[timeframe] = payload
    else:
        _last_scan_store.update(payload)
    log.info(
        f"Scan stored: {len(cross_confirmed)} cross-confirmed, "
        f"{len(single_exchange)} single-exchange"
    )

# ── Config ────────────────────────────────────────────────────────────────────

EXCHANGE_NAME   = os.getenv("EXCHANGE_NAME", "binance")
TIMEFRAME       = os.getenv("TIMEFRAME", "1h")
CANDLE_LIMIT    = int(os.getenv("CANDLE_LIMIT", "60"))
LOOP_INTERVAL   = int(os.getenv("LOOP_INTERVAL", str(60 * 60)))
MIN_CONFIDENCE       = float(os.getenv("MIN_CONFIDENCE", "0.60"))       # show in scanner UI
MIN_CONFIDENCE_TRADE = float(os.getenv("MIN_CONFIDENCE_TRADE", "0.75"))  # required to open a trade
TOP_N_COINS     = int(os.getenv("TOP_N_COINS", "1000"))
MIN_VOLUME_24H  = float(os.getenv("MIN_VOLUME_24H", "500000"))

# ── Shared state ──────────────────────────────────────────────────────────────

exchanges    = get_all_exchanges()
exchange     = exchanges[EXCHANGE_NAME]   # primary exchange

# Cache of which symbols each exchange actually supports (populated lazily)
_exchange_symbols_cache: dict[str, set[str]] = {}


def _filtered_watchlist(name: str, ex, base_wl: list[str]) -> list[str]:
    """Return only the symbols from base_wl that exist on the given exchange."""
    if name not in _exchange_symbols_cache:
        try:
            markets = ex.load_markets()
            _exchange_symbols_cache[name] = {
                m for m, d in markets.items() if d.get("active", True)
            }
            log.info(f"{name}: {len(_exchange_symbols_cache[name])} markets loaded")
        except Exception as e:
            log.warning(f"Could not load markets for {name}: {e}")
            return base_wl
    available = _exchange_symbols_cache[name]
    filtered = [s for s in base_wl if s in available]
    log.info(f"{name}: {len(filtered)}/{len(base_wl)} watchlist symbols available")
    return filtered

# Watchlist built from union of all exchanges — more coins than Binance alone
wl_manager = WatchlistManager(exchange=exchanges, top_n=TOP_N_COINS, min_volume=MIN_VOLUME_24H)

# All exchanges use the same watchlist manager to avoid multiple CoinGecko fetches
wl_managers = {name: wl_manager for name in exchanges}

scanner = make_scanner(exchange, EXCHANGE_NAME, timeframe=TIMEFRAME,
                       candle_limit=CANDLE_LIMIT, min_confidence=MIN_CONFIDENCE)

multi_scanner = MultiExchangeScanner(
    exchanges=exchanges,
    timeframe=TIMEFRAME,
    candle_limit=CANDLE_LIMIT,
    min_confidence=MIN_CONFIDENCE,
)

_scanner_cache: dict[str, MultiExchangeScanner] = {}


def _get_scanner(tf: str, cl: int) -> MultiExchangeScanner:
    if tf == TIMEFRAME and cl == CANDLE_LIMIT:
        return multi_scanner
    key = f"{tf}:{cl}"
    if key not in _scanner_cache:
        _scanner_cache[key] = MultiExchangeScanner(
            exchanges=exchanges, timeframe=tf,
            candle_limit=cl, min_confidence=MIN_CONFIDENCE,
        )
    return _scanner_cache[key]
paper = PaperTrader(starting_balance=float(os.getenv("STARTING_BALANCE", "10000")))
risk  = RiskManager(config=RiskConfig())


def update_min_confidence(value: float) -> float:
    """Update the scan display threshold across all live scanner instances."""
    global MIN_CONFIDENCE
    MIN_CONFIDENCE = max(0.50, min(float(value), 0.95))
    scanner.min_confidence = MIN_CONFIDENCE
    multi_scanner.min_confidence = MIN_CONFIDENCE
    for s in multi_scanner.scanners.values():
        s.min_confidence = MIN_CONFIDENCE
    return MIN_CONFIDENCE


def update_min_confidence_trade(value: float) -> float:
    """Update the minimum confidence required to open a paper trade."""
    global MIN_CONFIDENCE_TRADE
    MIN_CONFIDENCE_TRADE = max(0.50, min(float(value), 0.99))
    return MIN_CONFIDENCE_TRADE


# ── Core logic ────────────────────────────────────────────────────────────────

def run_once(limit: int = None, offset: int = 0, timeframe: str = None, candle_limit: int = None) -> list[dict]:
    tf = timeframe or TIMEFRAME
    cl = candle_limit or CANDLE_LIMIT
    active_scanner = _get_scanner(tf, cl)

    portfolio_value = paper.balance
    halt, halt_reason = risk.should_halt(portfolio_value)
    if halt:
        log.warning(f"Trading halted: {halt_reason}")

    # Always scan regardless of halt — store results for the UI
    base_wl = wl_manager.get()
    if offset or limit:
        end = (offset + limit) if limit else None
        base_wl = base_wl[offset:end]
    watchlists = {name: _filtered_watchlist(name, ex, base_wl) for name, ex in exchanges.items()}
    log.info(f"[{tf}] Scanning {', '.join(f'{n}:{len(w)}' for n,w in watchlists.items())} symbols")

    all_results: list[MultiScanResult] = active_scanner.scan(watchlists)

    # MTF alignment: graduated boost across 5m / 15m / 1h
    MTF_CHAIN = ["5m", "15m", "1h"]
    other_tfs = [t for t in MTF_CHAIN if t != tf and t in _timeframe_scan_stores]
    if other_tfs:
        sig_maps = {}
        for other in other_tfs:
            store = _timeframe_scan_stores[other]
            sig_maps[other] = {
                r_data["symbol"]: r_data["signal"]
                for r_data in store.get("cross_confirmed", []) + store.get("single_exchange", [])
            }
        for r in all_results:
            if r.signal == "HOLD":
                continue
            aligned = sum(1 for other in other_tfs if sig_maps[other].get(r.symbol) == r.signal)
            if aligned == len(other_tfs):          # all others agree — full boost
                r.mtf_aligned = True
                r.confidence  = min(r.confidence + 0.05, 1.0)
            elif aligned > 0:                      # partial alignment — smaller boost
                r.mtf_aligned = True
                r.confidence  = min(r.confidence + 0.02, 1.0)

    # 4H macro filter: block trades that go against the 4H trend
    four_h_block: set[str] = set()
    if "4h" in _timeframe_scan_stores:
        four_h_store = _timeframe_scan_stores["4h"]
        four_h_sigs  = {
            r_data["symbol"]: r_data["signal"]
            for r_data in four_h_store.get("cross_confirmed", []) + four_h_store.get("single_exchange", [])
        }
        four_h_block = {sym for sym, sig in four_h_sigs.items() if sig != "HOLD"}

    # Update open positions (only on the default auto-scan timeframe)
    if tf == TIMEFRAME:
        result_map = {r.symbol: r for r in all_results}
        for p in paper.positions:
            if p.status == "open" and p.symbol in result_map:
                r = result_map[p.symbol]
                if r.price > 0:
                    exits = paper.update_positions(p.symbol, r.price)
                    for exit_event in exits:
                        log.info(f"Position closed: {exit_event}")

    # Build 4H signal map for trade filter (only if 4H has been scanned)
    four_h_sigs: dict[str, str] = {}
    if "4h" in _timeframe_scan_stores:
        store_4h = _timeframe_scan_stores["4h"]
        for rd in store_4h.get("cross_confirmed", []) + store_4h.get("single_exchange", []):
            four_h_sigs[rd["symbol"]] = rd["signal"]

    def _passes_4h_filter(r) -> bool:
        """Allow trade unless 4H explicitly disagrees with the signal direction."""
        if not four_h_sigs:
            return True   # 4H not scanned yet — no filter applied
        four_h_sig = four_h_sigs.get(r.symbol)
        return four_h_sig is None or four_h_sig == r.signal

    # Only cross-confirmed signals that pass the 4H macro filter are eligible
    actionable = [
        r for r in all_results
        if r.signal != "HOLD" and r.confidence >= MIN_CONFIDENCE_TRADE
        and r.cross_confirmed and not r.error
        and _passes_4h_filter(r)
    ]
    buy_signals  = [r for r in actionable if r.signal == "BUY"]
    sell_signals = [r for r in actionable if r.signal == "SELL"]

    log.info(
        f"[{tf}] Cross-confirmed signals: {len(buy_signals)} BUY, {len(sell_signals)} SELL"
    )

    executed = []
    if halt:
        log.info(f"Skipping order execution: {halt_reason}")
    for sig in buy_signals + sell_signals:
        if halt:
            break
        halt_now, reason = risk.should_halt(portfolio_value)
        if halt_now:
            log.info(f"Halting execution: {reason}")
            break

        reason_str = " | ".join(sig.reasons[:3])
        if sig.cross_confirmed:
            reason_str = f"CROSS-CONFIRMED ({','.join(sig.exchanges)}) | " + reason_str

        order = risk.size_order(
            symbol=sig.symbol, side=sig.signal.lower(),
            entry_price=sig.price, portfolio_value=portfolio_value,
            reason=reason_str,
        )
        result = paper.execute_order(order)
        if result["ok"]:
            risk.open_positions += 1
            portfolio_value -= order.quantity * order.entry_price
            cc = " ✨CROSS-CONFIRMED" if sig.cross_confirmed else ""
            log.info(f"✅ {sig.signal} {sig.symbol} @ ${sig.price:,.4f} (conf={sig.confidence:.0%}){cc}")
            executed.append({
                "symbol": sig.symbol, "signal": sig.signal,
                "confidence": sig.confidence, "cross_confirmed": sig.cross_confirmed,
                "exchanges": sig.exchanges, "order": order,
            })
        else:
            log.warning(f"Order rejected: {result['error']}")

    store_scan_results(all_results, executed, paper.stats(), timeframe=tf if timeframe else None)
    return executed


async def run_loop():
    ex_names = ", ".join(exchanges.keys())
    log.info(f"🤖 Bot started — {ex_names} — top {TOP_N_COINS} coins — {TIMEFRAME} candles")

    loop = asyncio.get_event_loop()
    while True:
        log.info(f"\n{'='*60}\nTick: {datetime.utcnow().isoformat()}\n{'='*60}")
        try:
            # Run in executor so the scan never blocks the event loop
            executed = await loop.run_in_executor(None, run_once)
        except Exception as e:
            log.error(f"run_once() error: {e}", exc_info=True)
            executed = []

        stats = paper.stats()
        log.info(
            f"Portfolio: ${stats['current_balance']:,.2f} | "
            f"Return: {stats['return_pct']:+.2f}% | "
            f"Trades: {stats['total_trades']} | Win: {stats['win_rate']}%"
        )
        log.info(f"Next scan in {LOOP_INTERVAL // 60} minutes...")
        await asyncio.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_loop())
