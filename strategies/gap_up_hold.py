"""
Gap Up Hold Strategy (GapUpHold / 跳空高开持续)

Signal conditions (all knowable at 14:57, no future data):
  1. open_rate > 2%  (gap up > 2%, open_rate = (open/preclose - 1)*100)
  2. close >= open   (price held above open, gap not filled)
  3. close_rate >= 3.0 AND close_rate < 9.5 (daily gain 3-9.5%, exclude limit-up)
  4. turn > 0 AND turn < 1.0 (turnover rate < 1%)
  5. volume * close > 10,000,000 (daily traded value > 10M, volume in shares)

Trade rules:
  Buy at D0 close, Sell at D+1 close, 1-day holding, T+1 compliant
  Rank by turn ASC, top-1
"""


class GapUpHold:
    """Gap up hold overnight strategy"""

    def __init__(self):
        self.open_rate_min = 2.0      # gap up > 2%
        self.close_rate_min = 3.0
        self.close_rate_max = 9.5
        self.turn_max = 1.0
        self.min_traded_value = 10_000_000  # volume * close > 10M

    @property
    def name(self):
        return "GapUpHold"

    def generate_signals(self, day_data):
        """Filter stocks matching signal conditions from daily DataFrame.

        Requires columns: open, preclose, close, close_rate, turn, volume
        """
        # Calculate open_rate: (open / preclose - 1) * 100
        open_rate = (day_data["open"] / day_data["preclose"] - 1) * 100

        # Calculate daily traded value (volume in shares)
        traded_value = day_data["volume"] * day_data["close"]

        mask = (
            (open_rate > self.open_rate_min) &
            (day_data["close"] >= day_data["open"]) &
            (day_data["close_rate"] >= self.close_rate_min) &
            (day_data["close_rate"] < self.close_rate_max) &
            (day_data["turn"] > 0) &
            (day_data["turn"] < self.turn_max) &
            (traded_value > self.min_traded_value)
        )
        return day_data.loc[mask].copy()

    def rank_signals(self, signals):
        """Rank by turn ASC, return top-1."""
        if signals is None or signals.empty:
            return signals
        return signals.nsmallest(1, "turn")
