"""股票组合回测引擎（全缓存数据 · CLI 信号 · A股规则）。

与 CLI 单股事件回测（`_bt_events`）同口径：
- 信号 T 日收盘生成，T+1 成交（exec_px=close 收盘 / open 开盘）
- ATR(14) 跟踪止损：止损单用 T-1 日 ATR；浮盈超过 trail_trigger 后按
  最高价 * trail_ratio 移动止盈；跳空低开按开盘价成交
- A股规则：100 股整手、T+1 可卖、佣金/印花税/过户费、涨跌停不成交

组合规则：
- 同时最多 max_positions 只，单票不超过 max_position_pct
- 同一天多个 BUY 信号按评分降序择优；SELL 先执行释放资金与名额
- 逐日按收盘价估值，输出净值曲线与绩效指标（基准=等权全市场/指数）
"""
import bisect
import json
import math
import os
import re
import time
from datetime import datetime

from trading.stock_account import StockAccount
from trading.stock_executor import price_limit_pct
from data.stock import cli_bridge as bridge

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(BASE_DIR, "backtest", "results")

_SCORE_RE = re.compile(r"\((-?\d+)\)")


def result_markdown(result):
    """把回测结果 JSON 渲染成 markdown 报告。"""
    s = result.get("stats") or {}
    cfg = s.get("config") or {}
    eq = result.get("equity") or []
    idx = {d: v for d, v in (result.get("index_curve") or [])}
    bench = {d: v for d, v in (result.get("bench_curve") or [])}
    lines = [
        "# 股票组合回测报告",
        "",
        f"- 生成时间：{result.get('generated', '')}",
        f"- 区间：{s.get('range', ['?', '?'])[0]} ~ "
        f"{s.get('range', ['?', '?'])[1]}（{s.get('days', 0)} 个交易日）",
        f"- 参数：风险档 **{cfg.get('risk_mode')}**，最多 "
        f"{cfg.get('max_positions')} 只，单票 ≤{cfg.get('max_position_pct')}%，"
        f"成交价 {cfg.get('exec_px')}，止损 {cfg.get('stop')}",
        "",
        "## 总体指标",
        "",
        "| 指标 | 策略 | 等权全市场 | 上证指数 |",
        "|---|---:|---:|---:|",
        f"| 总收益 | {s.get('total_return', 0) * 100:+.1f}% | "
        f"{(s.get('bench_total_return') or 0) * 100:+.1f}% | "
        f"{(s.get('index_total_return') or 0) * 100:+.1f}% |",
        f"| 年化 | {s.get('annual', 0) * 100:+.1f}% | "
        f"{(s.get('bench_annual') or 0) * 100:+.1f}% | "
        f"{(s.get('index_annual') or 0) * 100:+.1f}% |",
        f"| 最大回撤 | {s.get('max_drawdown', 0) * 100:+.1f}% | - | "
        f"{(s.get('index_mdd') or 0) * 100:+.1f}% |",
        f"| Sharpe | {s.get('sharpe', 0):.2f} | - | - |",
        f"| 超额（指数） | {(s.get('index_excess') or 0) * 100:+.1f}pp | - | - |",
        "",
        f"- 期末资产：{s.get('final_asset', 0):,.0f} 元"
        f"（初始 {s.get('capital', 0):,.0f}）",
        f"- 交易：{s.get('trades', 0)} 笔（平仓 {s.get('closed', 0)}），"
        f"胜率 {(s.get('win_rate') or 0) * 100:.1f}%，"
        f"费用 {s.get('fees', 0):,.0f} 元",
        "",
    ]
    # 年度收益对比
    if eq:
        years = {}
        for d, v in eq:
            years.setdefault(d[:4], [v, v])[1] = v
        lines += ["## 年度收益", "",
                  "| 年份 | 策略 | 上证指数 |", "|---|---:|---:|"]
        prev = None
        for y in sorted(years):
            first, last = years[y]
            ret = (last / first - 1) * 100 if first else 0.0
            if idx:
                idates = sorted(d for d in idx if d[:4] == y)
                iret = ((idx[idates[-1]] / idx[idates[0]] - 1) * 100
                        if len(idates) > 1 else 0.0)
            else:
                iret = 0.0
            lines.append(f"| {y} | {ret:+.1f}% | {iret:+.1f}% |")
        lines.append("")
    lines += [
        "## 口径说明",
        "",
        "- 信号：`stock_predict` CLI 多维评分 `_composite_signals`（T 日收盘生成）",
        "- 成交：T+1（默认收盘价），100 股整手，T+1 可卖",
        "- 费用：佣金万2.5（最低5元）+ 过户费0.001% + 卖出印花税0.05%",
        "- 止损：ATR(14) 跟踪（浮盈超阈值后按最高价回撤比例移动）",
        "- 基准：等权全市场（逐日截面平均，无费用）；上证指数取自缓存",
        "",
        "*历史统计研究，不构成投资建议。*",
    ]
    return "\n".join(lines)


