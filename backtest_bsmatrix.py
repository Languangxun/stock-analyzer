#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_bsmatrix.py - 买卖点信号广样本收益回测（与桌面版信号系统同构）

协议：
  - 每股取缓存最近400根日K，信号只在其后120根上生成（与APP一致）
  - 多维打分（MACD/KDJ/RSI/量价/MA20/筹码/布林/ADX 加权）→ 方向切换+冷却+弱势过滤
  - 波段适合度<60 的股票改用趋势跟踪信号（与APP路由一致）
  - 交易模拟用 sg.backtest_signals（信号日收盘成交 + ATR止损 + 移动止盈）
输出：逐股指标 + 汇总（胜率/平均收益/盈亏比/年化/回撤/超额）
"""
import statistics
import time

import stock_gui as sg
from stock_gui import (calc_macd, calc_kdj, calc_rsi, calc_boll, calc_adx,
                       sma_period, vol_ratio_at, _is_etf, CFG,
                       _band_fit_score, _trend_track_signals, backtest_signals)
from stock_gui import db_conn

N_STOCKS = 300
TAIL = 400          # 每股取的K线根数


def load():
    with db_conn() as conn:
        idx = conn.execute("SELECT date, close FROM daily_bars "
                           "WHERE code='sh000001' ORDER BY date").fetchall()
        idx_chg = {b[0]: (b[1] / a[1]) * 100 - 100
                   for a, b in zip(idx, idx[1:])}
        rows = conn.execute(
            "SELECT code, date, open, high, low, close, vol "
            "FROM daily_bars ORDER BY code, date").fetchall()
    by = {}
    for c, d, o, h, l, cl, v in rows:
        by.setdefault(c, []).append(
            {"date": d, "open": o, "high": h, "low": l, "close": cl,
             "vol": v or 0.0})
    cands = [(c, r[-TAIL:]) for c, r in by.items()
             if len(r) >= TAIL and not _is_etf(c) and c != "sh000001"]
    cands.sort(key=lambda cr: -len(cr[1]))
    return cands[:N_STOCKS], idx_chg


def gen_signals(rows, idx_chg):
    """复刻 analyze 的多维打分信号生成（前120根窗口）"""
    n = len(rows)
    closes = [r["close"] for r in rows]
    dif, dea, _ = calc_macd(closes)
    k_, d_, _ = calc_kdj(rows)
    r6, _ = calc_rsi(closes, 6), calc_rsi(closes, 12)
    b_mid, b_up, b_low = calc_boll(closes)
    pdi_a, mdi_a, adx_a = calc_adx(rows)
    mas = {nn: sma_period(closes, nn) for nn in (20, 60)}
    vols_d = [r.get("vol") or 0.0 for r in rows]
    vr_arr = [vol_ratio_at(vols_d, k) for k in range(n)]
    try:
        chip_snaps = sg.chip_snapshots(rows, tail=120)
    except Exception:
        chip_snaps = {}

    start = max(1, n - 120)
    scores = []
    for i in range(start, n):
        if None in (dif[i], dea[i], dif[i - 1], dea[i - 1]):
            scores.append((i, rows[i]["date"], 0, []))
            continue
        sc = 0

        def _wadd(dim, pts):
            nonlocal sc
            sc += int(round(pts * CFG.IND_W.get(dim, 1.0)))
        # MACD
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            _wadd("MACD", 2)
        elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            _wadd("MACD", -2)
        elif dif[i] > dea[i]:
            _wadd("MACD", 1)
        else:
            _wadd("MACD", -1)
        # KDJ
        if k_[i - 1] <= d_[i - 1] and k_[i] > d_[i] and k_[i] < 45:
            _wadd("KDJ", 2)
        elif k_[i - 1] >= d_[i - 1] and k_[i] < d_[i] and k_[i] > 65:
            _wadd("KDJ", -2)
        elif k_[i] > d_[i]:
            _wadd("KDJ", 1)
        else:
            _wadd("KDJ", -1)
        # RSI
        if r6[i] is not None and r6[i - 1] is not None:
            if r6[i - 1] < 20 and r6[i] >= 20:
                _wadd("RSI", 2)
            elif r6[i - 1] > 80 and r6[i] <= 80:
                _wadd("RSI", -2)
            elif r6[i] < 30:
                _wadd("RSI", 1)
            elif r6[i] > 70:
                _wadd("RSI", -1)
        # 量价
        c, cp = rows[i]["close"], rows[i - 1]["close"]
        v5 = sum(vols_d[max(0, i - 5):i]) / max(1, min(5, i))
        vr_d = vols_d[i] / v5 if v5 > 0 else 0.0
        if vr_d > 1.5 and c > cp:
            _wadd("量价", 1)
        elif vr_d > 1.5 and c < cp:
            _wadd("量价", -1)
        # MA20
        ma20, ma20p = mas[20][i], mas[20][i - 1]
        if ma20 and ma20p:
            if c > ma20 and ma20 > ma20p:
                _wadd("MA20", 1)
            elif c < ma20 and ma20 < ma20p:
                _wadd("MA20", -1)
        # 筹码
        snap = chip_snaps.get(rows[i]["date"])
        if snap:
            sup_i, res_i = snap[0], snap[1]
            if sup_i and c <= sup_i * 1.01:
                _wadd("筹码", 1)
            elif res_i and c >= res_i * 0.99:
                _wadd("筹码", -1)
        # 布林带
        bu_i, bl_i = b_up[i], b_low[i]
        if None not in (bu_i, bl_i):
            if c < bl_i:
                _wadd("布林带", 1)
            elif c > bu_i:
                _wadd("布林带", -1)
            elif rows[i - 1]["close"] <= (b_low[i - 1] or 0) and c > bl_i:
                _wadd("布林带", 1)
            elif rows[i - 1]["close"] >= (b_up[i - 1] or 1e18) and c < bu_i:
                _wadd("布林带", -1)
        # ADX
        a_i, p_i, m_i = adx_a[i], pdi_a[i], mdi_a[i]
        if None not in (a_i, p_i, m_i) and a_i >= 20:
            if p_i > m_i:
                _wadd("ADX", 1)
            elif m_i > p_i:
                _wadd("ADX", -1)
        scores.append((i, rows[i]["date"], sc, []))

    def weak_day(day):
        ic = idx_chg.get(day)
        return ic is not None and ic < CFG.WEAK_IDX_TH

    signals = []
    prev_dir = 0
    cooldown = 0
    for idx_i, day, sc, _rs in scores:
        if cooldown > 0:
            cooldown -= 1
            continue
        buy_th = CFG.SIGNAL_SCORE_BUY + 1 if weak_day(day) \
            else CFG.SIGNAL_SCORE_BUY
        if sc >= buy_th and prev_dir <= 0:
            signals.append((idx_i, day, "BUY", ""))
            prev_dir = 1
            cooldown = CFG.SIGNAL_COOLDOWN
        elif sc <= CFG.SIGNAL_SCORE_SELL and prev_dir >= 0:
            signals.append((idx_i, day, "SELL", ""))
            prev_dir = -1
            cooldown = CFG.SIGNAL_COOLDOWN

    # 波段路由
    band_score = _band_fit_score(rows, mas, vr_arr)
    if band_score < CFG.BAND_FIT_MIN:
        signals = _trend_track_signals(rows, mas, idx_chg, None)
        algo = "趋势"
    else:
        algo = "波段"
    return signals, algo, band_score


def main():
    t0 = time.time()
    stocks, idx_chg = load()
    print(f"样本 {len(stocks)} 只（≥{TAIL}根K线），窗口=后120根生成信号")
    per = []
    all_trades = []
    algo_stat = {"波段": [], "趋势": []}
    t0 = time.time()
    for k, (code, rows) in enumerate(stocks):
        try:
            sigs, algo, band = gen_signals(rows, idx_chg)
        except Exception:
            continue
        if not sigs:
            continue
        try:
            st = backtest_signals(rows, sigs)
        except Exception:
            continue
        if not st or not st.get("trades"):
            continue
        # 买入持有基线（同期）
        d0 = rows[sigs[0][0]]["close"]
        bh = rows[-1]["close"] / d0 - 1
        st["code"] = code
        st["algo"] = algo
        st["band"] = band
        st["bh"] = bh
        per.append(st)
        algo_stat[algo].append(st)
        # 汇集逐笔收益：粗略用每股平均代替（backtest_signals不返回逐笔列表）
        if k % 100 == 0:
            print(f"  {k}/{len(stocks)} {time.time()-t0:.0f}s")

    print(f"\n=== 汇总（{len(per)} 只有信号的股票） ===")
    tr_total = sum(s["trades"] for s in per)
    closed = sum(s["closed"] for s in per)
    wins = sum(s["wins"] for s in per)
    # 逐笔胜率（按每股笔数加权）
    win_rate = sum(s["winrate"] * s["closed"] for s in per
                   if s["winrate"] is not None) / max(1, closed)
    anns = [s["ann"] for s in per if s["ann"] is not None]
    mdds = [s["mdd"] for s in per]
    bhs = [s["bh"] for s in per]
    pl = [s["profit_loss"] for s in per if s["profit_loss"] != float("inf")]
    print(f"交易总笔数: {tr_total}（已平仓 {closed}）")
    print(f"加权胜率: {win_rate*100:.1f}%")
    print(f"平均盈利/平均亏损 盈亏比: 中位 {statistics.median(pl):.2f}" if pl else "")
    print(f"单股年化: 中位 {statistics.median(anns)*100:+.1f}%  "
          f"均值 {statistics.mean(anns)*100:+.1f}%  "
          f"正数占比 {sum(1 for a in anns if a > 0)/len(anns)*100:.0f}%")
    print(f"单股最大回撤: 中位 {statistics.median(mdds)*100:.1f}%")
    print(f"买入持有: 中位 {statistics.median(bhs)*100:+.1f}%  "
          f"均值 {statistics.mean(bhs)*100:+.1f}%")
    beat = sum(1 for s in per if (s["ann"] or 0) > 0)
    print(f"信号策略年化为正的股票: {beat}/{len(per)}")

    for algo, lst in algo_stat.items():
        if not lst:
            continue
        a = [s["ann"] for s in lst if s["ann"] is not None]
        w = sum(s["winrate"] * s["closed"] for s in lst
                if s["winrate"] is not None) / max(1, sum(s["closed"] for s in lst))
        print(f"\n[{algo}] {len(lst)}股 交易{sum(s['trades'] for s in lst)}笔 "
              f"加权胜率{w*100:.1f}% 年化中位 {statistics.median(a)*100:+.1f}%")

    # Top / Flop
    per.sort(key=lambda s: -(s["ann"] or 0))
    print("\n年化TOP5:", [(s["code"], f"{s['ann']*100:.0f}%") for s in per[:5]])
    print("年化FLOP5:", [(s["code"], f"{s['ann']*100:.0f}%") for s in per[-5:]])

    import json
    out = [{k2: (round(v2, 4) if isinstance(v2, float) else v2)
            for k2, v2 in s.items()} for s in per]
    with open("bs_matrix_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"\n完成 {time.time()-t0:.0f}s → bs_matrix_results.json")


if __name__ == "__main__":
    main()
