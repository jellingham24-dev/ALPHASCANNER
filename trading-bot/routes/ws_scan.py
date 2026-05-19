"""
ws_scan.py — WebSocket endpoint that streams live scan results.

Connect from the UI at: ws://localhost:8000/ws/scan

Message types sent to client:
  { type: "start",    total: 1000 }
  { type: "result",   symbol, signal, confidence, price, votes, rank, done: N }
  { type: "complete", total_scanned, signals: [...], elapsed_seconds }
  { type: "error",    message }

Send from client to control:
  { action: "start", direction: "BUY"|"SELL"|"ALL", limit: 100 }
  { action: "stop" }
"""

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from trader.bot      import wl_manager, scanner, exchange
from trader.exchange import fetch_ohlcv
from trader.signals  import compute_signal

log    = logging.getLogger("ws_scan")
router = APIRouter()


def _scan_one_ws(symbol: str, timeframe: str, limit: int) -> dict:
    """Scan a single symbol and return a serialisable result dict."""
    try:
        scanner._rate.wait()
        candles = fetch_ohlcv(exchange, symbol, timeframe, limit)
        result  = compute_signal(candles)

        votes = {
            name: {
                "vote":   data["vote"],
                "weight": round(data["weight"], 3),
                "reason": data["reason"],
            }
            for name, data in result.score_breakdown.items()
        }

        return {
            "symbol":     symbol,
            "signal":     result.signal,
            "confidence": round(result.confidence, 3),
            "price":      result.price,
            "votes":      votes,
            "error":      None,
        }
    except Exception as e:
        return {
            "symbol":     symbol,
            "signal":     "HOLD",
            "confidence": 0,
            "price":      0,
            "votes":      {},
            "error":      str(e),
        }


@router.websocket("/ws/scan")
async def websocket_scan(ws: WebSocket):
    await ws.accept()
    log.info("WebSocket client connected")

    active = False
    executor = ThreadPoolExecutor(max_workers=scanner.workers)

    try:
        while True:
            # Wait for a start command
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                await ws.send_text(json.dumps({"type": "error", "message": "Invalid JSON"}))
                continue

            action = msg.get("action")

            if action == "stop":
                active = False
                continue

            if action != "start":
                continue

            # ── Start scan ────────────────────────────────────────────────
            direction = msg.get("direction", "ALL").upper()
            limit     = int(msg.get("limit", 1000))
            timeframe = msg.get("timeframe", scanner.timeframe)
            candle_n  = int(msg.get("candle_limit", scanner.candle_limit))

            watchlist = wl_manager.get()[:limit]
            total     = len(watchlist)
            active    = True

            await ws.send_text(json.dumps({"type": "start", "total": total}))
            log.info(f"WS scan started: {total} symbols, direction={direction}")

            done     = 0
            signals  = []
            start_ts = time.monotonic()

            loop = asyncio.get_event_loop()

            # Submit all in batches of 100, stream results as they finish
            BATCH = 100
            for batch_start in range(0, total, BATCH):
                if not active:
                    break

                batch = watchlist[batch_start : batch_start + BATCH]

                futures = {
                    executor.submit(_scan_one_ws, sym, timeframe, candle_n): sym
                    for sym in batch
                }

                # Stream each result as it completes
                for future in as_completed(futures):
                    if not active:
                        break

                    result = await loop.run_in_executor(None, future.result)
                    done  += 1

                    # Filter by direction if requested
                    passes = (
                        direction == "ALL"
                        or result["signal"] == direction
                    )

                    payload = {
                        "type":       "result",
                        "done":       done,
                        "total":      total,
                        **result,
                    }

                    # Always send — UI decides what to show
                    await ws.send_text(json.dumps(payload))

                    if result["signal"] != "HOLD" and result["confidence"] >= 0.55:
                        signals.append(result)

                # Yield to event loop between batches
                await asyncio.sleep(0)

            elapsed = round(time.monotonic() - start_ts, 1)
            await ws.send_text(json.dumps({
                "type":            "complete",
                "total_scanned":   done,
                "elapsed_seconds": elapsed,
                "signals":         signals,
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
