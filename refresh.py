"""Refresh the database with the latest market snapshot.

Run manually:
    python refresh.py
    python refresh.py --pages 4   # top 1000 coins instead of top 250

Run on a schedule (recommended for production):
    crontab -e
    */15 * * * * cd /path/to/project && /path/to/venv/bin/python refresh.py

Or use APScheduler inside the FastAPI app if you prefer one process.
"""
import argparse
import asyncio
import sys

from db import init_db, upsert_coins
from fetcher import fetch_markets, normalize_coin
from scorer import score_all


async def refresh(pages: int = 1) -> int:
    raw = await fetch_markets(pages=pages)
    coins = [normalize_coin(r) for r in raw]
    coins = score_all(coins)
    init_db()
    upsert_coins(coins)
    return len(coins)


def _print_summary(count: int) -> None:
    from db import get_top_by_score

    print(f"Refreshed {count} coins.")
    print("\nTop 5 bullish:")
    for c in get_top_by_score(5, "desc"):
        flag = " *" if c["aligned"] else "  "
        print(f"  {c['symbol'].upper():>6}{flag} score={c['score']:+.3f}")
    print("\nTop 5 bearish:")
    for c in get_top_by_score(5, "asc"):
        flag = " *" if c["aligned"] else "  "
        print(f"  {c['symbol'].upper():>6}{flag} score={c['score']:+.3f}")
    print("\n* = all timeframes aligned (high-conviction signal)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pages",
        type=int,
        default=1,
        help="Number of 250-coin pages (1=top 250, 4=top 1000). Default 1.",
    )
    args = parser.parse_args()
    try:
        n = asyncio.run(refresh(pages=args.pages))
        _print_summary(n)
    except Exception as e:
        print(f"Refresh failed: {e}", file=sys.stderr)
        sys.exit(1)
