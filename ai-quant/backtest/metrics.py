"""绩效指标：从回放 records 计算完整策略评价指标。

输入：SimEngine.records（或 latest.json 的 records）+ account + loader。
输出：dict，包含收益/风险/交易质量/归因/基准对比全维度指标。

注意：
- 2026-08-17 之前的回放数据里 SUBSCRIBE 的 shares 记录为 0（P0 bug，
  已在 executor.py 修复）。本模块对历史数据用 amount/nav 估算申购份额做
  FIFO 匹配，单笔收益为近似值，总额与 account.realized_pnl 对齐校验。
- records[].positions 是【份额】不是市值（engine 快照 total_shares）。
  未实现盈亏 = 期末份额 × 期末净值 - FIFO 剩余成本。
"""
import math
import statistics
from datetime import date


def _as_date(d):
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def _daily_returns(assets):
    rets = []
    for i in range(1, len(assets)):
        prev = assets[i - 1]
        if prev > 0:
            rets.append(assets[i] / prev - 1.0)
    return rets


def _max_drawdown(assets, dates):
    peak = assets[0]
    peak_date = dates[0]
    max_dd = 0.0
    dd_from = dd_to = None
    for v, d in zip(assets, dates):
        if v > peak:
            peak = v
            peak_date = d
        dd = (v - peak) / peak if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
            dd_from = peak_date
            dd_to = d
    return max_dd, dd_from, dd_to


def _fifo_trades(records):
    """逐笔交易盈亏（FIFO 匹配）。

    申购队列：[(trade_date, est_shares, nav, amount)]，
    est_shares = amount / nav（历史数据份额为 0 时估算）。
    赎回：按 FIFO 从队列扣份额，按比例分摊申购成本。
    返回 (per_trade, remaining_lots, realized_by_symbol, fees)。
    """
    lots = {}   # symbol -> [ {date, shares, nav, amount} ]
    per_trade = []
    fees = 0.0
    for r in records:
        t = r.get("trade")
        if not isinstance(t, dict):
            continue
        sym = t.get("symbol")
        side = t.get("side")
        fees += t.get("fee", 0.0)
        if side == "SUBSCRIBE":
            amt = t.get("amount", 0.0)
            nav = t.get("nav") or 0.0
            shares = t.get("shares") or 0.0
            if shares <= 0 and nav > 0:
                shares = amt / nav  # 历史数据缺陷：估算份额
            lots.setdefault(sym, []).append({
                "date": t.get("trade_date"),
                "shares": shares,
                "nav": nav,
                "amount": amt,
            })
        elif side == "REDEEM":
            red_shares = t.get("shares", 0.0)
            proceeds = t.get("amount", 0.0)
            red_date = t.get("trade_date")
            q = lots.get(sym, [])
            remaining = red_shares
            cost = 0.0
            buy_date = None
            while remaining > 1e-9 and q:
                lot = q[0]
                take = min(remaining, lot["shares"])
                if lot["nav"] > 0:
                    cost += take * lot["nav"]
                remaining -= take
                buy_date = lot["date"]
                lot["shares"] -= take
                if lot["shares"] <= 1e-9:
                    q.pop(0)
            pnl = proceeds - cost
            per_trade.append({
                "symbol": sym,
                "buy_date": buy_date,
                "sell_date": red_date,
                "shares": red_shares,
                "cost": cost,
                "proceeds": proceeds,
                "pnl": pnl,
                "hold_days": (
                    (_as_date(red_date) - _as_date(buy_date)).days
                    if buy_date and red_date else None
                ),
            })
    # 剩余持仓（未实现）——注意这是估算份额
    remaining_lots = {}
    for sym, q in lots.items():
        if q:
            remaining_lots[sym] = [dict(lot) for lot in q]
    # 已实现按 symbol
    realized_by_symbol = {}
    for t in per_trade:
        realized_by_symbol[t["symbol"]] = (
            realized_by_symbol.get(t["symbol"], 0.0) + t["pnl"]
        )
    return per_trade, remaining_lots, realized_by_symbol, fees


