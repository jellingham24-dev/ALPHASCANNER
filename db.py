"""SQLite layer. One table that stores the latest snapshot per coin.

Design: we don't store historical snapshots in v1 because CoinGecko already
gives us the % changes we need pre-computed. If you later want to do your
own indicator math (RSI, MACD, custom moving averages), add a `candles`
table and pull OHLCV from /coins/{id}/market_chart.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "crypto.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS coin_snapshots (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    image TEXT,
    market_cap_rank INTEGER,
    current_price REAL,
    market_cap REAL,
    total_volume REAL,
    pct_1h REAL,
    pct_24h REAL,
    pct_7d REAL,
    pct_30d REAL,
    pct_1y REAL,
    score REAL,
    aligned INTEGER,
    last_updated TEXT
);
CREATE INDEX IF NOT EXISTS idx_score ON coin_snapshots(score DESC);
CREATE INDEX IF NOT EXISTS idx_rank ON coin_snapshots(market_cap_rank);
"""


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def upsert_coins(coins: list[dict]) -> None:
    """Insert or replace coin snapshots. `coins` must include score & aligned."""
    if not coins:
        return
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO coin_snapshots
            (id, symbol, name, image, market_cap_rank, current_price, market_cap,
             total_volume, pct_1h, pct_24h, pct_7d, pct_30d, pct_1y,
             score, aligned, last_updated)
            VALUES (:id, :symbol, :name, :image, :market_cap_rank, :current_price,
                    :market_cap, :total_volume, :pct_1h, :pct_24h, :pct_7d,
                    :pct_30d, :pct_1y, :score, :aligned, :last_updated)
            """,
            coins,
        )
        conn.commit()


def get_top_by_score(limit: int = 50, direction: str = "desc") -> list[dict]:
    order = "DESC" if direction == "desc" else "ASC"
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM coin_snapshots ORDER BY score {order} LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_coin(coin_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM coin_snapshots WHERE id = ?", (coin_id,)
        ).fetchone()
        return dict(row) if row else None


def count_coins() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM coin_snapshots").fetchone()[0]
