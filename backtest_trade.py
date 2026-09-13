#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_trade.py - 交易口径回测：胜率/年化（不磕IC）

分组协议：
  trad(传统行业): L1 / L1+L2(同行业) / L1+L2(同行业+ETF池20只宽基行业ETF)
  theme(科技医药题材): 仅L1     etf: 仅L1
信号：预测P50收益 > 阈值 → 次日做多(close→close)，否则空仓。
指标： pooled胜率 / 组合年化(逐日等权合成) / 交易次数 / 对比买入持有。
"""
import math
import time

from backtest_levels import (load_all, match_pool, match_l1, fuse, rank_ic,
                             W, TAIL, TOPK)
import stock_gui as sg
from stock_gui import (_is_etf, logret, znorm, rsi_at,
                       vola_at, candle_feats, volchg_at, weekly_ctx,
                       vol_ratio_at, _limit_pct)

STEP = 6
TAIL = 180
N_TRAD, N_THEME, N_ETF = 40, 20, 20
L2_N, ETF_N = 30, 20
THRESHOLDS = (0.0, 0.003, 0.006, 0.01)


def precompute(rows, is_etf=False):
    closes = [r["close"] for r in rows]
    rets = logret(closes, is_etf=is_etf)
    vols = [r["vol"] for r in rows]
    n = len(rows)
    return {
        "dates": [r["date"] for r in rows],
        "closes": closes,
        "zwin": [znorm(rets[i - W:i]) if i >= W else None
                 for i in range(n + 1)],
        "struct": [candle_feats(rows, i) for i in range(n)],
        "vola": [vola_at(rets, i) for i in range(n)],
        "rsi": [rsi_at(closes, i) for i in range(n)],
        "volchg": [volchg_at(vols, i) for i in range(n)],
        "weekly": [weekly_ctx(rows, i, 4) for i in range(n)],
        "vr": [vol_ratio_at(vols, i) for i in range(n)],
    }


THEME_KW = ("软件", "计算机", "半导体", "元件", "电子", "通信", "光电",
            "IT", "互联网", "游戏", "传媒", "数字", "消费电子", "光学",
            "医药", "中药", "生物", "医疗", "制药", "疫苗")


def is_theme(ind):
    return any(k in (ind or "") for k in THEME_KW)


def main():
    t0 = time.time()
    by, meta = load_all()
    stocks = [(c, r) for c, r in by.items() if len(r) >= 400 and not _is_etf(c)
              and c in meta and meta[c][2]]
    stocks.sort(key=lambda cr: -len(cr[1]))
    trad = [c for c, _ in stocks if not is_theme(meta[c][2])][:N_TRAD]
    theme = [c for c, _ in stocks if is_theme(meta[c][2])][:N_THEME]
    etfs = [c for c, r in by.items() if _is_etf(c) and len(r) >= 250][:N_ETF]
    print(f"trad={len(trad)} theme={len(theme)} etf={len(etfs)}")

    feats = {}
    def F(c):
        if c not in feats:
            feats[c] = precompute(by[c], is_etf=_is_etf(c))
        return feats[c]
    for c in trad + theme + etfs:
        F(c)
    etf_feats = [F(c) for c in etfs]
    print(f"预计算 {len(feats)} 只 {time.time()-t0:.0f}s，开始评估...")

    # 收集：group -> {config -> [(pred, actual, date)]}
    R = {"trad_L1": [], "trad_L1L2行业": [], "trad_L1L2行业+ETF": [],
         "theme_L1": [], "etf_L1": []}
    n_eval = 0
    for code in trad:
        m = meta[code]
        ind = m[2]
        fp = F(code)
        peers = [c for c in trad if c != code and meta[c][2] == ind][:L2_N]
        for c in peers:
            F(c)
        n = len(fp["closes"])
        for t in range(max(2 * W + 2, n - TAIL), n - 1, STEP):
            d = fp["dates"][t]
            cur_win = fp["zwin"][t]
            if cur_win is None:
                continue
            cur_ctx = {"struct": fp["struct"][t], "vola": fp["vola"][t],
                       "rsi": fp["rsi"][t], "volchg": fp["volchg"][t],
                       "weekly": fp["weekly"][t]}
            vr_now = fp["vr"][t]
            s1 = match_l1(fp, t, cur_win, cur_ctx, vr_now)
            if len(s1) < 3:
                continue
            actual = fp["closes"][t + 1] / fp["closes"][t] - 1
            p_l1 = fuse([("L1", s1)])
            R["trad_L1"].append((p_l1, actual, d))
            s2i = []
            for pc in peers:
                s2i.extend(match_pool(F(pc), d, cur_win, cur_ctx, vr_now))
            s2i.sort(key=lambda s: s["similarity_score"]); s2i = s2i[:TOPK]
            R["trad_L1L2行业"].append(
                (fuse([("L1", s1), ("L2", s2i)]), actual, d))
            s2e = list(s2i)
            for ef in etf_feats:
                s2e.extend(match_pool(ef, d, cur_win, cur_ctx, vr_now))
            s2e.sort(key=lambda s: s["similarity_score"]); s2e = s2e[:TOPK]
            R["trad_L1L2行业+ETF"].append(
                (fuse([("L1", s1), ("L2", s2e)]), actual, d))
            n_eval += 1
    for grp, codes in (("theme_L1", theme), ("etf_L1", etfs)):
        for code in codes:
            fp = F(code)
            n = len(fp["closes"])
            for t in range(max(2 * W + 2, n - TAIL), n - 1, STEP):
                cur_win = fp["zwin"][t]
                if cur_win is None:
                    continue
                cur_ctx = {"struct": fp["struct"][t], "vola": fp["vola"][t],
                           "rsi": fp["rsi"][t], "volchg": fp["volchg"][t],
                           "weekly": fp["weekly"][t]}
                s1 = match_l1(fp, t, cur_win, cur_ctx, fp["vr"][t])
                if len(s1) < 3:
                    continue
                actual = fp["closes"][t + 1] / fp["closes"][t] - 1
                R[grp].append((fuse([("L1", s1)]), actual,
                               fp["dates"][t]))
                n_eval += 1

    print(f"评估点 {n_eval}，耗时 {time.time()-t0:.0f}s\n")

    def report(name, arr, n_days_per_stock):
        # 买入持有基线
        bh = [a for _, a, _ in arr]
        bh_ann = sum(bh) / len(bh) * 250
        out = [f"{name:<18} 买入持有年化≈{bh_ann*100:+5.1f}%"]
        best = None
        for th in THRESHOLDS:
            trades = [(p, a, d) for p, a, d in arr if p is not None and p > th]
            if len(trades) < 30:
                continue
            win = sum(1 for _, a, _ in trades if a > 0) / len(trades)
            # 组合年化：按日期聚合等权
            byd = {}
            for p, a, d in trades:
                byd.setdefault(d, []).append(a)
            days = sorted(byd)
            port = [sum(byd[d]) / len(byd[d]) for d in days]
            ann = 0.0
            comp = 1.0
            for r in port:
                comp *= (1 + r)
            ann = comp ** (250 / max(1, len(days))) - 1
            tag = ""
            if best is None or ann > best[0]:
                best = (ann, win, th, len(trades))
            out.append(f"   th>{th*100:.1f}%: 交易{len(trades):>4} "
                       f"胜率{win*100:5.1f}% 组合年化{ann*100:+6.1f}%")
        out.append(f"   ★最优: th>{best[2]*100:.1f}% 胜率{best[1]*100:.1f}% "
                   f"年化{best[0]*100:+.1f}% ({best[3]}笔)")
        print("\n".join(out))

    for name in ("trad_L1", "trad_L1L2行业", "trad_L1L2行业+ETF",
                 "theme_L1", "etf_L1"):
        report(name, R[name], TAIL)
    import json
    with open("trade_results.json", "w", encoding="utf-8") as f:
        json.dump({n: [(round(p,5) if p else None, round(a,5), d)
                       for p, a, d in v] for n, v in R.items()},
                  f, ensure_ascii=False)
    print(f"\n完成 {time.time()-t0:.0f}s → trade_results.json")


if __name__ == "__main__":
    main()
