"""FastAPI app exposing the trend scanner over JSON.

Run locally:
    uvicorn main:app --reload --port 8000

Then:
    curl http://localhost:8000/rankings
    curl http://localhost:8000/rankings?direction=bearish&aligned_only=true
    curl http://localhost:8000/coin/bitcoin
    curl -X POST http://localhost:8000/refresh

Open docs:
    http://localhost:8000/docs
"""
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from db import count_coins, get_coin, get_top_by_score, init_db
from refresh import refresh as do_refresh


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Crypto Trend Scanner",
    description="Multi-timeframe trend scoring for the crypto market.",
    version="0.1.0",
    lifespan=lifespan,
)

# Wide-open CORS for development. Tighten this before exposing publicly:
# replace allow_origins=["*"] with the URL of your mobile app's dev server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "service": "crypto-trend-scanner",
        "version": "0.1.0",
        "coins_in_db": count_coins(),
        "endpoints": ["/rankings", "/coin/{id}", "/refresh", "/docs"],
        "disclaimer": (
            "This is an educational technical-analysis tool. The signals are "
            "computed from public market data; they are not financial advice "
            "and should not be the sole basis for trading decisions."
        ),
    }


@app.get("/rankings")
def rankings(
    direction: Literal["bullish", "bearish"] = "bullish",
    limit: int = Query(50, ge=1, le=250),
    aligned_only: bool = Query(False, description="Only return coins where all timeframes agree in sign"),
):
    """Top coins by trend score.

    - bullish: highest scores (strongest uptrends)
    - bearish: lowest scores (strongest downtrends)
    - aligned_only: high-conviction signals only
    """
    coins = get_top_by_score(
        limit=limit, direction="desc" if direction == "bullish" else "asc"
    )
    if aligned_only:
        coins = [c for c in coins if c["aligned"]]
    return {"direction": direction, "count": len(coins), "coins": coins}


@app.get("/coin/{coin_id}")
def coin_detail(coin_id: str):
    coin = get_coin(coin_id)
    if not coin:
        raise HTTPException(
            status_code=404,
            detail=f"Coin '{coin_id}' not found. Run a refresh first, or check the ID (CoinGecko slugs, e.g. 'bitcoin', 'ethereum').",
        )
    return coin


@app.post("/refresh")
async def refresh_endpoint(pages: int = Query(1, ge=1, le=4)):
    """Trigger a refresh manually. In production, run on a cron schedule instead."""
    count = await do_refresh(pages=pages)
    return {"status": "ok", "coins_refreshed": count}
