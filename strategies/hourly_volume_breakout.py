"""
Hourly Volume Breakout Strategy (H)

Signal conditions (all observable at 14:00, no future data):
  1. hour1_volume / (hour1_volume + hour2_volume + hour3_volume) > 0.6
  2. hour1_close_rate > 4.0 (first hour close vs preclose > 4%)
  3. turn < 1.0 (daily turnover rate < 1%)
  4. volume * close > 10,000,000 (daily trade amount > 10M, volume in shares)
  5. close_rate >= 4.0 AND close_rate < 9.5 (exclude limit-up)

Trade rules:
  Buy at D0 close (14:57 auction), Sell at D+1 close
  Rank by turn ASC, top-1
  T+1 compliant
"""


class HourlyVolumeBreakout:
    """Early morning volume breakout overnight strategy"""

    def __init__(self, volume_ratio_min=0.6, hour1_rate_min=4.0,
                 turn_max=1.0, min_trade_amount=10_000_000,
                 close_rate_min=4.0, close_rate_max=9.5):
        self.volume_ratio_min = volume_ratio_min
        self.hour1_rate_min = hour1_rate_min
        self.turn_max = turn_max
        self.min_trade_amount = min_trade_amount
        self.close_rate_min = close_rate_min
        self.close_rate_max = close_rate_max

    @property
    def name(self):
        return "HourlyVolumeBreakout(H)"

    def generate_signals(self, day_data):
        """Filter stocks matching all signal conditions from daily DataFrame.

        Required columns: hour1_volume, hour2_volume, hour3_volume,
                         hour1_close_rate, turn, volume, close, close_rate
        """
        df = day_data.copy()

        # Data completeness: all three hourly volumes must be positive
        has_hourly_data = (
            (df['hour1_volume'] > 0) &
            (df['hour2_volume'] > 0) &
            (df['hour3_volume'] > 0)
        )

        # Compute hour1-3 total volume
        total_h123 = df['hour1_volume'] + df['hour2_volume'] + df['hour3_volume']

        # Avoid division by zero
        volume_ratio = df['hour1_volume'] / total_h123.replace(0, float('nan'))

        # Compute daily trade amount: volume(shares) * close
        # Note: volume in DB is in shares (not lots), amount ~= volume * close
        trade_amount = df['volume'] * df['close']

        mask = (
            has_hourly_data &
            (volume_ratio > self.volume_ratio_min) &
            (df['hour1_close_rate'] > self.hour1_rate_min) &
            (df['turn'] > 0) &
            (df['turn'] < self.turn_max) &
            (trade_amount > self.min_trade_amount) &
            (df['close_rate'] >= self.close_rate_min) &
            (df['close_rate'] < self.close_rate_max)
        )
        return day_data.loc[mask].copy()

    def rank_signals(self, signals):
        """Rank by turn ASC, return top-1."""
        if signals is None or signals.empty:
            return signals
        return signals.nsmallest(1, 'turn')
