"""场外 ETF 联接 C 模拟引擎（历史回放 + 每日推进）。

每日流程：
1. 确认到期 pending 订单（份额入仓 / 现金到账）
2. 构建决策上下文（T 日 ETF 市场信号 + T-1 日 NAV + 账户状态）
3. 多模型决策（ensemble）
4. 风控 + 执行（金额申购 / 份额赎回）
5. 记录当日快照（signal/trade/nav/confirm 日期显式记录）

防未来函数：决策只用 T 日可得信息（ETF 盘中行情、T-1 日净值），
成交按 T 日净值（未知价），T+1 确认。
"""
import json
import os
import statistics
from datetime import date

from dataclasses import asdict
from trading.account import FundAccount
from trading.fund_lot import FundLot, PendingOrder
from trading.executor import Executor
from trading.calendar import TradingCalendar
from data.features.technical import TechnicalFeature
from data.fund.fund_mapping import FUND_MAP
from memory.otc_memory import load_lessons

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(BASE_DIR, "backtest", "results")


class SimEngine:
    def __init__(self, capital=100000, ensemble=None, min_holding_days=7,
                 loader=None, symbols=None):
        self.account = FundAccount(capital, min_holding_days=min_holding_days)
        self.calendar = TradingCalendar()
        self.executor = Executor(self.account, calendar=self.calendar)
        self.ensemble = ensemble  # EnsembleDecision 或 None（HOLD 模式）
        self.feature_engine = TechnicalFeature()
        self.loader = loader
        self.symbols = symbols or list(FUND_MAP.keys())
        self.records = []

    # ---------- 上下文 ----------

    def build_context(self, trade_date, navs):
        """构建 AI 决策上下文（T 日可得信息）。

        - market：T 日 ETF 行情 + 技术指标（截至 T 日序列计算）
        - fund：T-1 日 NAV（当日净值未知）
        - account：账户状态
        """
        if self.loader is None:
            return {"date": trade_date, "market": [], "account": {}}

        market = []
        for symbol in self.symbols:
            series = self.loader.market_series(symbol, trade_date)
            if not series:
                continue
            prices = [p["close"] for p in series]
            feats = self.feature_engine.calculate_prices(prices)
            info = FUND_MAP.get(symbol)
            prev_nav = None
            for p in sorted(
                (self.loader.load_fund_navs().get(symbol, {})
                 .get("points", [])),
                key=lambda x: x["date"],
            ):
                if p["date"] < trade_date:
                    prev_nav = p
                else:
                    break
            anomaly = self.loader.nav_anomaly(symbol, trade_date)
            market.append({
                "symbol": symbol,
                "etf_code": info.etf_code if info else "",
                "close": prices[-1],
                "change_1d": round(feats["change_1d"], 2),
                "ma5": round(feats["ma5"], 4),
                "ma20": round(feats["ma20"], 4),
                "trend": feats["trend"],
                "volatility": round(feats["volatility"], 4),
                "prev_nav": prev_nav["nav"] if prev_nav else None,
                "prev_nav_date": prev_nav["date"] if prev_nav else None,
                "anomaly": anomaly,
            })

        positions = []
        for symbol in self.symbols:
            shares = self.account.total_shares(symbol)
            if shares <= 0:
                continue
            nav = navs.get(symbol, 0)
            positions.append({
                "symbol": symbol,
                "shares": round(shares, 4),
                "nav": nav,
                "pct": round(self.account.position_pct(symbol, navs), 2),
                "frozen": round(self.account.frozen_shares(symbol), 4),
            })

        return {
            "date": trade_date,
            "market": market,
            "lessons": load_lessons(5),
            "account": {
                "cash": round(self.account.cash, 2),
                "total_asset": round(self.account.total_asset(navs), 2),
                "positions": positions,
                "pending": len([
                    o for o in self.account.pending
                    if o.status == "pending"
                ]),
            },
        }

    # ---------- 单日推进 ----------

    def advance_day(self, trade_date, navs, nav_date=None, decide=True):
        """推进一个交易日。

        trade_date: 交易日 T
        navs: {symbol: T日净值}（用于成交/确认）
        nav_date: 净值日期（默认 = trade_date）
        decide: False 时跳过 AI 决策（仅确认+记录）
        """
        trade_date = str(trade_date)
        # 1. 确认到期订单
        self.account.confirm_orders(trade_date, navs)

        # 2. 上下文 + 决策
        context = self.build_context(trade_date, navs)
        decision = None
        votes = None
        trade_result = "HOLD"

        if decide and self.ensemble is not None:
            current = 0.0
            # 当前仓位：取持仓市值最大的单基金（决策围绕它）
            best = None
            for symbol in self.symbols:
                pct = self.account.position_pct(symbol, navs)
                if pct > current:
                    current = pct
                    best = symbol
            decision, votes = self.ensemble.decide(context, current)
            # 3. 除权/数据异常守卫 + 风控 + 执行
            anomaly_note = None
            if decision.action != "HOLD":
                for m in context.get("market", []):
                    if (m.get("symbol") == decision.target
                            and m.get("anomaly")):
                        anomaly_note = m["anomaly"]
                        break
            if anomaly_note:
                trade_result = f"BLOCKED (数据异常: {anomaly_note})"
            else:
                trade_result = self.executor.execute(
                    decision, navs, trade_date
                )

        # 4. 快照
        record = {
            "date": trade_date,
            "nav_date": nav_date or trade_date,
            "asset": round(self.account.total_asset(navs), 2),
            "cash": round(self.account.cash, 2),
            "positions": {
                symbol: round(self.account.total_shares(symbol), 4)
                for symbol in self.symbols
                if self.account.total_shares(symbol) > 0
            },
            "frozen": {
                symbol: round(self.account.frozen_shares(symbol), 4)
                for symbol in self.symbols
                if self.account.frozen_shares(symbol) > 0
            },
            "decision": (
                {
                    "action": decision.action,
                    "target": decision.target,
                    "target_position": decision.target_position,
                    "confidence": decision.confidence,
                    "source": decision.source,
                }
                if decision else None
            ),
            "votes": [
                {
                    "voter": v["voter"],
                    "ok": v["ok"],
                    "error": v.get("error"),
                    "target_position": (
                        v["decision"].target_position
                        if v["ok"] else None
                    ),
                }
                for v in (votes or [])
            ],
            "trade": (
                trade_result.to_dict()
                if hasattr(trade_result, "to_dict")
                else str(trade_result)
            ),
        }
        self.records.append(record)
        return record

    # ---------- 历史回放 ----------

    def replay(self, start=None, end=None, decide=True, progress=True,
           checkpoint_path=None):
        """按交易日序列回放。start/end 为 YYYY-MM-DD。"""
        days = self.loader.trading_days()
        if start:
            days = [d for d in days if d >= start]
        if end:
            days = [d for d in days if d <= end]
        for i, d in enumerate(days):
            navs = {
                symbol: (self.loader.nav_on(symbol, d) or {}).get("nav")
                for symbol in self.symbols
            }
            navs = {k: v for k, v in navs.items() if v}
            if not navs:
                continue
            record = self.advance_day(d, navs, decide=decide)
            if checkpoint_path:
                save_checkpoint(checkpoint_path, d, self)
            if progress and (i % 20 == 0 or i == len(days) - 1):
                print(
                    f"  [{i+1}/{len(days)}] {d} "
                    f"asset={record['asset']} "
                    f"pos={record['positions']} "
                    f"trade={str(record['trade'])[:40]}"
                )
        return self.records

    # ---------- 结果 ----------

    def save_result(self, path=None):
        os.makedirs(RESULTS_DIR, exist_ok=True)
        path = path or os.path.join(RESULTS_DIR, "latest.json")
        payload = {
            "account": self.account.snapshot(self._last_navs()),
            "records": self.records,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path

    def _last_navs(self):
        if not self.records:
            return {}
        last = self.records[-1]
        d = last["nav_date"]
        return {
            symbol: (self.loader.nav_on(symbol, d) or {}).get("nav")
            for symbol in self.symbols
        }


# ---------- 检查点 ----------

def account_to_dict(acc):
    return {
        "initial_capital": acc.initial_capital,
        "min_subscribe": acc.min_subscribe,
        "subscribe_fee_rate": acc.subscribe_fee_rate,
        "redeem_fee_rate": acc.redeem_fee_rate,
        "min_holding_days": acc.min_holding_days,
        "cash": acc.cash,
        "fees": acc.fees,
        "realized_pnl": acc.realized_pnl,
        "trade_count": acc.trade_count,
        "order_seq": acc.order_seq,
        "lots": {k: [asdict(l) for l in v] for k, v in acc.lots.items()},
        "pending": [asdict(o) for o in acc.pending],
    }


def account_from_dict(d):
    acc = FundAccount(
        initial_capital=d["initial_capital"],
        min_subscribe=d.get("min_subscribe", 100.0),
        subscribe_fee_rate=d.get("subscribe_fee_rate", 0.0),
        redeem_fee_rate=d.get("redeem_fee_rate", 0.0),
        min_holding_days=d.get("min_holding_days", 7),
    )
    acc.cash = d["cash"]
    acc.fees = d["fees"]
    acc.realized_pnl = d["realized_pnl"]
    acc.trade_count = d["trade_count"]
    acc.order_seq = d["order_seq"]
    acc.lots = {
        k: [FundLot(**x) for x in v] for k, v in d.get("lots", {}).items()
    }
    acc.pending = [PendingOrder(**x) for x in d.get("pending", [])]
    return acc


def save_checkpoint(path, last_date, engine):
    payload = {
        "last_date": last_date,
        "account": account_to_dict(engine.account),
        "records": engine.records,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load_checkpoint(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["account"] = account_from_dict(data["account"])
    return data
