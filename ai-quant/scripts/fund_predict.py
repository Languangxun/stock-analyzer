#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""场外基金 · 历史形态相似度净值预测（ai-quant 集成版）

与桌面版 stock_predict 同源算法，针对场外基金改造：
  - 数据源：东方财富天天基金净值历史（复用 ai-quant 的 Provider）
  - 场外基金无盘中高低价/成交量 → 预测目标为下一交易日净值涨跌区间
  - 保留 大盘(上证指数当日涨跌) 匹配维度；剔除量能维度

用法：
  cd ~/ai-quant && .venv/bin/python scripts/fund_predict.py [基金代码]
示例：scripts/fund_predict.py 007301

作者：獨白 (kingrux106@gmail.com / QQ 2180287399)
免责声明：仅为历史数据统计研究用途，不构成投资建议，盈亏自负。
"""
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta

W_WINDOW, TOPK = 10, 10
QT_URL = "https://qt.gtimg.cn/q="
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def http_get(url, retries=3, timeout=15):
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(url,
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("gbk", errors="ignore")
        except Exception as e:
            last = e
            time.sleep(1.5 * (a + 1))
    raise RuntimeError(f"网络请求失败: {last}")


def pctile(vals, p):
    s = sorted(vals)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    f = k - lo
    return s[lo] * (1 - f) + s[hi] * f


def znorm(w):
    m = sum(w) / len(w)
    sd = (sum((x - m) ** 2 for x in w) / len(w)) ** 0.5 or 1e-12
    return [(x - m) / sd for x in w]


def logret(seq):
    return [math.log(seq[i + 1] / seq[i]) for i in range(len(seq) - 1)]


def fetch_nav_history(fund_code):
    """场外基金净值历史 [{date, nav}]。优先用本地缓存文件，失败在线拉取。"""
    # 本地缓存（fetch_all 维护）
    nav_file = os.path.join(BASE_DIR, "data", "history", "fund_nav.json")
    if os.path.exists(nav_file):
        try:
            with open(nav_file, encoding="utf-8") as f:
                data = json.load(f)
            for sym, info in data.items():
                if info.get("fund_code") == fund_code:
                    pts = [{"date": p["date"], "nav": float(p["nav"])}
                           for p in info["points"] if float(p["nav"]) > 0]
                    if len(pts) >= W_WINDOW + TOPK + 30:
                        return pts
        except Exception:
            pass
    # 在线拉取（东财天天基金历史净值，分页）
    rows = []
    page = 1
    while len(rows) < 250 and page <= 6:
        hist_url = (f"https://api.fund.eastmoney.com/f10/lsjz"
                    f"?fundCode={fund_code}&pageIndex={page}&pageSize=49")
        req = urllib.request.Request(hist_url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": f"https://fundf10.eastmoney.com/jjjz_{fund_code}.html",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"净值接口失败: {e}")
        data = d.get("Data") or {}
        lsjz = data.get("LSJZList") or []
        if not lsjz:
            break
        for it in lsjz:                            # 接口倒序 → 暂存
            if it.get("DWJZ"):
                rows.append({"date": it["FSRQ"], "nav": float(it["DWJZ"])})
        total = int(d.get("TotalCount") or 0)
        if page * 49 >= total:
            break
        page += 1
        time.sleep(0.3)
    rows.reverse()                                 # 转正序
    if len(rows) < W_WINDOW + TOPK + 30:
        raise ValueError(f"基金 {fund_code} 净值样本不足({len(rows)})")
    return rows


def fetch_index_daily():
    """上证指数日K [{date, close}]。"""
    kd = json.loads(http_get(
        KLINE_URL + "?param=sh000001,day,,,500,qfq", timeout=20))
    d = kd["data"]["sh000001"]
    bars = d.get("qfqday") or d.get("day")
    return [(b[0], float(b[2])) for b in bars if float(b[2]) > 0]


def analyze_fund(fund_code):
    navs = fetch_nav_history(fund_code)
    closes = [p["nav"] for p in navs]
    dates = [p["date"] for p in navs]
    rets = logret(closes)

    # 大盘上下文
    try:
        idx_rows = fetch_index_daily()
        idx_chg_by_date = {
            b[0]: (b[1] / a[1]) * 100 - 100
            for a, b in zip(idx_rows, idx_rows[1:])
        }
        qf = http_get(QT_URL + "sh000001").split("~")
        idx_chg_today = ((float(qf[3]) / float(qf[4])) * 100 - 100
                         if len(qf) > 34 and qf[3] else None)
    except Exception:
        idx_chg_by_date, idx_chg_today = {}, None

    cur = znorm(rets[-W_WINDOW:])
    sims = []
    for i in range(W_WINDOW, len(rets) - 1):
        w = znorm(rets[i - W_WINDOW:i])
        d_px = sum((a - b) ** 2 for a, b in zip(cur, w)) ** 0.5
        ic = idx_chg_by_date.get(dates[i])
        d_i = (abs(ic - idx_chg_today)
               if (ic is not None and idx_chg_today is not None) else None)
        score = d_px + (min(1.5, 0.3 * d_i) if d_i is not None else 0.40)
        sims.append((score, i))
    sims.sort(key=lambda x: x[0])
    top = sims[:TOPK]

    samples = []
    for _, i in top:
        r, n1 = navs[i], navs[i + 1]
        ic = idx_chg_by_date.get(dates[i])
        samples.append({
            "t_date": dates[i], "n1_date": n1["date"],
            "t_nav": r["nav"], "n1_nav": n1["nav"],
            "cl_c": n1["nav"] / r["nav"] - 1,
            "idx_d": (ic - idx_chg_today
                      if (ic is not None and idx_chg_today is not None)
                      else None),
        })
    # 分层：大盘接近(±0.8pp)优先
    sel = [s for s in samples
           if s["idx_d"] is not None and abs(s["idx_d"]) <= 0.8]
    src = sel if len(sel) >= 3 else samples
    filter_note = "大盘(±0.8pp)筛选" if len(sel) >= 3 else "使用全部样本"

    last_nav = closes[-1]
    t_pred = {}
    for p in (10, 25, 50, 75, 90):
        chg = pctile([s["cl_c"] for s in src], p)
        t_pred[p] = {"chg_pct": round(chg * 100, 2),
                     "nav": round(last_nav * (1 + chg), 4)}
    up_prob = len([s for s in src if s["cl_c"] > 0]) / len(src)

    return {
        "fund_code": fund_code,
        "last_nav": last_nav, "last_date": dates[-1],
        "t_pred": t_pred, "up_prob": up_prob,
        "samples": samples, "src_n": len(src),
        "filtered": len(sel) >= 3,
        "idx_chg_today": idx_chg_today,
        "bars_used": len(navs),
    }


def main():
    code = sys.argv[1] if len(sys.argv) > 1 else input("基金代码：").strip()
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6:
        print("错误：请输入6位基金代码，如 007301")
        sys.exit(1)

    res = analyze_fund(code)
    tp = res["t_pred"]
    print("=" * 60)
    print(f"场外基金 {res['fund_code']}  最新净值 {res['last_nav']:.4f}"
          f"（{res['last_date']}，共{res['bars_used']}个净值点）")
    idx_txt = (f"{res['idx_chg_today']:+.2f}%"
               if res["idx_chg_today"] is not None else "未知")
    print(f"大盘(上证)今日 {idx_txt}   筛选: "
          f"{('大盘±0.8pp' if res['filtered'] else '全部样本')}")
    print("-" * 60)
    print("明日(T+1)净值预测 [锚定最新净值]")
    print(f"{'分位':<6}{'涨跌':>9}{'净值':>10}")
    for pp in (10, 25, 50, 75, 90):
        print(f"P{pp:<5}{tp[pp]['chg_pct']:>+8.2f}%{tp[pp]['nav']:>10.4f}")
    print(f"上涨概率: {res['up_prob']*100:.0f}%  "
          f"(样本 {res['src_n']}/{len(res['samples'])})")
    print("-" * 60)
    print("相似历史参考（T日 → 次日净值变化）")
    for s in res["samples"]:
        mark = "*" if (s["idx_d"] is not None and abs(s["idx_d"]) <= 0.8) else ""
        print(f"  {s['t_date']} → {s['n1_date']}  "
              f"{s['n1_nav']:.4f} ({s['cl_c']*100:+.2f}%) {mark}")
    print("=" * 60)
    print("注：场外基金为净值型，无盘中高低价；申购按金额、T+1确认。")
    print("作者：獨白 kingrux106@gmail.com QQ:2180287399")
    print("免责声明：仅为历史统计研究，不构成投资建议，盈亏自负。")


if __name__ == "__main__":
    main()
