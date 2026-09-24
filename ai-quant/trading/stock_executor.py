"""股票执行器：把决策（目标仓位/买卖）变成 A 股整手成交。

风控（execute 内统一校验）：
- 单票不超过 max_position_pct；同时持仓不超过 max_positions
- 买入金额不低于 min_order_amount；现金不足自动缩量或放弃
- SELL 只卖已解锁（T+1）份额；涨跌停附近不成交（可选）
"""
import math
from datetime import date, datetime

from trading.stock_account import StockTrade
from trading.calendar import TradingCalendar


def _as_date(d):
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d))


def price_limit_pct(code):
    """涨跌停幅度：创业板(30)/科创板(68) 20%，其余 10%。"""
    c = str(code).lower()
    if c.startswith(("sz30", "sh68", "bj")):
        return 0.20
    return 0.10


class StockExecutor:
    def __init__(self, account, calendar=None, max_positions=5,
                 max_position_pct=20.0, min_order_amount=5000.0,
                 check_limit=True):
        self.account = account
        self.calendar = calendar or TradingCalendar()
        self.max_positions = int(max_positions)
        self.max_position_pct = float(max_position_pct)
        self.min_order_amount = float(min_order_amount)
        self.check_limit = check_limit
        self.history = []

    def _next_date(self, trade_date):
        return str(self.calendar.next_trading_day(_as_date(trade_date)))

    def _limit_blocked(self, code, side, price, prev_close):
        """涨跌停附近禁止成交（涨停买不到 / 跌停卖不出）。"""
        if not self.check_limit or not prev_close or prev_close <= 0:
            return False
        chg = price / prev_close - 1.0
        lim = price_limit_pct(code)
        if side == "BUY" and chg >= lim - 0.002:
            return True
        if side == "SELL" and chg <= -lim + 0.002:
            return True
        return False

    def buy(self, code, price, trade_date, amount=None, target_pct=None,
            reason="", confidence=0.0, prices=None, prev_close=None):
        """买入：amount 金额优先，否则按 target_pct 目标仓位计算差额。"""
        prices = prices or {code: price}
        if price <= 0:
            return None
        if code not in self.account.lots and \
                len(self.account.position_codes()) >= self.max_positions:
            return "REJECT 持仓数已满"
        if self._limit_blocked(code, "BUY", price, prev_close):
            return "REJECT 涨停附近无法买入"

        if amount is None:
            total = self.account.total_asset(prices)
            target_value = total * (target_pct or 0) / 100.0
            current = self.account.position_value(code, price)
            amount = target_value - current
        # 单票上限
        total = self.account.total_asset(prices)
        cap = total * self.max_position_pct / 100.0
        current = self.account.position_value(code, price)
        amount = min(amount, cap - current)
        if amount < self.min_order_amount:
            return "NO CHANGE 金额不足"
        shares = int(amount / price)
        shares -= shares % self.account.lot_size
        shares = min(shares, self.account.max_buy_shares(price))
        if shares <= 0:
            return "NO CHANGE 现金不足"
        try:
            trade = self.account.buy(
                code, price, shares, trade_date, self._next_date(trade_date),
                reason=reason, confidence=confidence)
        except ValueError as e:
            return f"REJECT {e}"
        self.history.append(trade)
        return trade

    def sell(self, code, price, trade_date, shares=None, reason="",
             confidence=0.0, prev_close=None):
        """卖出：默认清掉全部可卖份额。"""
        if price <= 0:
            return None
        avail = self.account.available_shares(code, trade_date)
        if avail <= 0:
            return "NO CHANGE 无可卖份额(T+1)"
        if self._limit_blocked(code, "SELL", price, prev_close):
            return "REJECT 跌停附近无法卖出"
        shares = int(avail if shares is None else min(shares, avail))
        if shares <= 0:
            return "NO CHANGE"
        try:
            trade = self.account.sell(
                code, price, shares, trade_date,
                reason=reason, confidence=confidence)
        except ValueError as e:
            return f"REJECT {e}"
        self.history.append(trade)
        return trade

    def execute_order(self, order, prices, trade_date, prev_closes=None):
        """执行 LLM 订单：{"code","action","position","reason","confidence"}。"""
        code = str(order.get("code") or "").lower()
        action = str(order.get("action") or "HOLD").upper()
        if not code or action == "HOLD":
            return "HOLD"
        price = prices.get(code)
        prev_close = (prev_closes or {}).get(code)
        if not price or price <= 0:
            return "NO CHANGE 无行情"
        if action == "BUY":
            pos = order.get("position")
            return self.buy(
                code, price, trade_date,
                target_pct=float(pos) if pos is not None else None,
                reason=order.get("reason", ""),
                confidence=float(order.get("confidence") or 0),
                prices=prices, prev_close=prev_close)
        if action == "SELL":
            return self.sell(
                code, price, trade_date, reason=order.get("reason", ""),
                confidence=float(order.get("confidence") or 0),
                prev_close=prev_close)
        return f"REJECT 非法动作 {action}"

    def mark(self, prices):
        """估值快照。"""
        return self.account.snapshot(prices)
