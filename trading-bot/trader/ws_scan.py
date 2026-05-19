"""
ws_scan.py — WebSocket endpoint that streams live multi-exchange scan results.

Message types sent to client:
  { type: "start",   total, exchanges: ["binance","bybit"] }
  { type: "result",  symbol, signal, confidence, price, votes,
                     exchanges, cross_confirmed, done, total }
  { type: "complete", total_scanned, signals, elapsed_seconds }
  { type: "error",   message }

Client sends:
  { action: "start", direction: "ALL"|"BUY"|"SELL", limit: 100 }
  { action: "stop" }
"""

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from trader.bot      import wl_managers, multi_scanner, exchanges, scanner, exchange
from trader.exchange import fetch_ohlcv
from trader.signals  import compute_signal

log    = logging.getLogger("ws_scan")
router = APIRouter()


def _scan_one_multi(symbol: str, timeframe: str, limit: int) -> dict:
    """Scan a single symbol across all configured exchanges."""
    exchange_results = {}

    for ex_name, ex in exchanges.items():
        try:
            sc = multi_scanner.scanners[ex_name]
            sc._rate.wait()
            candles = fetch_ohlcv(ex, symbol, timeframe, limit)
            result  = compute_signal(candles)
            exchange_results[ex_name] = {
                "signal":     result.signal,
                "confidence": round(result.confidence, 3),
                "price":      result.price,
                "votes": {
                    name: {"vote": d["vote"], "weight": round(d["weight"], 3), "reason": d["reason"]}
                    for name, d in result.score_breakdown.items()
                },
            }
        except Exception as e:
            exchange_results[ex_name] = {"signal": "HOLD", "confidence": 0,
                                          "price": 0, "votes": {}, "error": str(e)}

    # Determine merged signal
    actionable = {
        n: r for n, r in exchange_results.items()
        if r.get("signal") not in ("HOLD", None) and r.get("confidence", 0) >= 0.55
    }

    if not actionable:
        best = max(exchange_results.values(), key=lambda r: r.get("confidence", 0))
        return {
            "symbol": symbol, "signal": "HOLD",
            "confidence": best.get("confidence", 0),
            "price": best.get("price", 0),
            "votes": best.get("votes", {}),
            "exchanges": list(exchange_results.keys()),
            "cross_confirmed": False,
            "exchange_results": exchange_results,
            "error": None,
        }

    directions = {r["signal"] for r in actionable.values()}
    cross_confirmed = len(directions) == 1 and len(actionable) >= 2

    avg_conf = sum(r["confidence"] for r in actionable.values()) / len(actionable)
    if cross_confirmed:
        avg_conf = min(avg_conf + 0.10, 1.0)

    best_ex = max(actionable.items(), key=lambda kv: kv[1]["confidence"])
    direction = list(directions)[0] if len(directions) == 1 else best_ex[1]["signal"]

    return {
        "symbol":           symbol,
        "signal":           direction,
        "confidence":       round(avg_conf, 3),
        "price":            best_ex[1]["price"],
        "votes":            best_ex[1]["votes"],
        "exchanges":        list(actionable.keys()),
        "cross_confirmed":  cross_confirmed,
        "exchange_results": exchange_results,
        "error":            None,
    }


@router.websocket("/ws/scan")
async def websocket_scan(ws: WebSocket):
    await ws.accept()
    log.info("WebSocket client connected")

    active   = False
    executor = ThreadPoolExecutor(max_workers=scanner.workers)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                await ws.send_text(json.dumps({"type": "error", "message": "Invalid JSON"}))
                continue

            if msg.get("action") == "stop":
                active = False
                continue

            if msg.get("action") != "start":
                continue

            direction  = msg.get("direction", "ALL").upper()
            limit      = int(msg.get("limit", 1000))
            timeframe  = msg.get("timeframe", scanner.timeframe)
            candle_n   = int(msg.get("candle_limit", scanner.candle_limit))

            # Build combined watchlist (union of all exchanges)
            all_symbols: set[str] = set()
            for wm in wl_managers.values():
                all_symbols.update(wm.get())
            watchlist = list(all_symbols)[:limit]
            total     = len(watchlist)
            active    = True

            await ws.send_text(json.dumps({
                "type":      "start",
                "total":     total,
                "exchanges": list(exchanges.keys()),
            }))
            log.info(f"WS multi-scan: {total} symbols across {list(exchanges.keys())}")

            done     = 0
            signals  = []
            start_ts = time.monotonic()
            loop     = asyncio.get_event_loop()

            BATCH = 100
            for batch_start in range(0, total, BATCH):
                if not active:
                    break
                batch = watchlist[batch_start : batch_start + BATCH]
                futures = {
                    executor.submit(_scan_one_multi, sym, timeframe, candle_n): sym
                    for sym in batch
                }
                for future in as_completed(futures):
                    if not active:
                        break
                    result = await loop.run_in_executor(None, future.result)
                    done  += 1
                    payload = {"type": "result", "done": done, "total": total, **result}
                    await ws.send_text(json.dumps(payload))
                    if result["signal"] != "HOLD" and result["confidence"] >= 0.55:
                        signals.append(result)
                await asyncio.sleep(0)

            elapsed = round(time.monotonic() - start_ts, 1)
            # Sort signals: cross-confirmed first, then by confidence
            signals.sort(key=lambda r: (r["cross_confirmed"], r["confidence"]), reverse=True)
            await ws.send_text(json.dumps({
                "type": "complete", "total_scanned": done,
                "elapsed_seconds": elapsed, "signals": signals,
            }))
            log.info(f"WS scan complete: {done} scanned, {len(signals)} signals, {elapsed}s")
            active = False

    except WebSocketDisconnect:
        log.info("WebSocket client disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}", exc_info=True)
        try:
            await ws.send_text(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass
    finally:
        executor.shutdown(wait=False)