def compute_metrics(records, account=None, loader=None):
    """主入口。loader 提供 market_series/nav_on 算基准与期末净值。"""
    if not records:
        return {}
    assets = [r["asset"] for r in records]
    dates = [r["date"] for r in records]
    start_date, end_date = _as_date(dates[0]), _as_date(dates[-1])
    n_days = (end_date - start_date).days
    years = max(n_days / 365.25, 1e-9)

    start, end = assets[0], assets[-1]
    total_return = end / start - 1.0 if start > 0 else 0.0
    cagr = (end / start) ** (1 / years) - 1.0 if start > 0 and end > 0 else 0.0

    rets = _daily_returns(assets)
    vol = statistics.pstdev(rets) * math.sqrt(252) if len(rets) > 1 else 0.0
    rf = 0.0
    sharpe = (cagr - rf) / vol if vol > 0 else 0.0
    downside = [min(r, 0.0) for r in rets]
    dd_vol = (statistics.pstdev(downside) if len(downside) > 1 else 0.0) * math.sqrt(252)
    sortino = (cagr - rf) / dd_vol if dd_vol > 0 else 0.0

    max_dd, dd_from, dd_to = _max_drawdown(assets, dates)
    calmar = cagr / abs(max_dd) if max_dd != 0 else 0.0

    # 交易质量
    per_trade, remaining_lots, realized_by_symbol, fees = _fifo_trades(records)
    n_trades = len(per_trade)
    wins = [t for t in per_trade if t["pnl"] > 0]
    losses = [t for t in per_trade if t["pnl"] <= 0]
    win_rate = len(wins) / n_trades if n_trades else 0.0
    avg_win = statistics.mean(t["pnl"] for t in wins) if wins else 0.0
    avg_loss = statistics.mean(t["pnl"] for t in losses) if losses else 0.0
    profit_factor = (
        sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
        if losses and sum(t["pnl"] for t in losses) != 0 else None
    )
    hold_days = [t["hold_days"] for t in per_trade if t["hold_days"] is not None]
    avg_hold_days = statistics.mean(hold_days) if hold_days else None

    # 换手率（单边年化）：min(申购额, 赎回额) / 平均资产 / 年数
    sub_amt = sum(
        r["trade"]["amount"] for r in records
        if isinstance(r.get("trade"), dict) and r["trade"]["side"] == "SUBSCRIBE"
    )
    red_amt = sum(
        r["trade"]["amount"] for r in records
        if isinstance(r.get("trade"), dict) and r["trade"]["side"] == "REDEEM"
    )
    avg_asset = statistics.mean(assets)
    turnover = (min(sub_amt, red_amt) / avg_asset / years) if avg_asset > 0 else 0.0

    # 空仓时间占比：positions 全空的天数
    cash_days = 0
    for r in records:
        pos = r.get("positions") or {}
        if not pos or all(abs(v) < 1e-6 for v in pos.values()):
            cash_days += 1
    cash_ratio = cash_days / len(records) if records else 0.0

    # 最大连续亏损（按逐笔交易）
    max_streak = 0
    cur_streak = 0
    streak_pnl = 0.0
    max_streak_pnl = 0.0
    for t in per_trade:
        if t["pnl"] < 0:
            cur_streak += 1
            streak_pnl += t["pnl"]
            if cur_streak > max_streak:
                max_streak = cur_streak
                max_streak_pnl = streak_pnl
        else:
            cur_streak = 0
            streak_pnl = 0.0

    # 期末净值（loader.nav_on）
    end_navs = {}
    if loader is not None:
        for sym in ["半导体", "通信", "人工智能", "银行", "消费电子"]:
            info = loader.nav_on(sym, str(end_date)) or {}
            if info.get("nav"):
                end_navs[sym] = info["nav"]

    # 未实现：期末份额 × 期末净值 - FIFO 剩余成本
    # records[-1].positions 是份额；只对期末真实持仓(>0份额)计算，
    # 避免 FIFO 估算残留把已清仓 ETF 算成浮亏。
    final_shares = records[-1].get("positions") or {}
    unrealized = 0.0
    unrealized_by_symbol = {}
    for sym, sh in final_shares.items():
        if sh <= 1e-6:
            continue
        nav = end_navs.get(sym)
        if not nav:
            continue
        mv = sh * nav
        cost = sum(lot["shares"] * lot["nav"] for lot in remaining_lots.get(sym, []))
        u = mv - cost
        unrealized += u
        unrealized_by_symbol[sym] = u

    # 每 ETF 贡献（已实现 + 未实现）
    contribution = {}
    for sym in set(list(realized_by_symbol.keys()) + list(unrealized_by_symbol.keys())):
        contribution[sym] = {
            "realized": round(realized_by_symbol.get(sym, 0.0), 2),
            "unrealized": round(unrealized_by_symbol.get(sym, 0.0), 2),
            "total": round(realized_by_symbol.get(sym, 0.0)
                           + unrealized_by_symbol.get(sym, 0.0), 2),
        }

    # 基准对比（需要 loader）
    benchmark = {}
    if loader is not None:
        bh = {}
        for sym in ["半导体", "通信", "人工智能", "银行", "消费电子"]:
            series = loader.market_series(sym, str(end_date))
            sp = ep = None
            for p in series:
                if p["date"] <= str(start_date):
                    sp = p["close"]
                if p["date"] <= str(end_date):
                    ep = p["close"]
                else:
                    break
            if sp and ep and sp > 0:
                bh[sym] = round((ep - sp) / sp * 100, 2)
        if bh:
            benchmark["etf_bh_each"] = bh
            benchmark["etf_bh_equal_weight"] = round(
                statistics.mean(bh.values()), 2)
        benchmark["cash"] = 0.0
        benchmark["model_return_pct"] = round(total_return * 100, 2)
        if bh:
            benchmark["alpha_vs_equal_weight"] = round(
                total_return * 100 - benchmark["etf_bh_equal_weight"], 2)

    realized_pnl_acct = None
    unrealized_pnl_acct = None
    if isinstance(account, dict):
        realized_pnl_acct = account.get("realized_pnl")
        unrealized_pnl_acct = account.get("unrealized_pnl")

    return {
        "period": {"start": str(start_date), "end": str(end_date),
                   "trading_days": len(records)},
        "return": {
            "start_asset": round(start, 2),
            "end_asset": round(end, 2),
            "total_return_pct": round(total_return * 100, 2),
            "cagr_pct": round(cagr * 100, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "dd_from": dd_from, "dd_to": dd_to,
            "calmar": round(calmar, 3),
        },
        "risk": {
            "annual_vol_pct": round(vol * 100, 2),
            "sharpe": round(sharpe, 3),
            "sortino": round(sortino, 3),
        },
        "trade_quality": {
            "trade_count": n_trades,
            "win_rate_pct": round(win_rate * 100, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 3) if profit_factor else None,
            "avg_hold_days": round(avg_hold_days, 1) if avg_hold_days else None,
            "annual_turnover": round(turnover * 100, 2),
            "cash_ratio_pct": round(cash_ratio * 100, 2),
            "max_losing_streak": max_streak,
            "max_losing_streak_pnl": round(max_streak_pnl, 2),
            "realized_pnl": round(sum(t["pnl"] for t in per_trade), 2),
            "account_realized_pnl": realized_pnl_acct,
            "account_unrealized_pnl": unrealized_pnl_acct,
            "fees": round(fees, 2),
        },
        "contribution_by_etf": contribution,
        "per_trade": per_trade,
        "benchmark": benchmark,
    }


def format_report(m, verbose_trades=False):
    """格式化为 markdown 报告。"""
    L = []
    L.append("## 回放绩效报告\n")
    p = m["period"]
    L.append(f"- 区间：{p['start']} ~ {p['end']}（{p['trading_days']} 个交易日）\n")
    r = m["return"]
    L.append("### 收益与风险")
    L.append("| 指标 | 数值 |")
    L.append("| --- | --- |")
    L.append(f"| 总收益 | {r['total_return_pct']:+.2f}% |")
    L.append(f"| CAGR | {r['cagr_pct']:+.2f}% |")
    L.append(f"| 年化波动率 | {m['risk']['annual_vol_pct']:.2f}% |")
    L.append(f"| Sharpe | {m['risk']['sharpe']:.3f} |")
    L.append(f"| Sortino | {m['risk']['sortino']:.3f} |")
    L.append(f"| 最大回撤 | {r['max_drawdown_pct']:.2f}%（{r['dd_from']} ~ {r['dd_to']}） |")
    L.append(f"| Calmar | {r['calmar']:.3f} |")
    L.append("")
    tq = m["trade_quality"]
    L.append("### 交易质量")
    L.append("| 指标 | 数值 |")
    L.append("| --- | --- |")
    L.append(f"| 交易笔数（赎回） | {tq['trade_count']} |")
    L.append(f"| 胜率 | {tq['win_rate_pct']:.2f}% |")
    L.append(f"| 平均盈利 | {tq['avg_win']:+.2f} |")
    L.append(f"| 平均亏损 | {tq['avg_loss']:+.2f} |")
    L.append(f"| 盈亏比 | {tq['profit_factor']} |")
    L.append(f"| 平均持仓周期 | {tq['avg_hold_days']} 天 |")
    L.append(f"| 年化换手率 | {tq['annual_turnover']:.1f}% |")
    L.append(f"| 空仓时间占比 | {tq['cash_ratio_pct']:.1f}% |")
    L.append(f"| 最大连续亏损 | {tq['max_losing_streak']} 笔（{tq['max_losing_streak_pnl']:+.2f}） |")
    L.append(f"| 已实现盈亏（逐笔合计） | {tq['realized_pnl']:+.2f} |")
    if tq.get("account_realized_pnl") is not None:
        L.append(f"| 已实现盈亏（账本） | {tq['account_realized_pnl']:+.2f} |")
    if tq.get("account_unrealized_pnl") is not None:
        L.append(f"| 未实现盈亏（账本） | {tq['account_unrealized_pnl']:+.2f} |")
    L.append("")
    L.append("### 各 ETF 贡献（已实现 + 未实现）")
    L.append("| ETF | 已实现 | 未实现 | 合计 |")
    L.append("| --- | --- | --- | --- |")
    for sym, c in m["contribution_by_etf"].items():
        L.append(f"| {sym} | {c['realized']:+.2f} | {c['unrealized']:+.2f} | {c['total']:+.2f} |")
    L.append("")
    b = m.get("benchmark", {})
    if b:
        L.append("### 基准对比")
        L.append("| 基准 | 收益 |")
        L.append("| --- | --- |")
        L.append(f"| 模型 | {b.get('model_return_pct', 0):+.2f}% |")
        for sym, v in b.get("etf_bh_each", {}).items():
            L.append(f"| ETF {sym} 买入持有 | {v:+.2f}% |")
        if "etf_bh_equal_weight" in b:
            L.append(f"| 等权 ETF 买入持有 | {b['etf_bh_equal_weight']:+.2f}% |")
        L.append(f"| 现金 | {b['cash']:+.2f}% |")
        if "alpha_vs_equal_weight" in b:
            L.append(f"| **Alpha（vs 等权）** | **{b['alpha_vs_equal_weight']:+.2f}pp** |")
    L.append("")
    if verbose_trades:
        L.append("### 逐笔交易")
        L.append("| 基金 | 买入日 | 卖出日 | 份额 | 成本 | 到账 | 盈亏 | 持仓天数 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for t in m["per_trade"]:
            L.append(f"| {t['symbol']} | {t['buy_date']} | {t['sell_date']} | "
                     f"{t['shares']:.0f} | {t['cost']:.0f} | {t['proceeds']:.0f} | "
                     f"{t['pnl']:+.0f} | {t['hold_days']} |")
    return "\n".join(L)
