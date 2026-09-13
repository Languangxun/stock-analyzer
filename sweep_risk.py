# -*- coding: utf-8 -*-
"""风险偏好三档网格寻优：
  激进=捕捉机会(信号松+止损松)  保守=重确认(信号严+止损紧)  稳健居中"""
import statistics
import itertools
import stock_gui as sg
from stock_gui import CFG
import backtest_bsmatrix as bb

stocks, idx_chg = bb.load()
print(f"样本 {len(stocks)} 只\n")


def evaluate(atr_mult, trail_trigger, trail_ratio, buy_th, cooldown):
    CFG.RISK_PARAMS["_t"] = {"atr_mult": atr_mult,
                             "trail_trigger": trail_trigger,
                             "trail_ratio": trail_ratio,
                             "buy_th": buy_th, "cooldown": cooldown}
    CFG.RISK_MODE = "_t"
    per = []
    for code, rows in stocks:
        try:
            sigs, algo, band = bb.gen_signals(rows, idx_chg)
            if not sigs:
                continue
            st = sg.backtest_signals(rows, sigs)
            if st and st.get("closed"):
                per.append(st)
        except Exception:
            continue
    if len(per) < 50:
        return None, per
    anns = [s["ann"] for s in per if s["ann"] is not None]
    closed = sum(s["closed"] for s in per)
    wr = sum(s["winrate"] * s["closed"] for s in per
             if s["winrate"] is not None) / max(1, closed)
    pl = [s["profit_loss"] for s in per if s["profit_loss"] != float("inf")]
    med = statistics.median(anns)
    return med, {"n": len(per), "trades": sum(s["trades"] for s in per),
                 "wr": wr, "pf": statistics.median(pl),
                 "ann_med": med, "ann_mean": statistics.mean(anns),
                 "pos": sum(1 for a in anns if a > 0) / len(anns)}


GRIDS = {
    "保守(信号严+止损紧)": {"buy_th": 3, "cooldown": 8,
                        "atr": (1.0, 1.5, 2.0),
                        "trail": ((1.01, 0.96), (1.02, 0.94))},
    "稳健(居中)": {"buy_th": 2, "cooldown": 5,
                 "atr": (1.5, 2.0),
                 "trail": ((1.02, 0.94), (1.03, 0.92))},
    "激进(捕捉机会+止损松)": {"buy_th": (1, 2), "cooldown": 3,
                          "atr": (2.5, 3.0, 3.5),
                          "trail": ((1.05, 0.90), (1.08, 0.86))},
}

best_all = {}
for mode, g in GRIDS.items():
    print(f"=== {mode} ===")
    results = []
    bths = g["buy_th"] if isinstance(g["buy_th"], tuple) else (g["buy_th"],)
    for bt in bths:
        for atr in g["atr"]:
            for tt, tr in g["trail"]:
                med, info = evaluate(atr, tt, tr, bt, g["cooldown"])
                if med is None:
                    continue
                results.append((med, bt, atr, tt, tr, info))
                print(f"  th{bt} cd{g['cooldown']} atr{atr} "
                      f"trail({tt},{tr}): 胜率{info['wr']*100:.1f}% "
                      f"PF{info['pf']:.2f} 年化中位{med*100:+.1f}% "
                      f"正占比{info['pos']*100:.0f}%")
    results.sort(key=lambda r: -r[0])
    if results:
        med, bt, atr, tt, tr, info = results[0]
        best_all[mode] = (bt, g["cooldown"], atr, tt, tr)
        print(f"  ★最优: th{bt} cd{g['cooldown']} atr{atr} "
              f"trail({tt},{tr}) → 年化中位{med*100:+.1f}%\n")

print("=== 最终三档参数 ===")
for mode, p in best_all.items():
    print(mode, p)
import json
with open("risk_sweep_results.json", "w", encoding="utf-8") as f:
    json.dump({m: {"buy_th": p[0], "cooldown": p[1], "atr_mult": p[2],
                   "trail_trigger": p[3], "trail_ratio": p[4]}
               for m, p in best_all.items()}, f, ensure_ascii=False, indent=1)
