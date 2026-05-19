"""
Risk management layer.

Controls:
- How much to risk per trade (% of portfolio)
- Stop-loss and take-profit levels
- Maximum open positions
- Daily drawdown kill-switch

TP/SL can be adjusted:
- Globally via RiskConfig (applies to all new trades)
- Per-position via PaperTrader.adjust_levels() (live positions only)
"""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class RiskConfig:
    risk_per_trade:    float = 0.02   # 2% of portfolio per trade
    stop_loss_pct:     float = 0.03   # 3% stop-loss
    take_profit_pct:   float = 0.06   # 6% take-profit (2:1 RR)
    max_open_positions: int  = 3
    max_daily_loss_pct: float = 0.05  # halt if down 5% today

    def update(self, **kwargs) -> "RiskConfig":
        """Return a new RiskConfig with updated fields (immutable update pattern)."""
        valid = {f for f in self.__dataclass_fields__}
        for k, v in kwargs.items():
            if k not in valid:
                raise ValueError(f"Unknown config field: {k}")
            # Clamp percentages to sane ranges
            if k in ("risk_per_trade", "stop_loss_pct", "take_profit_pct", "max_daily_loss_pct"):
                v = max(0.001, min(float(v), 0.50))
            if k == "max_open_positions":
                v = max(1, min(int(v), 50))
            setattr(self, k, v)
        return self

    @property
    def risk_reward_ratio(self) -> float:
        return round(self.take_profit_pct / self.stop_loss_pct, 2)

    def as_dict(self) -> dict:
        return {
            "risk_per_trade":     self.risk_per_trade,
            "stop_loss_pct":      self.stop_loss_pct,
            "take_profit_pct":    self.take_profit_pct,
            "max_open_positions": self.max_open_positions,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "risk_reward_ratio":  self.risk_reward_ratio,
        }


@dataclass
class TradeOrder:
    symbol:      str
    side:        str    # "buy" or "sell"
    quantity:    float
    entry_price: float
    stop_loss:   float
    take_profit: float
    reason:      str


@dataclass
class RiskManager:
    config:              RiskConfig = field(default_factory=RiskConfig)
    open_positions:      int        = 0
    daily_start_balance: float      = 0.0
    daily_pnl:           float      = 0.0
    _today:              date       = field(default_factory=date.today)

    def reset_daily_if_needed(self) -> None:
        today = date.today()
        if today != self._today:
            self.daily_pnl = 0.0
            self._today    = today

    def should_halt(self, current_balance: float) -> tuple[bool, str]:
        self.reset_daily_if_needed()
        if self.daily_start_balance > 0:
            daily_loss = (self.daily_start_balance - current_balance) / self.daily_start_balance
            if daily_loss >= self.config.max_daily_loss_pct:
                return True, f"Daily loss limit hit: {daily_loss:.1%}"
        if self.open_positions >= self.config.max_open_positions:
            return True, f"Max open positions: {self.open_positions}"
        return False, ""

    def size_order(
        self,
        symbol:          str,
        side:            str,
        entry_price:     float,
        portfolio_value: float,
        reason:          str,
        sl_pct:          float | None = None,   # override global SL %
        tp_pct:          float | None = None,   # override global TP %
    ) -> TradeOrder:
        """
        Calculate position size and SL/TP levels.
        sl_pct / tp_pct override the global config for this trade only.
        """
        sl = sl_pct if sl_pct is not None else self.config.stop_loss_pct
        tp = tp_pct if tp_pct is not None else self.config.take_profit_pct

        risk_amount   = portfolio_value * self.config.risk_per_trade
        stop_distance = entry_price * sl
        quantity      = risk_amount / stop_distance if stop_distance > 0 else 0

        if side == "buy":
            stop_loss   = entry_price * (1 - sl)
            take_profit = entry_price * (1 + tp)
        else:
            stop_loss   = entry_price * (1 + sl)
            take_profit = entry_price * (1 - tp)

        return TradeOrder(
            symbol      = symbol,
            side        = side,
            quantity    = round(quantity, 6),
            entry_price = entry_price,
            stop_loss   = round(stop_loss,   4),
            take_profit = round(take_profit, 4),
            reason      = reason,
        )
