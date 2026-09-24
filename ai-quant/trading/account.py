"""场外基金模拟账户（金额申购 / 份额赎回 / T+1 确认 / FundLot）。

规则：
- 金额申购：最低 100 元，申购费 0，滑点 0。
  T 日现金立即扣减，份额确认日入账（按 T 日净值），期间记 pending。
- 份额赎回：赎回费 0。提交即冻结份额（FIFO），现金确认日到账（按 T 日净值）。
- 策略级持有期规则：min_holding_days（默认 7），持有期从确认日起算，可配置为 0 关闭。
- 总资产守恒：申购冻结现金、赎回冻结份额都不使总资产凭空减少。
  total_asset = cash + 已确认持仓市值 + pending 申购金额 + pending 赎回份额市值
"""
from dataclasses import dataclass, field

from trading.fund_lot import FundLot, PendingOrder

MIN_SUBSCRIBE = 100.0
SUBSCRIBE_FEE_RATE = 0.0
REDEEM_FEE_RATE = 0.0


@dataclass
class FundAccount:
    initial_capital: float
    min_subscribe: float = MIN_SUBSCRIBE
    subscribe_fee_rate: float = SUBSCRIBE_FEE_RATE
    redeem_fee_rate: float = REDEEM_FEE_RATE
    min_holding_days: int = 7

    cash: float = field(init=False)
    lots: dict = field(default_factory=dict)     # symbol -> list[FundLot]
    pending: list = field(default_factory=list)  # list[PendingOrder]
    fees: float = field(init=False)
    realized_pnl: float = field(init=False)
    trade_count: int = field(init=False)
    order_seq: int = field(init=False, default=0)

    def __post_init__(self):
        self.cash = self.initial_capital
        self.fees = 0.0
        self.realized_pnl = 0.0
        self.trade_count = 0

    # ---------- 申购 ----------

    def subscribe(self, symbol, fund_code, amount, trade_date,
                  confirm_date, nav_est=0.0, reason="", confidence=0.0):
        """金额申购：现金立即扣减，份额确认日入账。"""
        if amount < self.min_subscribe:
            raise ValueError(
                f"申购金额 {amount} 低于最低申购 {self.min_subscribe} 元"
            )
        fee = amount * self.subscribe_fee_rate
        if self.cash < amount + fee:
            raise ValueError("现金不足")
        self.cash -= amount + fee
        self.fees += fee
        self.trade_count += 1
        self.order_seq += 1
        order = PendingOrder(
            side="SUBSCRIBE",
            symbol=symbol,
            fund_code=fund_code,
            amount=amount,
            shares=(amount - fee) / nav_est if nav_est > 0 else 0.0,
            nav=nav_est,
            fee=fee,
            trade_date=trade_date,
            confirm_date=confirm_date,
            reason=reason,
            confidence=confidence,
        )
        self.pending.append(order)
        return order

    # ---------- 赎回 ----------

    def _sorted_lots(self, symbol, today):
        lots = self.lots.get(symbol, [])
        return sorted(lots, key=lambda l: l.confirm_date)

    def available_shares(self, symbol, today) -> float:
        return sum(
            lot.available_shares(today, self.min_holding_days)
            for lot in self._sorted_lots(symbol, today)
        )

    def total_shares(self, symbol) -> float:
        """已确认总份额（含冻结）。"""
        return sum(lot.shares for lot in self.lots.get(symbol, []))

    def frozen_shares(self, symbol) -> float:
        return sum(lot.frozen_shares for lot in self.lots.get(symbol, []))

    def redeem(self, symbol, fund_code, shares, trade_date,
               confirm_date, nav_est=0.0, reason="", confidence=0.0):
        """份额赎回：提交即冻结（FIFO，跳过持有期不足的批次），现金确认日到账。"""
        today = trade_date
        avail = self.available_shares(symbol, today)
        if shares <= 0:
            raise ValueError("赎回份额必须大于 0")
        if shares > avail:
            raise ValueError(
                f"赎回份额 {shares} 超过可用份额 {avail}"
            )
        # FIFO 冻结
        remaining = shares
        for lot in self._sorted_lots(symbol, today):
            if remaining <= 0:
                break
            can = lot.available_shares(today, self.min_holding_days)
            take = min(can, remaining)
            lot.frozen_shares += take
            remaining -= take
        if remaining > 1e-9:  # 理论不可达（avail 已校验）
            raise ValueError("冻结份额不足")
        fee = 0.0  # 当前规则赎回费 0；确认时按到账金额重算
        self.trade_count += 1
        self.order_seq += 1
        order = PendingOrder(
            side="REDEEM",
            symbol=symbol,
            fund_code=fund_code,
            amount=shares * nav_est if nav_est > 0 else 0.0,
            shares=shares,
            nav=nav_est,
            fee=fee,
            trade_date=trade_date,
            confirm_date=confirm_date,
            reason=reason,
            confidence=confidence,
        )
        self.pending.append(order)
        return order

    # ---------- 确认 ----------

    def confirm_orders(self, today, navs):
        """确认 confirm_date <= today 的订单。

        navs: {symbol: T日净值}（key 取 symbol，兼容 fund_code 作为备用 key）。
        申购：shares = amount / T日净值，建 FundLot（confirm_date=today 前已定的确认日）。
        赎回：amount = shares * T日净值，现金到账，份额扣除，计已实现收益。
        """
        confirmed = []
        for order in self.pending:
            if order.status != "pending":
                continue
            if order.confirm_date > str(today):
                continue
            nav = navs.get(order.symbol, navs.get(order.fund_code))
            if nav is None or nav <= 0:
                continue  # 缺净值数据则跳过，等待
            if order.side == "SUBSCRIBE":
                order.nav = nav
                order.shares = order.amount / nav
                order.status = "confirmed"
                lot = FundLot(
                    symbol=order.symbol,
                    fund_code=order.fund_code,
                    shares=order.shares,
                    nav=nav,
                    amount=order.amount,
                    trade_date=order.trade_date,
                    confirm_date=order.confirm_date,
                )
                self.lots.setdefault(order.symbol, []).append(lot)
            elif order.side == "REDEEM":
                order.nav = nav
                order.amount = order.shares * nav
                order.fee = order.amount * self.redeem_fee_rate
                order.status = "confirmed"
                self.cash += order.amount - order.fee
                self.fees += order.fee
                self._deduct_shares(order, nav)
            confirmed.append(order)
        return confirmed

    def _deduct_shares(self, order, nav):
        """确认赎回：从冻结批次扣除份额（FIFO）。"""
        remaining = order.shares
        for lot in self._sorted_lots(order.symbol, order.trade_date):
            if remaining <= 0:
                break
            take = min(lot.frozen_shares, remaining)
            if take <= 0:
                continue
            lot.shares -= take
            lot.frozen_shares -= take
            self.realized_pnl += (nav - lot.nav) * take
            remaining -= take
        # 清理零份额批次
        lots = [l for l in self.lots.get(order.symbol, []) if l.shares > 1e-9]
        if lots:
            self.lots[order.symbol] = lots
        else:
            self.lots.pop(order.symbol, None)

    # ---------- 估值 ----------

    def position_value(self, symbol, nav) -> float:
        return self.total_shares(symbol) * nav

    def total_asset(self, navs) -> float:
        """总资产 = 现金 + 已确认持仓市值 + pending 申购金额 + pending 赎回市值。"""
        value = self.cash
        for symbol, lots in self.lots.items():
            nav = navs.get(symbol)
            if nav is None:
                continue
            value += sum(lot.shares * nav for lot in lots)
        for order in self.pending:
            if order.status != "pending":
                continue
            if order.side == "SUBSCRIBE":
                # 现金已扣，冻结金额仍属账户资产
                value += order.amount
            # REDEEM：冻结份额仍在持仓市值中（total_shares 含 frozen），
            # 此处不重复计。
        return value

    def position_pct(self, symbol, navs) -> float:
        """已确认持仓占账户比例（含冻结，不含 pending）。"""
        total = self.total_asset(navs)
        if total <= 0:
            return 0.0
        return self.position_value(symbol, navs.get(symbol, 0.0)) / total * 100

    def unrealized_pnl(self, navs) -> float:
        pnl = 0.0
        for symbol, lots in self.lots.items():
            nav = navs.get(symbol)
            if nav is None:
                continue
            pnl += sum((nav - lot.nav) * lot.shares for lot in lots)
        return pnl

    def to_state(self) -> dict:
        """持久化状态。"""
        return {
            "initial_capital": self.initial_capital,
            "min_subscribe": self.min_subscribe,
            "subscribe_fee_rate": self.subscribe_fee_rate,
            "redeem_fee_rate": self.redeem_fee_rate,
            "min_holding_days": self.min_holding_days,
            "cash": self.cash,
            "fees": self.fees,
            "realized_pnl": self.realized_pnl,
            "trade_count": self.trade_count,
            "order_seq": self.order_seq,
            "lots": {
                symbol: [vars(lot) for lot in lots]
                for symbol, lots in self.lots.items()
            },
            "pending": [vars(o) for o in self.pending],
        }

    @classmethod
    def from_state(cls, state: dict):
        acc = cls(
            initial_capital=state["initial_capital"],
            min_subscribe=state.get("min_subscribe", MIN_SUBSCRIBE),
            subscribe_fee_rate=state.get("subscribe_fee_rate", SUBSCRIBE_FEE_RATE),
            redeem_fee_rate=state.get("redeem_fee_rate", REDEEM_FEE_RATE),
            min_holding_days=state.get("min_holding_days", 7),
        )
        acc.cash = state["cash"]
        acc.fees = state.get("fees", 0.0)
        acc.realized_pnl = state.get("realized_pnl", 0.0)
        acc.trade_count = state.get("trade_count", 0)
        acc.order_seq = state.get("order_seq", 0)
        acc.lots = {
            symbol: [FundLot(**lot) for lot in lots]
            for symbol, lots in state.get("lots", {}).items()
        }
        acc.pending = [PendingOrder(**o) for o in state.get("pending", [])]
        return acc

    def snapshot(self, navs) -> dict:
        return {
            "cash": round(self.cash, 2),
            "fees": round(self.fees, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl(navs), 2),
            "total_asset": round(self.total_asset(navs), 2),
            "trade_count": self.trade_count,
            "positions": {
                symbol: {
                    "shares": round(self.total_shares(symbol), 4),
                    "frozen": round(self.frozen_shares(symbol), 4),
                    "pct": round(self.position_pct(symbol, navs), 2),
                }
                for symbol in self.lots
            },
            "pending": [
                {
                    "side": o.side,
                    "symbol": o.symbol,
                    "amount": round(o.amount, 2),
                    "shares": round(o.shares, 4),
                    "trade_date": o.trade_date,
                    "confirm_date": o.confirm_date,
                    "status": o.status,
                }
                for o in self.pending
                if o.status == "pending"
            ],
        }
