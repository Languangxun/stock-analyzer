"""FundLot + 待确认订单：场外基金份额批次模型。"""
from dataclasses import dataclass
from datetime import date, datetime


def _as_date(d):
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d))


@dataclass
class FundLot:
    """一次确认申购形成的独立份额批次。

    持有期从 confirm_date（确认日）起算，不是申购申请日。
    """
    symbol: str
    fund_code: str
    shares: float
    nav: float                # 确认净值
    amount: float             # 申购金额
    trade_date: str           # 申购申请日 T
    confirm_date: str         # 份额确认日
    frozen_shares: float = 0.0  # 已被赎回订单冻结的份额

    def holding_days(self, today) -> int:
        return (_as_date(today) - _as_date(self.confirm_date)).days

    def available_shares(self, today, min_holding_days=0):
        if self.holding_days(today) < min_holding_days:
            return 0.0
        return max(0.0, self.shares - self.frozen_shares)

    def value(self, nav: float) -> float:
        return self.shares * nav


@dataclass
class PendingOrder:
    """待确认订单。

    申购：amount=金额，shares=预计份额（确认时按 T 日净值重算）
    赎回：shares=赎回份额，amount=预计到账金额（确认时按 T 日净值重算）
    """
    side: str                 # SUBSCRIBE / REDEEM
    symbol: str
    fund_code: str
    amount: float
    shares: float
    nav: float                # T 日净值（下单时未知，先用最新净值占位）
    fee: float
    trade_date: str           # 申请日 T
    confirm_date: str         # 确认日
    reason: str = ""
    confidence: float = 0.0
    status: str = "pending"   # pending -> confirmed
