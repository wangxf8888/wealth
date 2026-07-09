"""
Low Turnover Momentum Strategy (LowTurnoverMomentum)

Signal conditions:
  Primary (S1):   5%% <= close_rate < 6%%, turn < 0.7%%, amount > 10M
  Extended (S1-E): 5%% <= close_rate < 6%%, turn < 1.0%%, amount > 10M

Trade rules:
  Buy at D0 close, Sell at D+1 close, 1-day holding, T+1 compliant
  Rank by turn ASC, top-1
"""


class LowTurnoverMomentum:
    """Low turnover momentum overnight strategy"""

    MODES = {
        "primary":  {"turn_max": 0.7, "label": "S1"},
        "extended": {"turn_max": 1.0, "label": "S1-E"},
    }

    def __init__(self, mode="primary"):
        if mode not in self.MODES:
            raise ValueError(f"Unknown mode: {mode}. Use primary or extended.")
        self.mode = mode
        cfg = self.MODES[mode]
        self.turn_max = cfg["turn_max"]
        self.label = cfg["label"]
        self.close_rate_min = 5.0
        self.close_rate_max = 6.0
        self.min_amount = 10_000_000

    @property
    def name(self):
        return f"LowTurnoverMomentum({self.label})"

    def generate_signals(self, day_data):
        """Filter stocks matching signal conditions from daily DataFrame."""
        mask = (
            (day_data["close_rate"] >= self.close_rate_min) &
            (day_data["close_rate"] < self.close_rate_max) &
            (day_data["turn"] > 0) &
            (day_data["turn"] < self.turn_max) &
            (day_data["amount"] > self.min_amount)
        )
        return day_data.loc[mask].copy()

    def rank_signals(self, signals):
        """Rank by turn ASC, return top-1."""
        if signals is None or signals.empty:
            return signals
        return signals.nsmallest(1, "turn")

    def generate_signals_pure(self, rows):
        """Pure Python version for list[dict] input."""
        result = []
        for r in rows:
            cr = r.get("close_rate") or 0
            t = r.get("turn") or 0
            amt = r.get("amount") or 0
            if (self.close_rate_min <= cr < self.close_rate_max
                    and 0 < t < self.turn_max
                    and amt > self.min_amount):
                result.append(r)
        return result

    def rank_signals_pure(self, signals):
        """Pure Python rank by turn ASC, top-1."""
        if not signals:
            return []
        signals_sorted = sorted(signals, key=lambda x: x.get("turn", 999))
        return signals_sorted[:1]
