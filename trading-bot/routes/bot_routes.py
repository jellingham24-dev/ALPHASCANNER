"""
FastAPI routes — bot control, watchlist management, and live scanning.

GET  /bot/status                    — portfolio stats + open positions
POST /bot/tick                      — manually trigger one full scan cycle
GET  /bot/log                       — recent trade log
GET  /bot/watchlist                 — current coin watchlist with metadata
POST /bot/watchlist/refresh         — force-refresh the top-1000 list now
GET  /bot/scan/top                  — scan and return top BUY/SELL signals right now
GET  /bot/signal/{symbol}           — signal preview for one coin (no trading)
GET  /bot/risk                      — current risk config
PATCH /bot/risk                     — update global risk config (SL%, TP%, etc.)
PATCH /bot/position/{symbol}/levels — adjust TP/SL on a live open position
GET  /bot/last-scan                 — results from the most recent bot scan cycle
"""

import asyncio
import json
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
import trader.bot as _bot
from trader.bot import run_once, paper, wl_manager, scanner, exchange, risk, _last_scan_store, _timeframe_scan_stores, update_min_confidence, update_min_confidence_trade

router = APIRouter(prefix="/bot", tags=["bot"])


# ── Status & control ──────────────────────────────────────────────────────────

@router.get("/status")
def bot_status():
    stats = paper.stats()
    open_positions = [
        {
            "symbol":    p.symbol,
            "side":      p.side,
            "qty":       p.quantity,
            "entry":     p.entry_price,
            "sl":        p.stop_loss,
            "tp":        p.take_profit,
            "opened_at": p.opened_at,
        }
        for p in paper.positions if p.status == "open"
    ]
    return {
        **stats,
        "open_positions":        open_positions,
        "watchlist_size":        len(wl_manager.get()),
        "watchlist_age_minutes": round(wl_manager.last_refresh_age_minutes, 1),
    }


class ResetBody(BaseModel):
    starting_balance: Optional[float] = None


@router.post("/reset")
def reset_paper_trader(body: ResetBody = None):
    """Clear all positions and reset balance. Optionally set a new starting balance."""
    if body and body.starting_balance is not None:
        paper.starting_balance = max(100.0, float(body.starting_balance))
    result = paper.reset()
    risk.open_positions = 0
    return {**result, "message": f"Paper trader reset — balance restored to ${paper.starting_balance:,.0f}"}


# ── Settings ──────────────────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    risk_per_trade:        Optional[float] = None
    stop_loss_pct:         Optional[float] = None
    take_profit_pct:       Optional[float] = None
    max_open_positions:    Optional[int]   = None
    max_daily_loss_pct:    Optional[float] = None
    min_confidence:        Optional[float] = None
    min_confidence_trade:  Optional[float] = None
    starting_balance:      Optional[float] = None


@router.get("/settings")
def get_settings():
    return {
        **risk.config.as_dict(),
        "min_confidence":        _bot.MIN_CONFIDENCE,
        "min_confidence_trade":  _bot.MIN_CONFIDENCE_TRADE,
        "starting_balance":      paper.starting_balance,
        "timeframe":             _bot.TIMEFRAME,
        "candle_limit":          _bot.CANDLE_LIMIT,
        "top_n_coins":           _bot.TOP_N_COINS,
    }


@router.patch("/settings")
def update_settings(body: SettingsUpdate):
    risk_fields = {k: v for k, v in body.dict().items()
                   if v is not None and k in {
                       "risk_per_trade", "stop_loss_pct", "take_profit_pct",
                       "max_open_positions", "max_daily_loss_pct"
                   }}
    if risk_fields:
        risk.config.update(**risk_fields)
    if body.min_confidence is not None:
        update_min_confidence(body.min_confidence)
    if body.min_confidence_trade is not None:
        update_min_confidence_trade(body.min_confidence_trade)
    if body.starting_balance is not None:
        paper.starting_balance = max(100.0, float(body.starting_balance))
    return get_settings()


@router.post("/tick")
def manual_tick(
    limit:  Optional[int] = Query(None, description="Number of coins to scan"),
    offset: int           = Query(0,    description="Start position in watchlist (0=top coins)"),
):
    results = run_once(limit=limit, offset=offset)
    return {"ok": True, "executed": len(results), "results": results, "stats": paper.stats()}


@router.post("/scan/trigger")
async def trigger_scan(
    limit:        Optional[int] = Query(None),
    offset:       int           = Query(0),
    timeframe:    str           = Query("1h"),
    candle_limit: int           = Query(50),
):
    """Start a scan in the background and return immediately. Poll /bot/last-scan?timeframe=X for results."""
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, lambda: run_once(limit=limit, offset=offset, timeframe=timeframe, candle_limit=candle_limit))
    current_scanned_at = _timeframe_scan_stores.get(timeframe, {}).get("scanned_at")
    return {"ok": True, "previous_scanned_at": current_scanned_at, "timeframe": timeframe}


@router.get("/log")
def trade_log(limit: int = 50):
    return {"log": paper.trade_log[-limit:]}


# ── Watchlist ─────────────────────────────────────────────────────────────────

@router.get("/watchlist")
def get_watchlist(limit: int = Query(100), offset: int = Query(0)):
    wl = wl_manager.get()
    return {
        "total":       len(wl),
        "offset":      offset,
        "limit":       limit,
        "age_minutes": round(wl_manager.last_refresh_age_minutes, 1),
        "symbols":     wl[offset : offset + limit],
    }


