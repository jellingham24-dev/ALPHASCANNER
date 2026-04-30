# Crypto Trend Scanner — Backend

A FastAPI service that scans the crypto market on a schedule, scores every
coin's trend across 5 timeframes (1h, 24h, 7d, 30d, 1y), and exposes a JSON
API so a mobile app (or anything else) can read the rankings.

## Design at a glance

```
┌────────────────┐    every 15 min    ┌──────────────────┐
│  CoinGecko API │ ───────────────▶   │  refresh.py      │
└────────────────┘                    │  fetch + score   │
                                      └────────┬─────────┘
                                               │ writes
                                               ▼
                                      ┌──────────────────┐
                                      │  SQLite (crypto.db) │
                                      └────────┬─────────┘
                                               │ reads
                                               ▼
┌────────────────┐    HTTP/JSON       ┌──────────────────┐
│  Mobile app    │ ◀──────────────────│  FastAPI (main.py)│
└────────────────┘                    └──────────────────┘
```

The phone never calls CoinGecko directly. The backend caches the latest
snapshot of the whole market in SQLite, so the mobile UI is fast and you
stay well inside the free-tier rate limit.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

**1. Pull the first batch of data:**

```bash
python refresh.py                  # top 250 coins
python refresh.py --pages 4        # top 1000 coins
```

You'll see a summary of the top 5 bullish and bearish coins printed to the
terminal. This is the fastest way to validate the scoring logic before
touching the API.

**2. Start the API:**

```bash
uvicorn main:app --reload --port 8000
```

**3. Hit the endpoints:**

```bash
curl http://localhost:8000/rankings
curl "http://localhost:8000/rankings?direction=bearish&aligned_only=true&limit=20"
curl http://localhost:8000/coin/bitcoin
curl -X POST http://localhost:8000/refresh
```

Interactive docs: <http://localhost:8000/docs>

**4. Schedule refreshes (production):**

```cron
*/15 * * * * cd /path/to/project && /path/to/.venv/bin/python refresh.py
```

## How the scoring works

For each coin, every percent-change is squashed through `tanh(pct / scale)`
to a smooth signal in (-1, +1). The 5 squashed signals are averaged with
weights that favor longer timeframes. If every timeframe agrees in sign,
a 1.25× alignment bonus kicks in (clamped to ±1). The result:

| Score range  | Meaning                                |
| ------------ | -------------------------------------- |
| +0.7 to +1.0 | Strong, sustained uptrend              |
| +0.3 to +0.7 | Moderate uptrend                       |
| −0.3 to +0.3 | Mixed / flat                           |
| −0.7 to −0.3 | Moderate downtrend                     |
| −1.0 to −0.7 | Strong, sustained downtrend            |

The `aligned` flag separates "every timeframe agrees" (high conviction)
from "the average happens to land here" (mixed signals canceling out).

Tune `SCALES`, `WEIGHTS`, and `ALIGNMENT_BONUS` in `scorer.py` to taste.

## File layout

```
crypto-trend-backend/
├── main.py            FastAPI app and HTTP endpoints
├── refresh.py         Standalone data refresh job
├── fetcher.py         CoinGecko API client
├── scorer.py          Trend scoring (the opinionated part)
├── db.py              SQLite schema + queries
├── requirements.txt
└── README.md
```

## Why no 4hr by default?

CoinGecko's `/coins/markets` endpoint returns 1h, 24h, 7d, 30d, and 1y
percent changes in a single call — that's how we scan the whole market in
one API request. It does not return a 4hr field. To get true 4hr signals
you'd hit `/coins/{id}/market_chart` per coin, which means hundreds of
extra API calls.

There's a stub for `fetch_4h_change()` in `fetcher.py`. Recommended
approach: run the existing scan first, then fetch 4hr data only for the
top ~30 candidates and refine their scores. That keeps the free tier happy.

## What to add next

- **Real candle indicators**: pull OHLCV from `/coins/{id}/market_chart`,
  store in a `candles` table, compute RSI, MACD, EMAs. Use `pandas-ta`.
- **Volume confirmation**: a price move on rising volume scores higher
  than the same move on falling volume.
- **Volatility adjustment**: penalize coins where short-term swings dwarf
  the long-term trend (lots of noise, weak signal).
- **Backtest harness**: replay historical snapshots and check whether
  high-score coins outperformed lower-score ones over the next N days.
- **Authentication**: when you expose this beyond localhost, add an API
  key and tighten the CORS `allow_origins`.
- **Hosting**: Render, Railway, or Fly.io free tiers all run this fine.
  Use Postgres instead of SQLite if you need concurrent writes.

## Important: this is not financial advice

This service computes statistical signals from public market data. Crypto
is volatile, technical signals fail constantly, and past trends do not
predict future returns. Treat the rankings as a research tool that points
your attention at coins worth investigating — not a buy/sell recommender.
The `disclaimer` field on the `/` endpoint is there for a reason; keep
something similar visible in the mobile UI.
