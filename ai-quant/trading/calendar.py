"""A股交易日历。

规则：
- 周六/周日非交易日
- 预留法定节假日接口（holidays 集合，YYYY-MM-DD）
- 15:00 前下单记当日 T；15:00 后顺延下一交易日
"""
from datetime import date, datetime, time, timedelta

CUTOFF = time(15, 0)


class TradingCalendar:
    """交易日历。默认只跳过周末，可注入法定节假日。"""

    def __init__(self, holidays=None):
        self.holidays = set(holidays or [])

    def is_trading_day(self, d: date) -> bool:
        if d.weekday() >= 5:  # Sat=5 Sun=6
            return False
        if d.isoformat() in self.holidays:
            return False
        return True

    def next_trading_day(self, d: date, offset: int = 1) -> date:
        """从 d 之后数第 offset 个交易日（不含 d 本身）。"""
        cur = d
        count = 0
        while count < offset:
            cur += timedelta(days=1)
            if self.is_trading_day(cur):
                count += 1
        return cur

    def trade_date_of(self, dt: datetime) -> date:
        """下单时刻 -> 交易日期 T。

        - 交易日 15:00 前：当日
        - 交易日 15:00 后：下一交易日
        - 非交易日任意时刻：下一交易日
        """
        d = dt.date()
        if self.is_trading_day(d) and dt.time() <= CUTOFF:
            return d
        return self.next_trading_day(d)
