#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_strategy_portfolio.py - 每股自动消融选出的策略 → 组合回测

读取 research/strategy_ablation_per_stock.json（每股自动选型结果），
把每股选出的策略聚合成组合回测。两种口径：

  --mode slot    （默认）v4 式仓位槽位：共享资金、单仓 frac、最多 max_pos 仓，
                 涨跌停/停牌/滑点与 v4 组合回测一致；组合指标可比。
  --mode sleeve  等权独立袖套：每股 1/N 资金独立运行，再平均权益曲线。

验证段（--segment val）为样本外：选型只在训练段完成。

口径（2026-09-13 修正，旧成绩作废需重跑）：
  - 早盘信号：T 日决策只用 T-1 信息（信号映射到执行日+1、ATR 取前一日），
    T 日收盘执行；止损为前一日挂单、T 日盘中触发合法；
  - 生存者偏差修复：val 用统一近端 252 日窗口，不再按结束日剔除退市股，
    窗口内退市/长停持仓按最后收盘价了结（stale_days=20）；
  - 默认不允许涨停价买入（旧口径可用 --allow-limit-up-tiers 显式恢复）。
  旧文档中的 +34.2% / val +14.3% 等数字产生于上述修正之前的同根 K 线成交
  与结束日对齐口径，不可再引用。

新三档（2026-09-13 定版，保守废弃）：
  稳健 = 原稳健选型（Calmar；不启用弱市覆盖）
  均衡 = 原激进选型 + 弱市覆盖（弱市收紧止损 + 停开仓）= 上证冠军配置
  激进 = 冠军逻辑的「不停开仓」版（最大参与，弱市只收紧止损）

用法：
  python backtest_strategy_portfolio.py --tier all --segment val --benchmark sh000001
  python backtest_strategy_portfolio.py --tier 稳健 --mode sleeve
