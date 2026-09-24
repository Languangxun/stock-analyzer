"""场外基金执行器：Decision + 风控 -> 金额申购 / 份额赎回。"""
import math
from datetime import datetime

from trading.trade import Trade
from trading.calendar import TradingCalendar
from risk.manager import RiskManager


class Executor:
    def __init__(self, account, calendar=None, risk_manager=None):
        self.account = account
        self.calendar = calendar or TradingCalendar()
        self.risk = risk_manager or RiskManager()
        self.history = []

    def execute(self, decision, navs, trade_date, now=None):
        """执行决策。

        decision: Decision（target_position 为目标仓位 %）
        navs: {symbol: 当日净值}
        trade_date: 交易日期 T（str/date）
        now: 下单时间（默认 14:30，即 15:00 前）

        返回 "HOLD" / "NO CHANGE" / Trade / RiskResult(拒绝)
        """
        symbol = decision.target
        trade_date = str(trade_date)

        if decision.action == "HOLD":
            return "HOLD"

        nav = navs.get(symbol)
        if nav is None or nav <= 0:
            return "NO CHANGE"

        current_pct = self.account.position_pct(symbol, navs)
        risk_result = self.risk.check(decision, current_pct)
        if not risk_result.allowed:
            return risk_result

        target_pct = risk_result.target_position
        total = self.account.total_asset(navs)
        target_value = total * target_pct / 100
        current_value = self.account.position_value(symbol, nav)
        diff = target_value - current_value

        confirm_date = self.calendar.next_trading_day(
            _as_date(trade_date)
        )

        if abs(diff) < self.account.min_subscribe:
            return "NO CHANGE"

        if diff > 0:
            # 金额申购：取整到 100 的倍数，且不低于最低申购
            amount = int(diff // 100) * 100
            if amount < self.account.min_subscribe:
                return "NO CHANGE"
            order = self.account.subscribe(
                symbol=symbol,
                fund_code=self._fund_code(symbol),
                amount=amount,
                trade_date=trade_date,
                confirm_date=str(confirm_date),
                nav_est=nav,
                reason=decision.reason,
                confidence=decision.confidence,
            )
            trade = Trade(
                timestamp=datetime.now(),
                symbol=symbol,
                fund_code=self._fund_code(symbol),
                side="SUBSCRIBE",
                nav=nav,
                amount=order.amount,
                shares=order.shares,  # P0修复: 记录预估份额(amount/nav), 确认份额以T+1净值在账本重算
                fee=order.fee,
                reason=decision.reason,
                confidence=decision.confidence,
                trade_date=trade_date,
                confirm_date=str(confirm_date),
            )
        else:
            # 份额赎回；清仓（target=0）赎全部可用份额
            if target_pct == 0:
                shares = self.account.available_shares(symbol, trade_date)
            else:
                shares = abs(diff) / nav
            shares = math.floor(shares * 10000) / 10000
            # 封顶：冻结中（已提交赎回）的份额不可再赎回
            avail = self.account.available_shares(symbol, trade_date)
            shares = min(shares, avail)
            if shares <= 0:
                return "NO CHANGE"
            order = self.account.redeem(
                symbol=symbol,
                fund_code=self._fund_code(symbol),
                shares=shares,
                trade_date=trade_date,
                confirm_date=str(confirm_date),
                nav_est=nav,
                reason=decision.reason,
                confidence=decision.confidence,
            )
            trade = Trade(
                timestamp=datetime.now(),
                symbol=symbol,
                fund_code=self._fund_code(symbol),
                side="REDEEM",
                nav=nav,
                amount=order.shares * nav,
                shares=order.shares,
                fee=0.0,
                reason=decision.reason,
                confidence=decision.confidence,
                trade_date=trade_date,
                confirm_date=str(confirm_date),
            )

        self.history.append(trade)
        return trade

    def _fund_code(self, symbol):
        try:
            from data.fund.fund_mapping import FUND_MAP
            info = FUND_MAP.get(symbol)
            return info.fund_code if info else ""
        except Exception:
            return ""


def _as_date(d):
    from datetime import date, datetime as dt
    if isinstance(d, dt):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d))
