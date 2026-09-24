"""A 股模拟账户：T+1、100 股整手、佣金/印花税/过户费、逐批持仓。

规则：
- 买入：100 股整数倍；现金立即扣减；费用 = 佣金（万2.5，最低 5 元）
  + 过户费（0.001%）；成本价 = 含费成交均价；T+1 起可卖。
- 卖出：可用份额 = 已过 T+1 的批次（FIFO）；费用 = 佣金 + 印花税（0.05%，
  仅卖出）+ 过户费；现金立即到账，计已实现收益。
- 总资产 = 现金 + 持仓市值；持仓成本含买入费用，收益扣净卖出费用。
"""
from dataclasses import dataclass, field
from datetime import datetime

LOT_SIZE = 100
COMMISSION_RATE = 2.5e-4
MIN_COMMISSION = 5.0
STAMP_TAX_RATE = 5e-4
TRANSFER_FEE_RATE = 1e-5


@dataclass
class StockLot:
    """一次买入形成的持仓批次（T+1 解锁）。"""
    code: str
    shares: int
    cost: float            # 含买入费用的每股成本
    buy_date: str
    sellable_date: str     # 该批可卖的首个交易日
    reason: str = ""
    confidence: float = 0.0


@dataclass
class StockTrade:
    timestamp: datetime
    code: str
    side: str              # BUY / SELL
    price: float
    shares: int
    amount: float          # 成交金额（不含费）
    fee: float
    realized: float = 0.0  # 本笔已实现收益（卖出才有）
    reason: str = ""
    confidence: float = 0.0
    trade_date: str = ""
    sellable_date: str = ""

    def to_dict(self):
        return {
            "time": self.timestamp.isoformat(),
            "code": self.code,
            "side": self.side,
            "price": round(self.price, 4),
            "shares": self.shares,
            "amount": round(self.amount, 2),
            "fee": round(self.fee, 2),
            "realized": round(self.realized, 2),
            "reason": self.reason,
            "confidence": self.confidence,
            "trade_date": self.trade_date,
            "sellable_date": self.sellable_date,
        }