"""
import argparse
import datetime as _dt
import json
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

import stock_gui as sg
from backtest_strategy_ablation import (
    load_stocks, _trim_rows, ALGO_LABEL, PER_STOCK_FILE)

# 新三档；消融 JSON 旧档位键（保守/稳健/激进）读入时用 LEGACY_MODE 映射，
# 以后消融若按新键生成（稳健/均衡/激进）则直接读取。
TIERS = ("稳健", "均衡", "激进")
LEGACY_MODE = {"稳健": "稳健", "均衡": "激进", "激进": "激进"}
SLOT_CFG = {
    "稳健": {"frac": 0.20, "max_pos": 6},
    "均衡": {"frac": 0.33, "max_pos": 4},
    "激进": {"frac": 0.33, "max_pos": 4},
}
# 均衡档弱市覆盖：-0.6% 阈值、ATR×0.5、弱市停开仓
# 注意：(ATR×0.5, -0.6%) 是旧口径下用 val/h2 人工选出的参数，统一窗口+
# 早盘信号修正后需要重新做 train→val 复核，勿把旧 val 数字当 OOS。
BASELINE_WEAK = {"th": -0.006, "scale": 0.5, "skip": True}
# 激进档（2026-09-13 定版）：同逻辑但不停开仓（最大参与）
BASELINE_WEAK_AGGR = {"th": -0.006, "scale": 0.5, "skip": False}
# 冠军覆盖默认挂在哪一档（2026-09-13：原激进→均衡）
OVERLAY_TIER = "均衡"
_GEN = {
    "macd": sg._sig_macd, "kdj": sg._sig_kdj, "rsi": sg._sig_rsi,
    "boll": sg._sig_boll, "ma_trend": sg._sig_ma_trend,
    "l1_pattern": sg._sig_l1_pattern,
}


def _gen_signals(rows, sel):
    rp = dict(sel["params"])
    if sel["algo"] == "composite":
        return sg._composite_signals(rows, rp, idx_chg_by_date=None), rp
    gen = _GEN.get(sel["algo"])
    if gen is None:
        return None, rp
    return gen(rows), rp


def pick_from_all(cands, objective, mode_filter=None):
    """按训练集目标从该股全部候选中重选（防过拟合：只看 train）。
    mode_filter: 限定候选的风险参数档（如 {'保守','稳健'}）。"""
    if not cands:
        return None
    MIN_TR = 8
    pool = [c for c in cands if c["train"].get("trades", 0) >= MIN_TR]
    if not pool:
        pool = [c for c in cands if c["train"].get("trades", 0) >= 3]
    if not pool:
        pool = list(cands)
    if mode_filter:
        pool = [c for c in pool if c.get("mode") in mode_filter]
    if not pool:
        return None

    def _calmar(m):
        return m.get("ann", 0) / max(abs(m.get("mdd", 0.05)), 0.05)

    if objective == "mdd":
        pool.sort(key=lambda c: (abs(c["train"].get("mdd", 0.05)),
                                 -c["train"].get("ann", -1)))
    elif objective == "calmar":
        pool.sort(key=lambda c: -_calmar(c["train"]))
    elif objective == "ann":
        pool.sort(key=lambda c: -c["train"].get("ann", -1))
    else:
        raise SystemExit(f"未知选型目标: {objective}")
    return dict(pool[0])


def _seg_range(n, seg):
    val_n = max(200, n // 4)
    split = n - val_n
    return {"train": (0, split), "val": (split, n), "all": (0, n)}[seg]


# ---------------- slot 模式（v4 式组合） ----------------

def _worker_slot(args):
    code, rows, sels, seg = args[:4]
    lo, hi = (args[4] if len(args) > 4 and args[4] else (None, None))
    rows = _trim_rows(rows)
    n = len(rows)
    out = {"code": code, "dates": None, "buy": {}, "sell": {}, "rp": {},
           "o": None, "h": None, "l": None, "c": None, "atr": None}
    if n < 200:
        return out
    i0, i1 = _seg_range(n, seg)
    # 统一近端窗口：按日期把评估段裁到公共窗口（生存者偏差修复）
    while i0 < i1 and lo and rows[i0]["date"] < lo:
        i0 += 1
    while i1 > i0 and hi and rows[i1 - 1]["date"] > hi:
        i1 -= 1
    if i1 - i0 < 20:
        return out
    seg_rows = rows[i0:i1]
    out["dates"] = [r["date"] for r in seg_rows]
    out["o"] = np.array([r["open"] or 0.0 for r in seg_rows], np.float32)
    out["h"] = np.array([r["high"] or 0.0 for r in seg_rows], np.float32)
    out["l"] = np.array([r["low"] or 0.0 for r in seg_rows], np.float32)
    out["c"] = np.array([r["close"] or 0.0 for r in seg_rows], np.float32)
    # 早盘信号：决策在 T 日早盘（只能用 T-1 信息），T 日收盘执行；
    # 止损单在前一日收盘后设定，故执行日用 T-1 的 ATR
    atr_full = sg._precompute_atr(rows, 0, n)
    out["atr"] = np.array([atr_full[j - 1] if j > 0 else 0.0
                           for j in range(i0, i1)], np.float32)
    dset = {r["date"]: k for k, r in enumerate(seg_rows)}
    for tier in TIERS:
        sel = (sels or {}).get(tier)
        if not sel:
            continue
        try:
            sigs, rp = _gen_signals(rows, sel)
        except Exception:
            continue
        if not sigs:
            continue
        buys, sells = set(), set()
        for s in sigs:
            j = s[0] + 1                 # 执行日 = 信号日 + 1
            if j < i0 or j >= i1:
                continue
            k = dset.get(rows[j]["date"])
            if k is None:
                continue
            (buys if s[2] == "BUY" else sells).add(k)
        out["buy"][tier] = buys
        out["sell"][tier] = sells
        out["rp"][tier] = rp
    return out


def build_matrices(results, keep_idx, cal):
    didx = {d: k for k, d in enumerate(cal)}
    ns, nc = len(keep_idx), len(cal)
    M = {k: np.full((ns, nc), np.nan, np.float32)
         for k in ("open", "high", "low", "close", "atr")}
    buy = {t: np.zeros((ns, nc), bool) for t in TIERS}
    sell = {t: np.zeros((ns, nc), bool) for t in TIERS}
    rps = {}
    for row, gi in enumerate(keep_idx):
        r = results[gi]
        pairs = [(k, d) for k, d in enumerate(r["dates"]) if d in didx]
        if not pairs:
            continue
        cols = np.array([didx[d] for _, d in pairs], np.int64)
        src_idx = [k for k, _ in pairs]
        for src, dst in (("o", "open"), ("h", "high"), ("l", "low"),
                         ("c", "close"), ("atr", "atr")):
            M[dst][row, cols] = np.asarray(r[src], np.float32)[src_idx]
        for tier in TIERS:
            b, s = r["buy"].get(tier), r["sell"].get(tier)
            if b:
                buy[tier][row, [didx[r["dates"][k]] for k in b
                                if r["dates"][k] in didx]] = True
            if s:
                sell[tier][row, [didx[r["dates"][k]] for k in s
                                 if r["dates"][k] in didx]] = True
            if tier in r["rp"]:
                rps[(row, tier)] = r["rp"][tier]
    M["has_bar"] = np.isfinite(M["close"])
    ret1 = np.full((ns, nc), np.nan, np.float32)
    with np.errstate(invalid="ignore", divide="ignore"):
        ret1[:, 1:] = M["close"][:, 1:] / M["close"][:, :-1] - 1.0
    lim = np.array([sg._v4_limit_pct(results[i]["code"])
                    for i in keep_idx], np.float64)[:, None]
    with np.errstate(invalid="ignore"):
        M["limit_up"] = ret1 >= (lim - 0.005)
        M["limit_dn"] = ret1 <= -(lim - 0.005)
    return M, buy, sell, rps


def market_weak_mask(cal, thr, index="sh000001"):
    """按指数近5日日均收益 < thr 生成弱市日历掩码。thr=None 全 False。"""
    mask = np.zeros(len(cal), bool)
    if thr is None:
        return mask
    try:
        rows = sg.get_daily(index)
    except Exception:
        return mask
    ret = {}
    prev = None
    for r in rows:
        c = r.get("close") or 0.0
        if prev and prev > 0 and c > 0:
            ret[r["date"]] = c / prev - 1.0
        if c:
            prev = c
    vals = [ret.get(d, np.nan) for d in cal]
    for i in range(len(vals)):
        seg = [v for v in vals[max(0, i - 4):i + 1] if np.isfinite(v)]
        if len(seg) >= 3 and (sum(seg) / len(seg)) < thr:
            mask[i] = True
    return mask


def benchmark_metrics(code, cal):
    """基准指数在组合日历上的标准化权益指标（缺失日顺延前一收盘价）。

    用于「跑赢上证/深证/创业板」的对照：超额 = 组合年化 - 指数年化。"""
    try:
        rows = sg.get_daily(code)
    except Exception:
        return None
    if not rows:
        return None
    m = {r["date"]: (r.get("close") or 0.0) for r in rows}
    vals, last = [], None
    for d in cal:
        v = m.get(d)
        if v:
            last = v
        vals.append(last)
    if not vals or not vals[0]:
        return None
    eq = [v / vals[0] for v in vals]
    years = max((_dt.date.fromisoformat(cal[-1])
                 - _dt.date.fromisoformat(cal[0])).days / 365.25, 0.25)
    total = eq[-1] - 1.0
    ann = eq[-1] ** (1.0 / years) - 1.0 if eq[-1] > 0 else -1.0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    daily = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq))
             if eq[i - 1] > 0]
    sd = (sum(x * x for x in daily) / len(daily)) ** 0.5 if daily else 0.0
    return {"code": code, "total": total, "ann": ann, "mdd": mdd,
            "calmar": ann / abs(mdd) if mdd < -1e-9 else None,
            "sharpe": (sum(daily) / len(daily)) / sd * math.sqrt(252.0)
            if sd > 1e-12 else None, "days": len(cal)}


def index_timing_mask(cal, code, ma_w=60):
    """指数趋势闸门（防前视）：第 t 日是否在场 = 截至 t-1 收盘 > MA(ma_w)。
    返回 bool 数组（True=在场可持仓/开仓）。"""
    try:
        rows = sg.get_daily(code)
    except Exception:
        return None
    if not rows:
        return None
    cl = {r["date"]: (r.get("close") or 0.0) for r in rows}
    vals, last = [], None
    for d in cal:
        v = cl.get(d)
        if v:
            last = v
        vals.append(last)
    if not vals or not vals[0]:
        return None
    n = len(vals)
    mask = np.zeros(n, bool)
    for t in range(n):
        # 用 t-1 及以前的数据判断第 t 日是否在场
        if t < ma_w + 1:
            continue
        prev = vals[t - 1]
        seg = [v for v in vals[:t] if v][-ma_w:]
        if len(seg) < ma_w or not prev:
            continue
        mask[t] = prev > sum(seg) / len(seg)
    return mask


def slot_sim(cal, codes, M, buy, sell, rps, cfg,
             weak_mask=None, weak_atr_scale=1.0, weak_skip_entry=False,
             invest_mask=None, invest_exit=True, allow_limit_up=False,
             stale_days=20, exec_mode=None):
    nc, ns = len(cal), len(codes)
    buy_mult, sell_mult = 1.001, 0.999
    # 成交价口径：close=信号次日收盘（默认）；open=信号次日开盘
    exec_open = ((exec_mode or os.environ.get("EXEC_PX", "close"))
                 == "open")
    cash, pos = 1e6, {}
    last_px = np.zeros(ns, np.float64)
    eq_curve, trades = [], []
    for t in range(nc):
        col = M["close"][:, t]
        upd = np.isfinite(col)
        last_px[upd] = col[upd].astype(np.float64)
        # 早盘信号：T 日执行时，弱市判定只能用 T-1 收盘前信息
        weak = (bool(weak_mask[t - 1])
                if (weak_mask is not None and t > 0) else False)
        risk_off = (invest_mask is not None and not invest_mask[t])
        if risk_off and invest_exit:
            # 趋势闸门关闭：全部平仓（跌停顺延），当日不再开仓
            for ks in sorted(pos):
                if not M["has_bar"][ks, t] or not col[ks]:
                    continue
                if M["limit_dn"][ks, t]:
                    continue
                px = (float(M["open"][ks, t]) if exec_open else float(col[ks]))
                if not (px and px > 0):
                    px = float(col[ks])
                net = px * sell_mult
                cash += pos[ks]["shares"] * net
                trades.append({"ret": net / pos[ks]["buy_net"] - 1.0,
                               "pnl": pos[ks]["shares"]
                               * (net - pos[ks]["buy_net"]),
                               "hold": t - pos[ks]["t_in"]})
                del pos[ks]
        for ks in sorted(pos):
            if not M["has_bar"][ks, t]:
                p = pos[ks]
                p["miss"] = p.get("miss", 0) + 1
                if p["miss"] >= stale_days:
                    # 退市/长期停牌：按最后已知收盘价了结
                    px = last_px[ks] if last_px[ks] > 0 else p["entry"]
                    net = px * sell_mult
                    cash += p["shares"] * net
                    trades.append({"ret": net / p["buy_net"] - 1.0,
                                   "pnl": p["shares"]
                                   * (net - p["buy_net"]),
                                   "hold": t - p["t_in"]})
                    del pos[ks]
                continue                # 停牌顺延
            p = pos[ks]
            px_c = float(col[ks])
            px_o = float(M["open"][ks, t])
            hi = float(M["high"][ks, t])
            lo = float(M["low"][ks, t])
            prev_high = p["highest"]
            sold, px = False, None
            if not M["limit_dn"][ks, t]:
                rp = rps[ks]
                atr_t = float(M["atr"][ks, t])
                am = rp["atr_mult"] * (weak_atr_scale if weak else 1.0)
                if prev_high > p["entry"] * rp["trail_trigger"]:
                    stop = prev_high * rp["trail_ratio"]
                elif atr_t > 0:
                    stop = p["entry"] - am * atr_t
                else:
                    stop = p["entry"] * 0.95
                if lo <= stop:
                    px = px_o if px_o <= stop else min(stop, hi)
                    sold = True
                elif sell[ks, t]:
                    px = px_o if exec_open else px_c
                    sold = True
            if sold:
                net = px * sell_mult
                cash += p["shares"] * net
                trades.append({"ret": net / p["buy_net"] - 1.0,
                               "pnl": p["shares"] * (net - p["buy_net"]),
                               "hold": t - p["t_in"]})
                del pos[ks]
            else:
                p["highest"] = max(prev_high, hi)
        if len(pos) < cfg["max_pos"]:
            ok = buy[:, t] & M["has_bar"][:, t]
            if not allow_limit_up:
                ok = ok & ~M["limit_up"][:, t]
            cand = np.nonzero(ok)[0]
            if risk_off:
                cand = cand[:0]         # 趋势闸门关闭：当日不开新仓
            elif weak and weak_skip_entry:
                cand = cand[:0]         # 弱市只平不开（进场闸门，非仓位缩放）
            if len(cand) > 1:           # 名额不足：低波动优先，保证可复现
                atrp = np.where(col[cand] > 0,
                                M["atr"][cand, t] / col[cand], np.inf)
                cand = cand[np.argsort(atrp, kind="stable")]
            for ks in cand:
                if len(pos) >= cfg["max_pos"] or ks in pos:
                    continue
                px_c = float(col[ks])
                if not px_c:
                    continue
                px_fill = (float(M["open"][ks, t]) if exec_open else px_c)
                if not (px_fill and px_fill > 0):
                    px_fill = px_c
                buy_net = px_fill * buy_mult
                eq0 = cash + sum(pp["shares"] * last_px[k2]
                                 for k2, pp in pos.items())
                shares = int(eq0 * cfg["frac"] / buy_net / 100.0) * 100
                if shares <= 0 or shares * buy_net > cash:
                    continue
                cash -= shares * buy_net
                pos[ks] = {"t_in": t, "shares": shares, "buy_net": buy_net,
                           "entry": px_fill,
                           "highest": px_fill}  # 成交前的盘中高点不计入
        eq_curve.append(cash + sum(pp["shares"] * last_px[k2]
                                   for k2, pp in pos.items()))
    for ks in sorted(pos):
        pp = pos[ks]
        px = last_px[ks] if last_px[ks] > 0 else pp["entry"]
        net = px * sell_mult
        cash += pp["shares"] * net
        trades.append({"ret": net / pp["buy_net"] - 1.0,
                       "pnl": pp["shares"] * (net - pp["buy_net"]),
                       "hold": nc - 1 - pp["t_in"]})
    m = sg._v4_metrics(eq_curve, cal, trades)
    m["equity"] = eq_curve
    m["dates"] = cal
    return m


# ---------------- sleeve 模式（等权独立袖套） ----------------

def _worker_sleeve(args):
    code, rows, sel, seg = args[:4]
    lo, hi = (args[4] if len(args) > 4 and args[4] else (None, None))
    if not sel:
        return {"code": code, "curve": None, "trade_rets": [], "sel": None}
    rows = _trim_rows(rows)
    n = len(rows)
    if n < 200:
        return {"code": code, "curve": None, "trade_rets": [], "sel": sel}
    i0, i1 = _seg_range(n, seg)
    while i0 < i1 and lo and rows[i0]["date"] < lo:
        i0 += 1
    while i1 > i0 and hi and rows[i1 - 1]["date"] > hi:
        i1 -= 1
    if i1 - i0 < 20:
        return {"code": code, "curve": None, "trade_rets": [], "sel": sel}
    try:
        sigs, rp = _gen_signals(rows, sel)
        if not sigs:
            return {"code": code, "curve": None, "trade_rets": [],
                    "sel": sel}
        atrs = sg._precompute_atr(rows, 0, n)
        trets = []
        bt = sg._bt_events(rows, sigs, rp, i0, i1, atrs=atrs,
                           trade_out=trets)
    except Exception:
        return {"code": code, "curve": None, "trade_rets": [], "sel": sel}
    if not bt:
        return {"code": code, "curve": None, "trade_rets": [], "sel": sel}
    return {"code": code, "curve": bt["curve"], "trade_rets": trets,
            "sel": sel, "d0": rows[i0]["date"], "d1": rows[i1 - 1]["date"]}


def run_sleeve(tier, selections, stocks, seg, workers, bounds=(None, None)):
    args = [(c, r, (selections.get(c) or {}).get(tier), seg, bounds)
            for c, r in stocks]
    curves, trets, d0s, d1s, dist = [], [], [], [], {}
    with ProcessPoolExecutor(max_workers=workers) as exe:
        futs = [exe.submit(_worker_sleeve, a) for a in args]
        done = 0
        for fut in as_completed(futs):
            r = fut.result()
            done += 1
            s = r.get("sel")
            if s:
                dist[s["algo"]] = dist.get(s["algo"], 0) + 1
            curves.append(r["curve"] if r["curve"] else [1.0])
            trets.extend(r["trade_rets"])
            if r.get("d0"):
                d0s.append(r["d0"])
                d1s.append(r["d1"])
    L = max(len(c) for c in curves)
    M = np.ones((len(curves), L))
    for i, c in enumerate(curves):
        M[i, :len(c)] = c
    pf = M.mean(axis=0)
    years = 1.0
    if d0s and d1s:
        d0 = sorted(d0s)[len(d0s) // 2]
        d1 = sorted(d1s)[len(d1s) // 2]
        try:
            years = max((_dt.date.fromisoformat(d1)
                         - _dt.date.fromisoformat(d0)).days / 365.25, 0.25)
        except Exception:
            years = max(L / 250.0, 0.25)
    m = _sleeve_metrics(pf, years, trets, len(curves))
    m["selection_dist"] = dist
    return m


def _sleeve_metrics(eq, years, trade_rets, n_stocks):
    eq = np.asarray(eq, float)
    n = len(eq)
    out = {"total": 0.0, "ann": 0.0, "mdd": 0.0, "sharpe": None,
           "vol": 0.0, "winrate": None, "pf": None, "trades": 0,
           "avg_hold": 0.0, "calmar": None, "n_stocks": n_stocks, "days": n}
    if n < 2 or eq[0] <= 0:
        return out
    mult = float(eq[-1] / eq[0])
    out["total"] = mult - 1.0
    out["ann"] = mult ** (1.0 / years) - 1.0 if mult > 0 else -1.0
    daily = np.diff(eq) / eq[:-1]
    sd = float(daily.std())
    out["vol"] = sd * math.sqrt(252.0)
    out["sharpe"] = float(daily.mean()) / sd * math.sqrt(252.0) \
        if sd > 1e-12 else None
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    out["mdd"] = float(mdd)
    if trade_rets:
        wins = [t for t in trade_rets if t > 0]
        losses = [t for t in trade_rets if t <= 0]
        out["trades"] = len(trade_rets)
        out["winrate"] = len(wins) / len(trade_rets)
        gp, gl = sum(wins), -sum(losses)
        out["pf"] = (gp / gl) if gl > 1e-9 else None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="all",
                    choices=tuple(TIERS) + ("all",))
    ap.add_argument("--segment", default="val",
                    choices=("train", "val", "all"))
    ap.add_argument("--mode", default="slot",
                    choices=("slot", "sleeve"))
    ap.add_argument("--workers", type=int,
                    default=max(1, min(8, os.cpu_count() or 4)))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--align-days", type=int, default=-1,
                    help="≥0=旧口径（按测试段结束日对齐，隐含幸存者偏差）；"
                         "<0=统一近端窗口（默认，窗口内退市股保留、"
                         "持仓按最后收盘价了结）")
    ap.add_argument("--min-active", type=int, default=0,
                    help="组合日历裁剪：只保留当日有K线股票数≥N 的日期"
                         "（去掉少数长停牌股撑出的稀疏早期日历）")
    ap.add_argument("--select", action="append", default=[],
                    metavar="档位=目标",
                    help="按训练集目标重选（mdd/calmar/ann），"
                         "如 --select 稳健=calmar；可多次")
    ap.add_argument("--no-overlay", action="store_true",
                    help="关闭均衡档冠军 baseline 的弱市覆盖")
    ap.add_argument("--universe", default="all",
                    choices=("all", "chinext", "main"),
                    help="股票池：all=全A，chinext=创业板(sz30*)，main=主板(sh60/sz00)")
    ap.add_argument("--weak-index", default="sh000001",
                    help="弱市判定所用指数（默认上证；创业板策略用 sz399006）")
    ap.add_argument("--timing-index", default=None,
                    help="指数趋势闸门（防前视）：如 sz399006；关闭=不启用")
    ap.add_argument("--timing-ma", type=int, default=60,
                    help="趋势闸门均线周期（默认 60）")
    ap.add_argument("--timing-tiers", nargs="*", default=[],
                    choices=list(TIERS),
                    help="只对这些档启用趋势闸门（默认全部）")
    ap.add_argument("--timing-no-exit", action="store_true",
                    help="闸门关闭时只禁开仓、不平仓（默认清仓）")
    ap.add_argument("--allow-limit-up-tiers", nargs="*", default=[],
                    choices=list(TIERS),
                    help="允许涨停价买入（打板）的档位；默认全不允许"
                         "（早盘信号+收盘成交下涨停无法保证成交，"
                         "显式指定可复现旧口径）")
    ap.add_argument("--exec", dest="exec_px",
                    default=os.environ.get("EXEC_PX", "close"),
                    choices=("close", "open"),
                    help="成交价：close=信号次日收盘（默认）；"
                         "open=信号次日开盘")
    ap.add_argument("--tag", default="", help="输出文件后缀标签")
    ap.add_argument("--frac", type=float, default=None,
                    help="覆盖单仓比例（所有档位）")
    ap.add_argument("--max-pos", type=int, default=None,
                    help="覆盖最大持仓数（所有档位）")
    ap.add_argument("--half", type=int, default=0, choices=(0, 1, 2),
                    help="只跑组合日历的前/后 1/2 段（稳健性拆分）")
    ap.add_argument("--weak-atr-scale", type=float, default=1.0,
                    help="弱市 ATR 止损倍数缩放（<1 收紧；不改仓位）")
    ap.add_argument("--weak-mkt-th", type=float, default=None,
                    help="弱市判定阈值：上证近5日日均收益 < th（如 -0.006）")
    ap.add_argument("--weak-skip-entry", action="store_true",
                    help="弱市日不开新仓（只平不开；需配合 --weak-mkt-th）")
    ap.add_argument("--weak-tiers", nargs="*", default=[],
                    choices=list(TIERS),
                    help="按档启用经验证的弱市覆盖（ATR×0.5, mkt5<-0.6%%，"
                         "见 README 覆盖层寻优）；其它档保持基线")
    ap.add_argument("--benchmark", default="sh000001",
                    help="基准指数代码（默认上证 sh000001；传空串禁用），"
                         "输出各档超额年化")
    args = ap.parse_args()
    os.environ["EXEC_PX"] = args.exec_px     # 供 slot_sim/_bt_events 读取
    print(f"  成交价口径: {args.exec_px}")
    overrides = {}
    for s in args.select:
        if "=" not in s:
            raise SystemExit("--select 需形如 保守=calmar")
        t, o = s.split("=", 1)
        overrides[t.strip()] = o.strip()
    t0 = time.time()
    here = os.path.dirname(os.path.abspath(__file__))
    per = os.path.join(here, PER_STOCK_FILE)
    if not os.path.exists(per):
        raise SystemExit(f"缺少 {per}，先运行 backtest_strategy_ablation.py")
    print(f"加载 {per} ...")
    with open(per, encoding="utf-8") as f:
        data = json.load(f)
    selections = {}
    for d in data:
        if not d:
            continue
        mc_old = dict(d.get("mode_candidates", {}))
        if "均衡" in mc_old:            # 新键生成的消融结果
            mc = {t: mc_old.get(t) for t in TIERS if mc_old.get(t)}
            if not mc.get("激进"):
                mc["激进"] = mc.get("均衡")
        else:                            # 旧键（保守/稳健/激进）映射
            mc = {t: mc_old.get(src) for t, src in LEGACY_MODE.items()
                  if mc_old.get(src)}
        for t, o in overrides.items():
            obj, _, mf = o.partition("@")
            mset = set(x for x in mf.replace("，", "+").split("+") if x) \
                if mf else None
            picked = pick_from_all(d.get("all_candidates"), obj, mset)
            if picked:
                mc[t] = picked
        selections[d["code"]] = mc
    if overrides:
        print(f"  选型覆盖（训练集目标）: {overrides}")
    print(f"  股票 {len(selections)} 只")
    print("加载日K ...")
    stocks = load_stocks(min_bars=400)
    if args.universe == "chinext":
        stocks = [(c, r) for c, r in stocks if c.startswith("sz30")]
        print(f"  创业板股票池: {len(stocks)} 只")
    elif args.universe == "main":
        stocks = [(c, r) for c, r in stocks
                  if c.startswith("sh60") or c.startswith("sz00")]
        print(f"  主板股票池: {len(stocks)} 只")
    if args.limit:
        stocks = stocks[:args.limit]
    tiers = TIERS if args.tier == "all" else (args.tier,)
    slot_cfg = {t: dict(c) for t, c in SLOT_CFG.items()}
    if args.frac is not None or args.max_pos is not None:
        for t in slot_cfg:
            if args.frac is not None:
                slot_cfg[t]["frac"] = args.frac
            if args.max_pos is not None:
                slot_cfg[t]["max_pos"] = args.max_pos
        print(f"  槽位覆盖: frac={args.frac} max_pos={args.max_pos}")
    print(f"  股票 {len(stocks)} 只，segment={args.segment}, mode={args.mode}")

    # 生存者偏差修复（默认 align_days<0）：统一近端窗口 252 个交易日，
    # 窗口内退市/长停股票保留，持仓由 stale_days 规则了结。
    bounds = (None, None)
    if args.align_days < 0:
        idx_dates = [r["date"] for r in sg.get_daily("sh000001")
                     if r.get("close")]
        if len(idx_dates) <= 252:
            raise SystemExit("指数日历不足，无法构建统一窗口")
        win_i = len(idx_dates) - 252
        win_start = idx_dates[win_i]
        prev_start = idx_dates[win_i - 1] if win_i > 0 else None
        if args.segment == "val":
            bounds = (win_start, None)
            print(f"  统一窗口 {win_start} ~ {idx_dates[-1]}")
        elif args.segment == "train":
            bounds = (None, prev_start)
            print(f"  训练段截至统一窗口前一日 ≤{prev_start}")
        elif args.segment == "all":
            # 全样本：统一到近端 1000 个交易日，避免退市股的远古片段
            # （1990s）混入组合日历
            n_win = min(1000, len(idx_dates) - 1)
            bounds = (idx_dates[-n_win], None)
            print(f"  统一窗口（全样本近端{n_win}日） "
                  f"{idx_dates[-n_win]} ~ {idx_dates[-1]}")

    out = {}
    cal = [""]
    if args.mode == "sleeve":
        for tier in tiers:
            print(f"  [{tier}] 等权袖套回测 ...")
            m = run_sleeve(tier, selections, stocks, args.segment,
                           args.workers, bounds)
            m["selection_dist"] = {ALGO_LABEL.get(k, k): v
                                   for k, v in m["selection_dist"].items()}
            m["tier"] = tier
            out[tier] = m
    else:
        print("生成每股所选策略信号 ...")
        argl = [(c, r, selections.get(c), args.segment, bounds)
                for c, r in stocks]
        results = [None] * len(argl)
        with ProcessPoolExecutor(max_workers=args.workers) as exe:
            futs = {exe.submit(_worker_slot, a): i
                    for i, a in enumerate(argl)}
            done = 0
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    results[i] = fut.result()
                except Exception:
                    results[i] = {"code": argl[i][0], "dates": None,
                                  "buy": {}, "sell": {}, "rp": {}}
                done += 1
                if done % 1000 == 0:
                    print(f"  {done}/{len(argl)}")
        ends = [r["dates"][-1] for r in results if r.get("dates")]
        if not ends:
            raise SystemExit("无有效股票")
        if args.align_days >= 0:
            last_d = max(ends)
            ld = _dt.date.fromisoformat(last_d)
            keep_idx = [i for i, r in enumerate(results)
                        if r.get("dates")
                        and (ld - _dt.date.fromisoformat(
                            r["dates"][-1])).days <= args.align_days]
            print(f"  时间对齐保留 {len(keep_idx)}/{len(results)} 只"
                  f"（测试段结束距 {last_d} ≤{args.align_days} 天）")
        else:
            keep_idx = [i for i, r in enumerate(results) if r.get("dates")]
            print(f"  窗口内有效 {len(keep_idx)}/{len(results)} 只"
                  f"（窗口内退市股保留，持仓按最后收盘价了结）")
        cal = sorted({d for i in keep_idx for d in results[i]["dates"]})
        if args.half in (1, 2):
            half = len(cal) // 2
            cal = cal[:half] if args.half == 1 else cal[half:]
            print(f"  使用第 {args.half} 半段日历 "
                  f"{cal[0]} ~ {cal[-1]}（{len(cal)} 日）")
        print(f"  组合日历 {cal[0]} ~ {cal[-1]}（{len(cal)} 个交易日）")
        M, buy, sell, rps = build_matrices(results, keep_idx, cal)
        if args.min_active > 0:
            hb = M["has_bar"].sum(axis=0)
            good = np.nonzero(hb >= args.min_active)[0]
            if len(good):
                a, b = int(good[0]), int(good[-1]) + 1
                if a > 0 or b < len(cal):
                    print(f"  活跃≥{args.min_active}只裁剪: "
                          f"{cal[a]} ~ {cal[b-1]}（{b-a} 日，原 {len(cal)} 日）")
                    cal = cal[a:b]
                    M = {k: v[:, a:b] for k, v in M.items()}
                    buy = {t: v[:, a:b] for t, v in buy.items()}
                    sell = {t: v[:, a:b] for t, v in sell.items()}
        print(f"  矩阵 {M['close'].shape[0]} 股 × {M['close'].shape[1]} 日")
        codes_kept = [results[i]["code"] for i in keep_idx]
        wmask_cache = {}

        def _weak_for(tier):
            """该档的 (mask, scale, skip)：显式参数 > --weak-tiers 预设 >
            均衡档冠军 baseline（弱市覆盖+停开仓）；--no-overlay 关闭。"""
            if args.weak_mkt_th is not None:
                th, sc, sk = (args.weak_mkt_th, args.weak_atr_scale,
                              getattr(args, "weak_skip_entry", False))
            elif tier in getattr(args, "weak_tiers", []):
                th, sc, sk = (-0.006, 0.5,
                              getattr(args, "weak_skip_entry", False))
            elif tier == OVERLAY_TIER and not args.no_overlay:
                th = BASELINE_WEAK["th"]
                sc = BASELINE_WEAK["scale"]
                sk = BASELINE_WEAK["skip"]
            elif tier == "激进" and not args.no_overlay:
                th = BASELINE_WEAK_AGGR["th"]
                sc = BASELINE_WEAK_AGGR["scale"]
                sk = BASELINE_WEAK_AGGR["skip"]
            else:
                return None, 1.0, False
            if th not in wmask_cache:
                wmask_cache[th] = market_weak_mask(cal, th,
                                                   args.weak_index)
            return wmask_cache[th], sc, sk

        timing_mask = None
        if args.timing_index:
            timing_mask = index_timing_mask(cal, args.timing_index,
                                            args.timing_ma)
            if timing_mask is None:
                print(f"  趋势闸门 {args.timing_index} 加载失败")
            else:
                print(f"  趋势闸门 {args.timing_index} MA{args.timing_ma}: "
                      f"在场日 {int(timing_mask.sum())}/{len(cal)}"
                      f"{'（只禁开仓）' if args.timing_no_exit else '（关闭日清仓）'}")

        for tier in tiers:
            rp_tier = {k: (rps.get((k, tier))
                           or sg.CFG.RISK_PARAMS["稳健"])
                       for k in range(len(codes_kept))}
            wm, sc, sk = _weak_for(tier)
            if wm is not None:
                print(f"  [{tier}] 弱市覆盖 {args.weak_index} ATR×{sc}"
                      f"{' 停开仓' if sk else ''} "
                      f"弱市日 {int(wm.sum())}/{len(cal)}")
            tm = timing_mask if (timing_mask is not None
                                 and (not args.timing_tiers
                                      or tier in args.timing_tiers)) else None
            m = slot_sim(cal, codes_kept, M, buy[tier], sell[tier],
                         rp_tier, slot_cfg[tier], wm, sc, sk,
                         tm, not args.timing_no_exit,
                         tier in args.allow_limit_up_tiers)
            dist = {}
            for c in codes_kept:
                sel = selections.get(c, {}).get(tier)
                if sel:
                    dist[sel["algo"]] = dist.get(sel["algo"], 0) + 1
            m["selection_dist"] = {ALGO_LABEL.get(k, k): v
                                   for k, v in dist.items()}
            m["segment"] = args.segment
            m["tier"] = tier
            m["n_stocks"] = len(codes_kept)
            out[tier] = m

    bench = None
    if args.mode == "slot" and args.benchmark:
        bench = benchmark_metrics(args.benchmark, cal)
        if bench:
            for tier in tiers:
                out[tier]["benchmark"] = bench
                out[tier]["excess_ann"] = out[tier]["ann"] - bench["ann"]
                out[tier]["excess_total"] = (out[tier].get("total", 0.0)
                                             - bench["total"])
        else:
            print(f"基准 {args.benchmark} 加载失败，跳过对照")

    print("\n=== 每股自动消融策略 · 组合回测 ===")
    if bench:
        print(f"基准 {bench['code']}: 年化 {bench['ann']*100:+.1f}% "
              f"回撤 {bench['mdd']*100:+.1f}% "
              f"Calmar {(bench['calmar'] or 0):+.2f} "
              f"Sharpe {(bench['sharpe'] or 0):+.2f} "
              f"（{cal[0]} ~ {cal[-1]}）")
    hdr = (f"{'档位':<6}{'年化':>9}{'回撤':>9}{'Calmar':>8}{'Sharpe':>8}"
           f"{'胜率':>7}{'PF':>6}{'交易':>7}{'均持仓':>7}"
           + (f"{'超额年化':>9}" if bench else ""))
    print(hdr)
    for tier in tiers:
        m = out[tier]
        line = (f"{tier:<6}{m['ann']*100:>+8.1f}%{m['mdd']*100:>+8.1f}%"
                f"{(m['calmar'] or 0):>+8.2f}{(m['sharpe'] or 0):>+8.2f}"
                f"{(m['winrate'] or 0)*100:>6.1f}%{(m['pf'] or 0):>6.2f}"
                f"{m['trades']:>7d}{(m['avg_hold'] or 0):>7.1f}")
        if bench:
            line += f"{(m.get('excess_ann') or 0)*100:>+8.1f}%"
        print(line)
        print(f"       算法分布: {m['selection_dist']}")

    suffix = f"_{args.tag}" if args.tag else ""
    path = os.path.join(here, "research",
                        f"strategy_portfolio_{args.mode}_{args.segment}"
                        f"{suffix}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%d %H:%M"),
                   "segment": args.segment, "mode": args.mode,
                   "align_days": args.align_days,
                   "calendar": [cal[0], cal[-1]] if args.mode == "slot"
                   else None, "results": out}, f, ensure_ascii=False,
                  indent=1)
    print(f"\n写入 {path}，耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
