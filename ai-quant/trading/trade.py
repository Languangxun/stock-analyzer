"""场外基金交易记录：SUBSCRIBE / REDEEM。

- 申购：amount=金额，shares=确认份额
- 赎回：shares=赎回份额，amount=确认到账金额
"""
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Trade:
    timestamp: datetime
    symbol: str
    fund_code: str
    side: str              # SUBSCRIBE / REDEEM
    nav: float             # 成交净值
    amount: float          # SUBSCRIBE=金额；REDEEM=到账金额
    shares: float          # SUBSCRIBE=确认份额；REDEEM=赎回份额
    fee: float
    reason: str = ""
    confidence: float = 0.0
    trade_date: str = ""
    confirm_date: str = ""

    def to_dict(self):
        return {
            "time": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "fund_code": self.fund_code,
            "side": self.side,
            "nav": self.nav,
            "amount": self.amount,
            "shares": self.shares,
            "fee": self.fee,
            "reason": self.reason,
            "confidence": self.confidence,
            "trade_date": self.trade_date,
            "confirm_date": self.confirm_date,
        }
