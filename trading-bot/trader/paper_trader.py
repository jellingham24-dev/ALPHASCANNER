"""
Paper trader — simulates order execution in-memory.
No real money. Perfect for testing your strategy.

Switch to live by replacing execute_order() with real ccxt calls.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from trader.risk import TradeOrder


@dataclass
class Position:
    symbol: str
    side: str
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_at: str
    closed_at: Optional[str] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    status: str = "open"   # open | closed_tp | closed_sl | closed_manual


@dataclass
class PaperTrader:
    starting_balance: float = 10_000.0
    balance: float = field(init=False)
    positions: list[Position] = field(default_factory=list)
    trade_log: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.balance = self.starting_balance

    # ── Opening a position ────────────────────────────────────────────────

    def execute_order(self, order: TradeOrder) -> dict:
        cost = order.quantity * order.entry_price
        if cost > self.balance:
            return {"ok": False, "error": "Insufficient balance"}

        self.balance -= cost
        position = Position(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            entry_price=order.entry_price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            opened_at=datetime.utcnow().isoformat(),
        )
        self.positions.append(position)

        log = {
            "event": "open",
            "symbol": order.symbol,
            "side": order.side,
            "qty": order.quantity,
            "price": order.entry_price,
            "sl": order.stop_loss,
            "tp": order.take_profit,
            "reason": order.reason,
            "balance_after": self.balance,
            "ts": position.opened_at,
        }
        self.trade_log.append(log)
        return {"ok": True, "position": position, "log": log}

    # ── Checking SL/TP on each tick ──────────────────────────────────────

    def update_positions(self, symbol: str, current_price: float) -> list[dict]:
        """Call this on each new candle to check stop-loss / take-profit."""
        events = []
        for pos in self.positions:
            if pos.status != "open" or pos.symbol != symbol:
                continue

            hit_sl = current_price <= pos.stop_loss if pos.side == "buy" else current_price >= pos.stop_loss
            hit_tp = current_price >= pos.take_profit if pos.side == "buy" else current_price <= pos.take_profit

            if hit_tp or hit_sl:
                exit_price = pos.take_profit if hit_tp else pos.stop_loss
                gross = exit_price * pos.quantity
                cost = pos.entry_price * pos.quantity
                pnl = gross - cost if pos.side == "buy" else cost - gross

                self.balance += gross
                pos.exit_price = exit_price
                pos.pnl = round(pnl, 4)
                pos.closed_at = datetime.utcnow().isoformat()
                pos.status = "closed_tp" if hit_tp else "closed_sl"

                event = {
                    "event": "close",
                    "symbol": symbol,
                    "reason": "TP" if hit_tp else "SL",
                    "pnl": pos.pnl,
                    "balance_after": self.balance,
                    "ts": pos.closed_at,
                }
                self.trade_log.append(event)
                events.append(event)

        return events

    # ── Adjust live position TP/SL ───────────────────────────────────────

    def adjust_levels(
        self,
        symbol:     str,
        stop_loss:  float | None = None,
        take_profit: float | None = None,
    ) -> dict:
        """
        Move the SL and/or TP on an open position.
        Validates that:
          - SL is below entry for BUY, above for SELL
          - TP is above entry for BUY, below for SELL
          - SL and TP don't cross each other
        Returns {"ok": True} or {"ok": False, "error": "..."}
        """
        pos = next((p for p in self.positions if p.symbol == symbol and p.status == "open"), None)
        if not pos:
            return {"ok": False, "error": f"No open position for {symbol}"}

        new_sl = stop_loss  if stop_loss  is not None else pos.stop_loss
        new_tp = take_profit if take_profit is not None else pos.take_profit

        # Validate direction
        if pos.side == "buy":
            if new_sl >= pos.entry_price:
                return {"ok": False, "error": f"SL {new_sl} must be below entry {pos.entry_price} for BUY"}
            if new_tp <= pos.entry_price:
                return {"ok": False, "error": f"TP {new_tp} must be above entry {pos.entry_price} for BUY"}
        else:
            if new_sl <= pos.entry_price:
                return {"ok": False, "error": f"SL {new_sl} must be above entry {pos.entry_price} for SELL"}
            if new_tp >= pos.entry_price:
                return {"ok": False, "error": f"TP {new_tp} must be below entry {pos.entry_price} for SELL"}

        if pos.side == "buy" and new_sl >= new_tp:
            return {"ok": False, "error": "SL must be below TP"}
        if pos.side == "sell" and new_sl <= new_tp:
            return {"ok": False, "error": "SL must be above TP for SELL"}

        old = {"sl": pos.stop_loss, "tp": pos.take_profit}
        pos.stop_loss   = round(new_sl, 6)
        pos.take_profit = round(new_tp, 6)

        log_entry = {
            "event":  "adjust",
            "symbol": symbol,
            "old_sl": old["sl"], "new_sl": pos.stop_loss,
            "old_tp": old["tp"], "new_tp": pos.take_profit,
        }
        self.trade_log.append(log_entry)
        return {"ok": True, "position": pos, "log": log_entry}

    # ── Reset ─────────────────────────────────────────────────────────────

    def reset(self) -> dict:
        """Clear all positions and restore the starting balance."""
        self.balance   = self.starting_balance
        self.positions = []
        self.trade_log = []
        return {"ok": True, "balance": self.balance}

    # ── Stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        closed = [p for p in self.positions if p.status != "open"]
        wins = [p for p in closed if (p.pnl or 0) > 0]
        total_pnl = sum(p.pnl or 0 for p in closed)
        return {
            "starting_balance": self.starting_balance,
            "current_balance": round(self.balance, 2),
            "total_pnl": round(total_pnl, 2),
            "return_pct": round((self.balance - self.starting_balance) / self.starting_balance * 100, 2),
            "total_trades": len(closed),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "open_positions": len([p for p in self.positions if p.status == "open"]),
        }
