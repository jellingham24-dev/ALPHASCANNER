"""CoinGecko API client.

We use the /coins/markets endpoint, which is special: in a SINGLE call it
returns the top 250 coins WITH their 1h, 24h, 7d, 30d, and 1y percent
changes already computed. That's the foundation of the whole-market scan
working on the free tier.

NOTE on 4hr: /coins/markets does not return 4hr changes. To compute those,
you'd hit /coins/{id}/market_chart per coin (1 call each = expensive).
The current scoring uses 1h as the short-timeframe signal; if you want
true 4hr, fetch hourly candles only for the top N candidates after scoring
and add a `pct_4h` field. There's a stub for that at the bottom of this file.
"""
import asyncio
from datetime import datetime, timezone

import httpx

COINGECKO_BASE = "https://api.coingecko.com/api/v3"


async def fetch_markets(pages: int = 1, vs_currency: str = "usd") -> list[dict]:
    """Fetch the top `pages * 250` coins by market cap with percent changes.

    Free tier rate limit is roughly 10-30 req/min. We sleep 2s between pages
    to stay polite. For a whole-market scan, pages=4 = top 1000 coins.
    """
    url = f"{COINGECKO_BASE}/coins/markets"
    params = {
        "vs_currency": vs_currency,
        "order": "market_cap_desc",
        "per_page": 250,
        "sparkline": "false",
        "price_change_percentage": "1h,24h,7d,30d,1y",
    }
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for page in range(1, pages + 1):
            params["page"] = page
            r = await client.get(url, params=params)
            r.raise_for_status()
            out.extend(r.json())
            if page < pages:
                await asyncio.sleep(2)
    return out


def normalize_coin(raw: dict) -> dict:
    """Translate a raw CoinGecko coin dict into our internal shape."""
    return {
        "id": raw["id"],
        "symbol": (raw.get("symbol") or "").lower(),
        "name": raw.get("name") or "",
        "image": raw.get("image"),
        "market_cap_rank": raw.get("market_cap_rank"),
        "current_price": raw.get("current_price"),
        "market_cap": raw.get("market_cap"),
        "total_volume": raw.get("total_volume"),
        "pct_1h": raw.get("price_change_percentage_1h_in_currency"),
        "pct_24h": raw.get("price_change_percentage_24h_in_currency"),
        "pct_7d": raw.get("price_change_percentage_7d_in_currency"),
        "pct_30d": raw.get("price_change_percentage_30d_in_currency"),
        "pct_1y": raw.get("price_change_percentage_1y_in_currency"),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# --- 4hr extension stub ---------------------------------------------------
# Uncomment and call this on top-scoring coins if you want true 4hr signals.
#
# async def fetch_4h_change(coin_id: str) -> float | None:
#     """Compute % change over the last 4 hours from hourly candles."""
#     url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart"
#     params = {"vs_currency": "usd", "days": 1, "interval": "hourly"}
#     async with httpx.AsyncClient(timeout=30) as client:
#         r = await client.get(url, params=params)
#         r.raise_for_status()
#         prices = r.json().get("prices", [])
#     if len(prices) < 5:
#         return None
#     now_price = prices[-1][1]
#     four_hr_ago = prices[-5][1]
#     return ((now_price - four_hr_ago) / four_hr_ago) * 100
