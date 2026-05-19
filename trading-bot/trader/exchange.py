"""
Exchange connector using ccxt.
Supports multiple exchanges with separate API credentials.

Env vars per exchange:
  BINANCE_API_KEY / BINANCE_SECRET
  BYBIT_API_KEY   / BYBIT_SECRET

Legacy fallback (single exchange):
  EXCHANGE_API_KEY / EXCHANGE_SECRET
"""

import ccxt
import os
from datetime import datetime

PAPER_TRADE = os.getenv("PAPER_TRADE", "true").lower() == "true"

# Per-exchange credential map
_CRED_MAP = {
    "binance":  ("BINANCE_API_KEY",  "BINANCE_SECRET"),
    "bybit":    ("BYBIT_API_KEY",    "BYBIT_SECRET"),
    "okx":      ("OKX_API_KEY",      "OKX_SECRET"),
    "kucoin":   ("KUCOIN_API_KEY",   "KUCOIN_SECRET"),
    "gate":     ("GATE_API_KEY",     "GATE_SECRET"),
    "mexc":     ("MEXC_API_KEY",     "MEXC_SECRET"),
    "htx":      ("HTX_API_KEY",      "HTX_SECRET"),
    "bitget":   ("BITGET_API_KEY",   "BITGET_SECRET"),
    "phemex":   ("PHEMEX_API_KEY",   "PHEMEX_SECRET"),
    "cryptocom":("CRYPTOCOM_API_KEY","CRYPTOCOM_SECRET"),
    "kraken":   ("KRAKEN_API_KEY",   "KRAKEN_SECRET"),
}

# Exchanges that work without API keys (public market data only)
_PUBLIC_EXCHANGES = {"bybit", "okx", "kucoin", "gate", "mexc", "htx", "bitget",
                     "phemex", "cryptocom", "kraken"}


def get_exchange(name: str = "binance") -> ccxt.Exchange:
    """Create and return an authenticated exchange instance."""
    exchange_class = getattr(ccxt, name)

    # Per-exchange keys take priority; fall back to generic keys
    key_env, sec_env = _CRED_MAP.get(name, ("EXCHANGE_API_KEY", "EXCHANGE_SECRET"))
    api_key = os.getenv(key_env) or os.getenv("EXCHANGE_API_KEY", "")
    secret  = os.getenv(sec_env) or os.getenv("EXCHANGE_SECRET", "")

    exchange = exchange_class({
        "apiKey":          api_key,
        "secret":          secret,
        "enableRateLimit": True,
        "options":         {"defaultType": "spot"},
    })

    if PAPER_TRADE:
        try:
            exchange.set_sandbox_mode(True)
        except Exception:
            pass  # not all exchanges have a sandbox

    return exchange


def get_all_exchanges() -> dict[str, ccxt.Exchange]:
    """
    Return all configured exchanges.
    Binance uses API keys from .env.
    Bybit, OKX, KuCoin connect without API keys (public data only).
    """
    primary = os.getenv("EXCHANGE_NAME", "binance")
    exchanges = {primary: get_exchange(primary)}

    for name in _PUBLIC_EXCHANGES:
        if name != primary:
            exchange_class = getattr(ccxt, name)
            exchanges[name] = exchange_class({
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            })

    return exchanges


def fetch_ohlcv(exchange: ccxt.Exchange, symbol: str,
                timeframe: str = "1h", limit: int = 100) -> list[dict]:
    """Fetch OHLCV candles and return as list of dicts."""
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    return [
        {
            "timestamp": datetime.utcfromtimestamp(c[0] / 1000).isoformat(),
            "open": c[1], "high": c[2], "low": c[3],
            "close": c[4], "volume": c[5],
        }
        for c in raw
    ]


def fetch_ticker(exchange: ccxt.Exchange, symbol: str) -> dict:
    return exchange.fetch_ticker(symbol)


def fetch_balance(exchange: ccxt.Exchange) -> dict:
    balance = exchange.fetch_balance()
    return {k: v for k, v in balance["total"].items() if v > 0}