@router.post("/watchlist/refresh")
def refresh_watchlist():
    wl = wl_manager.force_refresh()
    return {"ok": True, "watchlist_size": len(wl), "sample": wl[:10]}


# ── Scanning & signals ────────────────────────────────────────────────────────

@router.get("/scan/top")
def scan_top_signals(
    direction: str = Query("BUY"),
    top_n:     int = Query(20),
    limit:     int = Query(None),
):
    wl = wl_manager.get()
    if limit:
        wl = wl[:limit]
    results = scanner.top_signals(wl, direction=direction, top_n=top_n)
    return {
        "direction": direction,
        "scanned":   len(wl),
        "found":     len(results),
        "signals": [
            {
                "symbol":      r.symbol,
                "confidence":  round(r.confidence, 3),
                "price":       r.price,
                "top_reasons": r.reasons[:3],
            }
            for r in results
        ],
    }


@router.get("/signal/{symbol:path}")
def preview_signal(symbol: str):
    from trader.exchange import fetch_ohlcv
    from trader.signals import compute_signal
    try:
        candles = fetch_ohlcv(exchange, symbol, scanner.timeframe, scanner.candle_limit)
        result  = compute_signal(candles)
        return {
            "symbol":     symbol,
            "signal":     result.signal,
            "confidence": result.confidence,
            "price":      result.price,
            "breakdown":  result.score_breakdown,
            "reasons":    result.reasons,
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/last-scan")
def last_scan(timeframe: Optional[str] = Query(None)):
    """Return the results from the most recent completed scan. Pass ?timeframe=1h or ?timeframe=5m for manual tab results."""
    if timeframe:
        store = _timeframe_scan_stores.get(timeframe)
        if not store:
            return {"available": False, "message": f"No {timeframe} scan completed yet."}
        return {"available": True, **store}
    if not _last_scan_store:
        return {"available": False, "message": "No scan has completed yet. Wait for the first cycle or POST /bot/tick."}
    return {"available": True, **_last_scan_store}


@router.get("/scan/stream")
async def scan_stream(limit: Optional[int] = Query(None)):
    """
    SSE endpoint — streams partial results as each exchange finishes scanning.
    Each event is JSON with type='progress' (after each exchange) or type='done' (final).
    """
    from trader.bot import multi_scanner, wl_managers, MIN_CONFIDENCE, store_scan_results

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _serialize(r):
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

    def on_exchange_done(name: str, partial_raw: dict):
        """Called from a worker thread each time one exchange finishes."""
        partial = multi_scanner._merge(partial_raw)
        actionable = [
            r for r in partial
            if r.signal != "HOLD" and r.confidence >= MIN_CONFIDENCE and not r.error
        ]
        event = {
            "type":               "progress",
            "completed_exchange": name,
            "completed":          list(partial_raw.keys()),
            "remaining":          [n for n in multi_scanner.exchanges if n not in partial_raw],
            "cross_confirmed":    [_serialize(r) for r in actionable if r.cross_confirmed],
            "single_exchange":    [_serialize(r) for r in actionable if not r.cross_confirmed],
            "total_scanned":      sum(len(v) for v in partial_raw.values()),
        }
        asyncio.run_coroutine_threadsafe(queue.put(event), loop)

    def run_scan():
        from trader.bot import _filtered_watchlist, exchanges as _exchanges
        base_wl = wl_manager.get()
        if limit:
            base_wl = base_wl[:limit]
        watchlists = {name: _filtered_watchlist(name, ex, base_wl) for name, ex in _exchanges.items()}
        all_results = multi_scanner.scan(watchlists, on_exchange_done=on_exchange_done)
        store_scan_results(all_results, [], paper.stats())
        asyncio.run_coroutine_threadsafe(queue.put(None), loop)  # sentinel

    asyncio.ensure_future(loop.run_in_executor(None, run_scan))

    async def generate():
        while True:
            event = await queue.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Risk config ───────────────────────────────────────────────────────────────

class RiskConfigUpdate(BaseModel):
    risk_per_trade:     Optional[float] = None
    stop_loss_pct:      Optional[float] = None
    take_profit_pct:    Optional[float] = None
    max_open_positions: Optional[int]   = None
    max_daily_loss_pct: Optional[float] = None


@router.get("/risk")
def get_risk_config():
    return risk.config.as_dict()


@router.patch("/risk")
def update_risk_config(body: RiskConfigUpdate):
    updates = {k: v for k, v in body.dict().items() if v is not None}
    if not updates:
        return {"ok": False, "error": "No fields provided"}
    try:
        risk.config.update(**updates)
        return {"ok": True, "config": risk.config.as_dict()}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


# ── Per-position TP/SL adjustment ─────────────────────────────────────────────

class LevelAdjust(BaseModel):
    stop_loss:   Optional[float] = None
    take_profit: Optional[float] = None


@router.patch("/position/{symbol:path}/levels")
def adjust_position_levels(symbol: str, body: LevelAdjust):
    if body.stop_loss is None and body.take_profit is None:
        return {"ok": False, "error": "Provide at least one of stop_loss or take_profit"}
    result = paper.adjust_levels(
        symbol      = symbol,
        stop_loss   = body.stop_loss,
        take_profit = body.take_profit,
    )
    return result
