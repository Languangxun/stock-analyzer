"""回测/模拟报告：策略收益 + 基准对比（ETF Buy&Hold / 现金）。"""
import statistics

from data.fund.fund_mapping import FUND_MAP


class BacktestReport:
    def __init__(self, loader=None):
        self.loader = loader

    def generate(self, records, account=None):
        """records: SimEngine.records；account: FundAccount（可选，取已实现收益）。"""
        if not records:
            return {}

        start = records[0]["asset"]
        end = records[-1]["asset"]
        assets = [r["asset"] for r in records]

        # 最大回撤
        peak = assets[0]
        max_dd = 0.0
        for v in assets:
            if v > peak:
                peak = v
            dd = (v - peak) / peak * 100 if peak > 0 else 0
            max_dd = min(max_dd, dd)

        # 交易统计
        subs = []
        reds = []
        for r in records:
            t = r.get("trade")
            if not isinstance(t, dict):
                continue
            if t.get("side") == "SUBSCRIBE":
                subs.append(t)
            elif t.get("side") == "REDEEM":
                reds.append(t)

        fees = sum(t.get("fee", 0) for t in subs + reds)

        # 每笔交易收益：赎回时按 (确认净值 - 成本) 估算
        # 成本取该基金最近一次申购净值（简化），精确值来自账户 realized_pnl
        trades_detail = []
        for t in reds:
            trades_detail.append({
                "side": "REDEEM",
                "symbol": t.get("symbol"),
                "date": t.get("trade_date"),
                "amount": t.get("amount", 0),
                "shares": t.get("shares", 0),
            })
        for t in subs:
            trades_detail.append({
                "side": "SUBSCRIBE",
                "symbol": t.get("symbol"),
                "date": t.get("trade_date"),
                "amount": t.get("amount", 0),
                "shares": t.get("shares", 0),
            })

        # 基准
        bench = self._benchmark(records)

        result = {
            "start_asset": round(start, 2),
            "end_asset": round(end, 2),
            "return_pct": round((end - start) / start * 100, 2) if start else 0,
            "max_drawdown_pct": round(max_dd, 2),
            "trade_count": len(subs) + len(reds),
            "subscribe_count": len(subs),
            "redeem_count": len(reds),
            "subscribe_amount": round(sum(t["amount"] for t in subs), 2),
            "redeem_amount": round(sum(t["amount"] for t in reds), 2),
            "subscribe_shares": round(sum(t.get("shares", 0) for t in subs), 4),
            "redeem_shares": round(sum(t.get("shares", 0) for t in reds), 4),
            "fees": round(fees, 2),
            "trades_detail": trades_detail[-20:],
            "realized_pnl": (
                round(account.realized_pnl, 2) if account else None
            ),
            "final_cash": records[-1].get("cash"),
            "final_positions": records[-1].get("positions"),
            "benchmark": bench,
        }
        if bench.get("etf_bh_return_pct") is not None:
            result["alpha_vs_etf_pct"] = round(
                result["return_pct"] - bench["etf_bh_return_pct"], 2
            )
        return result

    def _benchmark(self, records):
        """基准：等权 5 只场内 ETF Buy&Hold + 现金基准。"""
        if self.loader is None:
            return {"cash_return_pct": 0.0, "etf_bh_return_pct": None}
        start_date = records[0]["date"]
        end_date = records[-1]["nav_date"]
        rets = []
        for symbol in FUND_MAP:
            series = self.loader.market_series(symbol, end_date)
            start_p = end_p = None
            for p in series:
                if p["date"] <= start_date:
                    start_p = p["close"]
                if p["date"] <= end_date:
                    end_p = p["close"]
                else:
                    break
            if start_p and end_p and start_p > 0:
                rets.append((end_p - start_p) / start_p * 100)
        etf_bh = (
            statistics.mean(rets) if rets else None
        )
        return {
            "cash_return_pct": 0.0,  # 现金基准：0 收益
            "etf_bh_return_pct": round(etf_bh, 2) if etf_bh is not None else None,
        }