class StockAccount:
    """股票模拟账户。"""

    def __init__(self, initial_capital, lot_size=LOT_SIZE,
                 commission_rate=COMMISSION_RATE,
                 min_commission=MIN_COMMISSION,
                 stamp_tax_rate=STAMP_TAX_RATE,
                 transfer_fee_rate=TRANSFER_FEE_RATE):
        self.initial_capital = float(initial_capital)
        self.lot_size = int(lot_size)
        self.commission_rate = float(commission_rate)
        self.min_commission = float(min_commission)
        self.stamp_tax_rate = float(stamp_tax_rate)
        self.transfer_fee_rate = float(transfer_fee_rate)
        self.cash = float(initial_capital)
        self.lots = {}          # code -> list[StockLot]
        self.fees = 0.0
        self.realized_pnl = 0.0
        self.trade_count = 0
        self.order_seq = 0

    # ---------- 费用 ----------

    def _buy_fee(self, amount):
        commission = max(amount * self.commission_rate, self.min_commission)
        transfer = amount * self.transfer_fee_rate
        return commission + transfer

    def _sell_fee(self, amount):
        commission = max(amount * self.commission_rate, self.min_commission)
        stamp = amount * self.stamp_tax_rate
        transfer = amount * self.transfer_fee_rate
        return commission + stamp + transfer

    # ---------- 买入 ----------

    def max_buy_shares(self, price, cash=None):
        """给定价格下可买的最大整手股数（预留费用）。"""
        cash = self.cash if cash is None else float(cash)
        if price <= 0 or cash <= 0:
            return 0
        raw = int(cash / price)
        raw -= raw % self.lot_size
        while raw > 0:
            amount = raw * price
            if amount + self._buy_fee(amount) <= cash + 1e-9:
                return raw
            raw -= self.lot_size
        return 0

    def buy(self, code, price, shares, trade_date, sellable_date,
            reason="", confidence=0.0):
        """买入。shares 必须为整手，现金不足或非法数量抛 ValueError。"""
        shares = int(shares)
        if shares <= 0 or shares % self.lot_size:
            raise ValueError(f"买入股数 {shares} 非 {self.lot_size} 整数倍")
        amount = price * shares
        fee = self._buy_fee(amount)
        if amount + fee > self.cash + 1e-9:
            raise ValueError(
                f"现金不足：需 {amount + fee:.2f}，有 {self.cash:.2f}")
        self.cash -= amount + fee
        self.fees += fee
        self.trade_count += 1
        self.order_seq += 1
        lot = StockLot(
            code=code, shares=shares, cost=(amount + fee) / shares,
            buy_date=str(trade_date), sellable_date=str(sellable_date),
            reason=reason, confidence=confidence,
        )
        self.lots.setdefault(code, []).append(lot)
        return StockTrade(
            timestamp=datetime.now(), code=code, side="BUY", price=price,
            shares=shares, amount=amount, fee=fee, reason=reason,
            confidence=confidence, trade_date=str(trade_date),
            sellable_date=str(sellable_date),
        )

    # ---------- 卖出 ----------

    def available_shares(self, code, trade_date):
        """已解锁（sellable_date <= trade_date）的持仓股数。"""
        d = str(trade_date)
        return sum(l.shares for l in self.lots.get(code, [])
                   if l.sellable_date <= d)

    def total_shares(self, code):
        return sum(l.shares for l in self.lots.get(code, []))

    def sell(self, code, price, shares, trade_date, reason="",
             confidence=0.0):
        """卖出（FIFO 扣批次，仅可卖已解锁份额）。"""
        shares = int(shares)
        if shares <= 0:
            raise ValueError("卖出股数必须大于 0")
        avail = self.available_shares(code, trade_date)
        if shares > avail:
            raise ValueError(f"可卖 {avail} 股，不足 {shares} 股")
        amount = price * shares
        fee = self._sell_fee(amount)
        remaining = shares
        realized = 0.0
        keep = []
        for lot in sorted(self.lots.get(code, []),
                          key=lambda x: x.sellable_date):
            if remaining <= 0:
                keep.append(lot)
                continue
            if lot.sellable_date > str(trade_date):
                keep.append(lot)
                continue
            take = min(lot.shares, remaining)
            cost_basis = lot.cost * take
            proceeds = take * price
            # 卖出费用按股数比例分摊到本批次
            share_fee = fee * (take / shares) if shares else 0.0
            realized += proceeds - share_fee - cost_basis
            lot.shares -= take
            remaining -= take
            if lot.shares > 0:
                keep.append(lot)
        self.lots[code] = keep
        if not keep:
            self.lots.pop(code, None)
        self.cash += amount - fee
        self.fees += fee
        self.realized_pnl += realized
        self.trade_count += 1
        self.order_seq += 1
        return StockTrade(
            timestamp=datetime.now(), code=code, side="SELL", price=price,
            shares=shares, amount=amount, fee=fee, realized=realized,
            reason=reason, confidence=confidence, trade_date=str(trade_date),
        )

    # ---------- 估值 ----------

    def position_value(self, code, price):
        return self.total_shares(code) * price

    def market_value(self, prices):
        value = 0.0
        for code, lots in self.lots.items():
            price = prices.get(code)
            if price:
                value += sum(l.shares for l in lots) * price
        return value

    def total_asset(self, prices):
        return self.cash + self.market_value(prices)

    def position_pct(self, code, prices):
        total = self.total_asset(prices)
        if total <= 0:
            return 0.0
        return self.position_value(code, prices.get(code, 0.0)) / total * 100

    def unrealized_pnl(self, prices):
        pnl = 0.0
        for code, lots in self.lots.items():
            price = prices.get(code)
            if not price:
                continue
            for l in lots:
                pnl += (price - l.cost) * l.shares
        return pnl

    def position_codes(self):
        return [c for c, lots in self.lots.items()
                if sum(l.shares for l in lots) > 0]

    # ---------- 持久化 ----------

    def to_state(self):
        return {
            "initial_capital": self.initial_capital,
            "lot_size": self.lot_size,
            "commission_rate": self.commission_rate,
            "min_commission": self.min_commission,
            "stamp_tax_rate": self.stamp_tax_rate,
            "transfer_fee_rate": self.transfer_fee_rate,
            "cash": self.cash,
            "fees": self.fees,
            "realized_pnl": self.realized_pnl,
            "trade_count": self.trade_count,
            "order_seq": self.order_seq,
            "lots": {
                code: [vars(l) for l in lots]
                for code, lots in self.lots.items()
            },
        }

    @classmethod
    def from_state(cls, state):
        acc = cls(
            initial_capital=state["initial_capital"],
            lot_size=state.get("lot_size", LOT_SIZE),
            commission_rate=state.get("commission_rate", COMMISSION_RATE),
            min_commission=state.get("min_commission", MIN_COMMISSION),
            stamp_tax_rate=state.get("stamp_tax_rate", STAMP_TAX_RATE),
            transfer_fee_rate=state.get("transfer_fee_rate",
                                        TRANSFER_FEE_RATE),
        )
        acc.cash = state["cash"]
        acc.fees = state.get("fees", 0.0)
        acc.realized_pnl = state.get("realized_pnl", 0.0)
        acc.trade_count = state.get("trade_count", 0)
        acc.order_seq = state.get("order_seq", 0)
        acc.lots = {
            code: [StockLot(**l) for l in lots]
            for code, lots in state.get("lots", {}).items()
        }
        return acc

    def snapshot(self, prices):
        return {
            "cash": round(self.cash, 2),
            "fees": round(self.fees, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl(prices), 2),
            "total_asset": round(self.total_asset(prices), 2),
            "trade_count": self.trade_count,
            "positions": {
                code: {
                    "shares": self.total_shares(code),
                    "pct": round(self.position_pct(code, prices), 2),
                    "cost": round(
                        sum(l.cost * l.shares for l in lots)
                        / max(1, sum(l.shares for l in lots)), 4),
                    "price": round(prices.get(code, 0.0), 4),
                    "pnl_pct": round(
                        (prices.get(code, 0.0) / (
                            sum(l.cost * l.shares for l in lots)
                            / max(1, sum(l.shares for l in lots))) - 1) * 100,
                        2) if prices.get(code) else 0.0,
                }
                for code, lots in self.lots.items()
            },
        }
