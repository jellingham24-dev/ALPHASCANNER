"""
watchlist.py — Dynamic top-1000 coin watchlist.

Flow:
  1. Fetch top 1000 coins by market cap from CoinGecko (free, no API key needed)
  2. Load all USDT markets available on the exchange
  3. Intersect: only scan coins that exist on our exchange
  4. Refresh the list every REFRESH_HOURS (default 6h) so new entrants are picked up

CoinGecko free tier: 10-50 calls/min. We only call it once per refresh cycle.
"""

import time
import logging
import urllib.request
import urllib.error
import json
import pathlib
from typing import Optional

import ccxt

log = logging.getLogger("watchlist")

COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd&order=market_cap_desc"
    "&per_page=250&page={page}&sparkline=false"
    "&price_change_percentage=1h,24h"
)

REFRESH_HOURS  = 6
TOP_N          = 1000
MIN_VOLUME_24H = 500_000
STABLECOINS    = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "FRAX",
    "USDP", "GUSD", "LUSD", "SUSD", "USDD", "FDUSD",
}

_CACHE_FILE = pathlib.Path("coingecko_cache.json")


def _fetch_coingecko_page(page: int) -> list[dict]:
    """Fetch one page (250 coins) from CoinGecko markets endpoint."""
    url = COINGECKO_URL.format(page=page)
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            log.warning("CoinGecko rate limit hit — waiting 60s")
            time.sleep(60)
            return _fetch_coingecko_page(page)   # retry once
        log.error(f"CoinGecko HTTP error: {e.code}")
        return []
    except Exception as e:
        log.error(f"CoinGecko fetch failed: {e}")
        return []


def fetch_top_coins(n: int = TOP_N, min_volume: float = MIN_VOLUME_24H) -> list[dict]:
    """
    Returns up to n coins sorted by market cap.
    Results are cached to disk for REFRESH_HOURS to avoid CoinGecko rate limits.
    """
    # Check disk cache first
    if _CACHE_FILE.exists():
        age_hours = (time.time() - _CACHE_FILE.stat().st_mtime) / 3600
        if age_hours < REFRESH_HOURS:
            try:
                cached = json.loads(_CACHE_FILE.read_text())
                log.info(f"Using cached CoinGecko data ({age_hours:.1f}h old)")
                return [c for c in cached if c.get("volume_24h", 0) >= min_volume][:n]
            except Exception:
                pass  # fall through to fresh fetch
    coins = []
    pages_needed = (n + 249) // 250   # ceil(n / 250)

    for page in range(1, pages_needed + 1):
        log.info(f"Fetching CoinGecko page {page}/{pages_needed}...")
        data = _fetch_coingecko_page(page)
        if not data:
            break

        for coin in data:
            symbol = (coin.get("symbol") or "").upper()
            if symbol in STABLECOINS:
                continue
            vol = coin.get("total_volume") or 0
            if vol < min_volume:
                continue
            coins.append({
                "symbol":       symbol,
                "name":         coin.get("name", ""),
                "rank":         coin.get("market_cap_rank", 9999),
                "price_usd":    coin.get("current_price", 0),
                "volume_24h":   vol,
                "change_1h":    coin.get("price_change_percentage_1h_in_currency", 0) or 0,
                "change_24h":   coin.get("price_change_percentage_24h", 0) or 0,
            })

        if len(coins) >= n:
            break

        if page < pages_needed:
            time.sleep(1.5)

    # Save to disk cache
    try:
        _CACHE_FILE.write_text(json.dumps(coins))
        log.info(f"CoinGecko data cached to {_CACHE_FILE}")
    except Exception as e:
        log.warning(f"Could not write cache: {e}")

    return coins[:n]


def get_exchange_symbols(exchange: ccxt.Exchange, quote: str = "USDT") -> set[str]:
    """Return all base symbols available as {BASE}/USDT on this exchange."""
    try:
        markets = exchange.load_markets()
        return {
            m.split("/")[0]
            for m in markets
            if m.endswith(f"/{quote}") and markets[m].get("active", True)
        }
    except Exception as e:
        log.error(f"Could not load markets: {e}")
        return set()


def get_exchange_symbols_union(exchanges: dict, quote: str = "USDT") -> set[str]:
    """Return union of base symbols available as {BASE}/USDT across all exchanges."""
    union: set[str] = set()
    for name, ex in exchanges.items():
        syms = get_exchange_symbols(ex, quote)
        log.info(f"{name}: {len(syms)} {quote} symbols")
        union |= syms
    log.info(f"Union across {len(exchanges)} exchanges: {len(union)} unique symbols")
    return union


def build_watchlist(
    exchange,                          # ccxt.Exchange or dict of exchanges
    top_n: int = TOP_N,
    quote: str = "USDT",
    min_volume: float = MIN_VOLUME_24H,
) -> list[str]:
    """
    Returns a list of trading pairs like ['BTC/USDT', 'ETH/USDT', ...]
    filtered to coins that:
      - Are in CoinGecko's top N by market cap
      - Have > min_volume 24h USD volume
      - Are tradeable on at least one of the supplied exchanges
    """
    log.info(f"Building watchlist: top {top_n} coins vs {quote}...")

    top_coins = fetch_top_coins(top_n, min_volume)

    if isinstance(exchange, dict):
        available_set = get_exchange_symbols_union(exchange, quote)
    else:
        available_set = get_exchange_symbols(exchange, quote)

    watchlist = []
    skipped   = 0
    for coin in top_coins:
        sym = coin["symbol"]
        if sym in available_set:
            watchlist.append(f"{sym}/{quote}")
        else:
            skipped += 1

    log.info(
        f"Watchlist ready: {len(watchlist)} pairs "
        f"({skipped} top-{top_n} coins not on any exchange)"
    )
    return watchlist


# ── Cached watchlist manager ──────────────────────────────────────────────────

class WatchlistManager:
    """
    Wraps build_watchlist() with a time-based cache.
    Call .get() on every bot tick — it only refreshes every REFRESH_HOURS.
    """

    def __init__(
        self,
        exchange: ccxt.Exchange,
        top_n: int = TOP_N,
        refresh_hours: float = REFRESH_HOURS,
        quote: str = "USDT",
        min_volume: float = MIN_VOLUME_24H,
    ):
        self.exchange      = exchange
        self.top_n         = top_n
        self.refresh_secs  = refresh_hours * 3600
        self.quote         = quote
        self.min_volume    = min_volume

        self._watchlist: list[str]   = []
        self._last_refresh: float    = 0.0

    def get(self) -> list[str]:
        """Return cached watchlist, refreshing if stale."""
        if time.time() - self._last_refresh > self.refresh_secs or not self._watchlist:
            self._watchlist    = build_watchlist(
                self.exchange, self.top_n, self.quote, self.min_volume
            )
            self._last_refresh = time.time()
        return self._watchlist

    @property
    def last_refresh_age_minutes(self) -> float:
        return (time.time() - self._last_refresh) / 60

    def force_refresh(self) -> list[str]:
        self._last_refresh = 0
        return self.get()