def _load_cfg():
    import yaml
    path = os.path.join(BASE_DIR, "config", "stock.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["stock"]


class StockBacktest:
    def __init__(self, capital=None, risk_mode=None, max_positions=None,
                 max_position_pct=None, min_order_amount=None,
                 exec_px=None, check_limit=True, min_bars=None,
                 start=None, end=None, limit=0, boards=None, progress=print):
        cfg = _load_cfg()
        self.cfg = cfg
        self.capital = float(capital or cfg["capital"])
        self.risk_mode = risk_mode or cfg.get("risk_mode", "稳健")
        self.max_positions = int(max_positions or cfg["max_positions"])
        self.max_position_pct = float(
            max_position_pct or cfg["max_position_pct"])
        self.min_order_amount = float(
            min_order_amount or cfg["min_order_amount"])
        self.exec_px = exec_px or cfg.get("exec_px", "close")
        self.check_limit = check_limit
        self.min_bars = int(min_bars or cfg["universe"]["min_bars"])
        self.start = start or cfg.get("backtest", {}).get("start") or ""
        self.end = end or ""
        self.limit = int(limit or 0)
        self.boards = boards or cfg["universe"].get("boards") or "all"
        self.progress = progress
        self.stop_cfg = cfg.get("stop") or {}

        self.account = StockAccount(
            self.capital,
            commission_rate=cfg["fees"]["commission_rate"],
            min_commission=cfg["fees"]["min_commission"],
            stamp_tax_rate=cfg["fees"]["stamp_tax_rate"],
            transfer_fee_rate=cfg["fees"]["transfer_fee_rate"],
        )
        self.events = {}        # exec_date -> [(code, action, score, reason)]
        self.held = {}          # code -> {"rows": [...], "idx": {date: i},
                                #          "entry": px, "highest": px,
                                #          "signal_date": d}
        self.equity = []        # [(date, total_asset)]
        self.bench = {}         # date -> [sum_ret, count]
        self.trades = []
        self.stats = {}

    # ---------- 第一遍：扫描信号 + 基准 ----------

    def scan(self):
        codes = bridge.universe(min_bars=self.min_bars, boards=self.boards)
        if self.limit:
            codes = codes[:self.limit]
        total = len(codes)
        self.progress(f"股票池 {total} 只（min_bars={self.min_bars}, "
                      f"boards={self.boards}）")
        t0 = time.time()
        n_sig = 0
        for k, (code, name, industry, mktcap) in enumerate(codes):
            if k % 200 == 0:
                self.progress(f"  扫描 {k}/{total}  "
                              f"({time.time() - t0:.0f}s, 信号 {n_sig})")
            try:
                rows = bridge.db_rows(code)
            except Exception:
                continue
            if len(rows) < self.min_bars:
                continue
            # 等权基准：逐日收益累计
            for i in range(1, len(rows)):
                p0, p1 = rows[i - 1]["close"], rows[i]["close"]
                if p0 and p1 and p0 > 0:
                    b = self.bench.setdefault(rows[i]["date"], [0.0, 0])
                    b[0] += p1 / p0 - 1.0
                    b[1] += 1
            try:
                sigs = bridge.signals(rows, self.risk_mode)
            except Exception as e:
                self.progress(f"  [warn] {code} 信号失败: {e}")
                continue
            for i, sdate, action, reason in sigs:
                j = i + 1
                if j >= len(rows):
                    continue
                edate = rows[j]["date"]
                if self.start and edate < self.start:
                    continue
                if self.end and edate > self.end:
                    continue
                m = _SCORE_RE.search(reason or "")
                score = int(m.group(1)) if m else 0
                self.events.setdefault(edate, []).append(
                    (code, action, score, reason, name))
                n_sig += 1
        self.progress(f"扫描完成：{total} 只，信号 {n_sig} 条，"
                      f"耗时 {time.time() - t0:.0f}s")

    # ---------- 第二遍：逐日推进 ----------

    def run(self):
        self.scan()
        if not self.start:
            # 无显式起点：从最早信号日开始（此前无交易，曲线无信息量）
            self.start = min(self.events) if self.events else ""
        if not self.end:
            # 无显式终点：截到"多数股票仍有数据"的最后一天，
            # 避免个别股票更新到最新导致最后一周样本稀疏
            self.end = self._broad_end()
        dates = bridge.trade_dates(self.start or None)
        if self.end:
            dates = [d for d in dates if d <= self.end]
        if not dates:
            raise SystemExit("无交易日数据")
        self.progress(f"回测区间 {dates[0]} ~ {dates[-1]}（{len(dates)} 日）")
        t0 = time.time()
        for di, day in enumerate(dates):
            if di % 100 == 0:
                self.progress(
                    f"  推进 {di}/{len(dates)} {day} "
                    f"持仓{len(self.held)} 资产"
                    f"{self.account.total_asset(self._last_prices):.0f}")
            self._process_stops(day)
            self._process_signals(day)
            prices = self._close_prices(day)
            self.equity.append(
                (day, round(self.account.total_asset(prices), 2)))
        self.progress(f"推进完成，耗时 {time.time() - t0:.0f}s")
        self._metrics()

    def _broad_end(self):
        """多数股票仍有数据的最后日期（bench 计数 >= 峰值一半）。"""
        if not self.bench:
            return ""
        peak = max(n for _s, n in self.bench.values())
        ok = [d for d, (_s, n) in self.bench.items() if n >= peak * 0.5]
        return max(ok) if ok else ""

    # ---------- 持仓辅助 ----------

    def _load_held(self, code, entry_price, signal_date):
        rows = bridge.db_rows(code)
        self.held[code] = {
            "rows": rows,
            "idx": {r["date"]: i for i, r in enumerate(rows)},
            "dates": [r["date"] for r in rows],
            "entry": entry_price,
            "highest": entry_price,
            "signal_date": signal_date,
            "atr": bridge.atr(rows),
        }

    def _idx_on(self, code, day):
        """持仓行索引；停牌/数据截止时回退到之前最近一根。"""
        h = self.held.get(code)
        if not h:
            return None
        i = h["idx"].get(day)
        if i is not None:
            return i
        j = bisect.bisect_left(h["dates"], day) - 1
        return j if j >= 0 else None

    def _row_on(self, code, day):
        i = self._idx_on(code, day)
        if i is None:
            return None
        return self.held[code]["rows"][i]

    def _close_prices(self, day):
        prices = {}
        for code in list(self.held):
            r = self._row_on(code, day)
            if r and r["close"]:
                prices[code] = r["close"]
        return prices

    @property
    def _last_prices(self):
        if not self.equity:
            return {}
        return {c: self._row_on(c, self.equity[-1][0])["close"]
                for c in self.held
                if self._row_on(c, self.equity[-1][0])}

    # ---------- 止损（CLI 同款 ATR 跟踪） ----------

    def _process_stops(self, day):
        atr_mult = self.stop_cfg.get("atr_mult", 1.5)
        trail_ratio = self.stop_cfg.get("trail_ratio", 0.94)
        trail_trig = self.stop_cfg.get("trail_trigger", 1.02)
        for code in list(self.held):
            h = self.held[code]
            r = self._row_on(code, day)
            if not r:
                continue
            o, hi, lo = r["open"], r["high"], r["low"]
            if not (o and hi and lo):
                continue
            prev_high = h["highest"]
            h["highest"] = max(prev_high or h["entry"], hi)
            i = self._idx_on(code, day)
            atr_prev = h["atr"][i - 1] if i and i > 0 else 0.0
            entry = h["entry"]
            atr_stop = (entry - atr_mult * atr_prev) if atr_prev > 0 \
                else entry * 0.95
            trail_stop = (prev_high * trail_ratio
                          if prev_high and prev_high > entry * trail_trig
                          else atr_stop)
            if lo > trail_stop:
                continue
            # T+1：当日买入不可卖（本引擎买入在收盘后，天然满足；
            # 仍按可卖份额兜底）
            avail = self.account.available_shares(code, day)
            if avail <= 0:
                continue
            px = o if (o and o <= trail_stop) else trail_stop
            try:
                t = self.account.sell(code, px, avail, day,
                                      reason="ATR跟踪止损")
                self.trades.append(t.to_dict())
            except ValueError:
                continue
            self.held.pop(code, None)

    # ---------- 信号执行 ----------

    def _process_signals(self, day):
        evs = self.events.get(day)
        if not evs:
            return
        sells = [e for e in evs if e[1] == "SELL"]
        buys = sorted([e for e in evs if e[1] == "BUY"],
                      key=lambda e: -e[2])
        for code, _a, _s, reason, _n in sells:
            if code not in self.held:
                continue
            r = self._row_on(code, day)
            if not r or not r["close"]:
                continue
            if self._limit_blocked(code, "SELL", r["close"],
                                   self._prev_close(code, day)):
                continue
            avail = self.account.available_shares(code, day)
            if avail <= 0:
                continue
            try:
                t = self.account.sell(code, r["close"], avail, day,
                                      reason=reason)
                self.trades.append(t.to_dict())
                self.held.pop(code, None)
            except ValueError:
                continue
        for code, _a, score, reason, name in buys:
            if len(self.held) >= self.max_positions:
                break
            if code in self.held:
                continue
            r = self._row_on_buy(code, day)
            if not r:
                continue
            px = self._exec_price(code, day)
            if not px:
                continue
            if self._limit_blocked(code, "BUY", px,
                                   self._prev_close(code, day)):
                continue
            prices = self._close_prices(day)
            prices[code] = px
            total = self.account.total_asset(prices)
            cap = total * self.max_position_pct / 100.0
            amount = min(total / self.max_positions, cap,
                         self.account.cash)
            shares = self.account.max_buy_shares(px, cash=amount)
            if shares <= 0 or shares * px < self.min_order_amount:
                continue
            try:
                t = self.account.buy(code, px, shares, day, self._next_day(day),
                                     reason=reason)
                self.trades.append(t.to_dict())
                self._load_held(code, px, day)
            except ValueError:
                continue

    def _row_on_buy(self, code, day):
        """买入候选：未持仓，从缓存取当日K线（含 open/close）。"""
        return bridge.row_on(code, day)

    def _exec_price(self, code, day):
        r = bridge.row_on(code, day)
        if not r:
            return 0.0
        px = r["open"] if self.exec_px == "open" else r["close"]
        return px or 0.0

    def _prev_close(self, code, day):
        return bridge.prev_close(code, day)

    def _next_day(self, day):
        if getattr(self, "_dates", None) is None:
            self._dates = bridge.trade_dates(self.start or None)
        dates = self._dates
        try:
            i = dates.index(day)
        except ValueError:
            return day
        return dates[i + 1] if i + 1 < len(dates) else day

    def _limit_blocked(self, code, side, price, prev_close):
        if not self.check_limit or not prev_close or prev_close <= 0:
            return False
        chg = price / prev_close - 1.0
        lim = price_limit_pct(code)
        if side == "BUY" and chg >= lim - 0.002:
            return True
        if side == "SELL" and chg <= -lim + 0.002:
            return True
        return False

    # ---------- 指标 ----------

    def _metrics(self):
        eq = self.equity
        if len(eq) < 2:
            self.stats = {"error": "样本不足"}
            return
        vals = [e[1] for e in eq]
        rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals))
                if vals[i - 1] > 0]
        total_ret = vals[-1] / vals[0] - 1
        years = len(rets) / 252.0
        ann = (vals[-1] / vals[0]) ** (1 / years) - 1 if years > 0 else 0.0
        peak = vals[0]
        mdd = 0.0
        for v in vals:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        mean = sum(rets) / len(rets) if rets else 0.0
        var = (sum((r - mean) ** 2 for r in rets) / len(rets)
               if len(rets) > 1 else 0.0)
        std = math.sqrt(var)
        sharpe = (mean / std * math.sqrt(252)) if std > 0 else 0.0
        wins = [t for t in self.trades if t["side"] == "SELL"]
        win_n = sum(1 for t in wins if t["realized"] > 0)
        bench_curve = self._bench_curve(eq)
        index_curve = self._index_curve(eq)
        self.stats = {
            "range": [eq[0][0], eq[-1][0]],
            "days": len(eq),
            "capital": self.capital,
            "final_asset": vals[-1],
            "total_return": total_ret,
            "annual": ann,
            "max_drawdown": mdd,
            "sharpe": sharpe,
            "trades": len(self.trades),
            "closed": len(wins),
            "win_rate": (win_n / len(wins)) if wins else None,
            "fees": self.account.fees,
            "realized_pnl": self.account.realized_pnl,
            "bench_total_return": bench_curve.get("total"),
            "bench_annual": bench_curve.get("annual"),
            "excess_total": (total_ret - bench_curve["total"]
                             if bench_curve.get("total") is not None else None),
            "index_code": index_curve.get("code"),
            "index_total_return": index_curve.get("total"),
            "index_annual": index_curve.get("annual"),
            "index_mdd": index_curve.get("mdd"),
            "index_excess": (total_ret - index_curve["total"]
                             if index_curve.get("total") is not None else None),
            "config": {
                "risk_mode": self.risk_mode,
                "max_positions": self.max_positions,
                "max_position_pct": self.max_position_pct,
                "exec_px": self.exec_px,
                "stop": self.stop_cfg,
                "min_bars": self.min_bars,
            },
        }

    def _bench_curve(self, eq):
        """等权全市场基准：按日收益累计，对齐回测区间。"""
        start, end = eq[0][0], eq[-1][0]
        dates = [d for d in sorted(self.bench) if start <= d <= end]
        if not dates:
            return {}
        curve, val = [], 1.0
        for d in dates:
            s, n = self.bench[d]
            val *= (1 + (s / n if n else 0.0))
            curve.append((d, val))
        years = len(curve) / 252.0
        total = curve[-1][1] - 1
        ann = (curve[-1][1]) ** (1 / years) - 1 if years > 0 else 0.0
        peak, mdd = curve[0][1], 0.0
        for _d, v in curve:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        return {"curve": curve, "total": total, "annual": ann, "mdd": mdd}

    def _index_curve(self, eq):
        """指数基准（默认 sh000001，取自缓存）。"""
        code = (self.cfg.get("backtest", {}) or {}).get("benchmark") \
            or "sh000001"
        try:
            rows = bridge.db_rows(code)
        except Exception:
            return {}
        start, end = eq[0][0], eq[-1][0]
        pts = [(r["date"], r["close"]) for r in rows
               if start <= r["date"] <= end and r["close"]]
        if len(pts) < 2:
            return {}
        base = pts[0][1]
        curve = [(d, c / base) for d, c in pts]
        years = len(curve) / 252.0
        total = curve[-1][1] - 1
        ann = (curve[-1][1]) ** (1 / years) - 1 if years > 0 else 0.0
        peak, mdd = curve[0][1], 0.0
        for _d, v in curve:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        return {"code": code, "curve": curve, "total": total,
                "annual": ann, "mdd": mdd}

    def save(self, tag=None):
        os.makedirs(RESULTS_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"stock_backtest_{tag or stamp}"
        result = {
            "generated": datetime.now().isoformat(),
            "stats": self.stats,
            "equity": self.equity,
            "bench_curve": self._bench_curve(self.equity).get("curve", []),
            "index_curve": self._index_curve(self.equity).get("curve", []),
            "trades": self.trades,
        }
        jpath = os.path.join(RESULTS_DIR, name + ".json")
        with open(jpath, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, default=str)
        mpath = os.path.join(RESULTS_DIR, name + ".md")
        with open(mpath, "w", encoding="utf-8") as f:
            f.write(result_markdown(result))
        return jpath
