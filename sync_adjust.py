#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_adjust.py - 批量刷新 adjust 缩放系数 + 末根异常修复 + 基期漂移重迁

库内价格一律为后复权(hfq)。跨股票比较价格水平（低价分位、整手成本、
价格上限）需要现价口径：qfq(t) = hfq(t) * k，k = 同一交易日 raw/hfq。

2026-09-16/17 修复：
  ① 旧版用「实时价 / 库内最后一根」配对，库内日K落后时会把整段历史价格
     按最新涨跌幅缩放错（京东方A 昨收显示成今日价）；
  ② k 用同一交易日进行配对，取最近有效日；
  ③ 末根 bar 与邻近日比值明显不一致（腾讯偶发复权毛刺）时自动重拉修复，
     拉不到一致值则删除该根（宁可少一天，不可坏一天）；
  ④ 若「远端 hfq 与库内 hfq」多日整体不一致 → 基期漂移（分红重定基未
     合并），交给 data_clean.migrate_one 整只重迁。

用法：
  python sync_adjust.py            # 刷新全部有日K的代码
  python sync_adjust.py --limit 50 # 测试
"""
import argparse
import json
import sqlite3
import statistics
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

DB = "stock_cache.db"
KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
UA = {"User-Agent": "Mozilla/5.0"}


def fetch_daily(code, fq="hfq", count=15):
    """腾讯日K（多域名轮换 + 全局限速 + 重试）：fq='hfq' 后复权 / '' 不复权。"""
    import data_clean as dc
    param = (f"?param={code},day,,,{count},{fq}" if fq
             else f"?param={code},day,,,{count},")
    txt = None
    for host in dc.TX_HOSTS:
        try:
            txt = dc._get(host + param, retries=1, timeout=10)
            if txt:
                break
        except Exception:
            txt = None
    if not txt:
        raise RuntimeError("all hosts failed")
    d = (json.loads(txt).get("data") or {}).get(code) or {}
    bars = d.get("hfqday" if fq == "hfq" else "day") or d.get("day") or []
    out = {}
    for b in bars:
        try:
            if float(b[2]) > 0:
                out[b[0]] = float(b[2])
        except (ValueError, IndexError):
            continue
    return out


def scan_code(code):
    """返回 (k, fix, rebase)。fix: None/'delete'/新收盘价；rebase: 需整只重迁。"""
    con = sqlite3.connect(DB, timeout=30)
    try:
        db = {d: c for d, c in con.execute(
            "select date,close from daily_bars where code=? "
            "order by date desc limit 15", (code,))}
    finally:
        con.close()
    if not db:
        return None, None, False
    raw = fetch_daily(code, "", count=15)
    common = sorted(set(db) & set(raw))
    ratios = {d: raw[d] / db[d] for d in common if db[d] and raw[d] > 0}
    if not ratios:
        return None, None, False
    last_d = max(db)
    others = [v for d, v in ratios.items() if d != last_d]
    base = statistics.median(others) if others else ratios.get(
        last_d, 0.0)
    if not base:
        return None, None, False
    # 基期漂移：远端 hfq 与库内 hfq 多日整体不一致
    hfq = fetch_daily(code, "hfq", count=15)
    diff_ratio = 0
    checked = 0
    for d in common[:-1] if len(common) > 1 else common:
        v = hfq.get(d)
        if v and db[d]:
            if abs((raw[d] / v) / (raw[d] / db[d]) - 1) > 0.01:
                diff_ratio += 1
            checked += 1
    rebase = checked >= 3 and diff_ratio >= 3
    if rebase:
        return base, None, True
    k = ratios.get(last_d, base)
    fix = None
    if last_d in ratios and abs(k / base - 1) > 0.01:
        for _ in range(3):
            v = hfq.get(last_d)
            if v:
                r = raw[last_d] / v
                if abs(r / base - 1) <= 0.02:
                    fix = v
                    k = r
                    break
        if fix is None:
            fix = "delete"
            k = base
    return k, fix, False


def apply_fix(con, code, k, fix):
    if k and k > 0:
        con.execute("INSERT OR REPLACE INTO adjust VALUES(?,?,?)",
                    (code, float(k), time.time()))
    if fix == "delete":
        con.execute("DELETE FROM daily_bars WHERE code=? AND date="
                    "(SELECT MAX(date) FROM daily_bars WHERE code=?)",
                    (code, code))
        return "del"
    if isinstance(fix, float):
        row = con.execute("SELECT date,open,high,low,close FROM daily_bars "
                          "WHERE code=? ORDER BY date DESC LIMIT 1",
                          (code,)).fetchone()
        if row and row[4]:
            r = fix / row[4]
            con.execute("UPDATE daily_bars SET open=?,high=?,low=?,close=? "
                        "WHERE code=? AND date=?",
                        (row[1] * r, row[2] * r, row[3] * r, fix,
                         code, row[0]))
            return "fix"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()
    con = sqlite3.connect(DB)
    codes = [r[0] for r in con.execute(
        "select distinct code from daily_bars where code like 'sh%' "
        "or code like 'sz%' or code like 'bj%'")]
    old = {c: k for c, k in con.execute("select code,k from adjust")}
    if args.limit:
        codes = codes[:args.limit]
    con.execute("CREATE TABLE IF NOT EXISTS adjust("
                "code TEXT PRIMARY KEY, k REAL, ts REAL)")
    con.close()
    lock = threading.Lock()
    stats = {"ok": 0, "fail": 0, "chg": 0, "fix": 0, "del": 0, "rebase": 0}
    todo_rebase = []
    t0 = time.time()

    def work(c):
        try:
            return c, scan_code(c)
        except Exception:
            return c, (None, None, False)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, c) for c in codes]
        for i, fut in enumerate(as_completed(futs), 1):
            c, (k, fix, rebase) = fut.result()
            if rebase:
                with lock:
                    todo_rebase.append(c)
            else:
                with lock:
                    lcon = sqlite3.connect(DB, timeout=60)
                    try:
                        act = apply_fix(lcon, c, k, fix)
                        lcon.commit()
                    finally:
                        lcon.close()
                    if k and k > 0:
                        stats["ok"] += 1
                        o = old.get(c)
                        if o and abs(k / o - 1) > 0.01:
                            stats["chg"] += 1
                    else:
                        stats["fail"] += 1
                    if act == "fix":
                        stats["fix"] += 1
                    elif act == "del":
                        stats["del"] += 1
            if i % 500 == 0:
                print(f"  {i}/{len(codes)} {stats} "
                      f"{time.time() - t0:.0f}s", flush=True)
    # 基期漂移代码：整只重迁（替换为远端 hfq）
    if todo_rebase:
        print(f"基期漂移 {len(todo_rebase)} 只，整只重迁 ...", flush=True)
        try:
            import data_clean as dc
            con = sqlite3.connect(DB, timeout=60)
            names = {r[0]: (r[1] or "") for r in
                     con.execute("select code,name from stocks")}
            ok = 0
            for c in todo_rebase:
                try:
                    st = dc.migrate_one(con, c, names.get(c, ""), force=True)
                    ok += 1 if st == "ok" else 0
                except Exception:
                    pass
            con.close()
            stats["rebase"] = ok
        except Exception as e:
            print("重迁失败:", e)
    print(f"完成：{stats} 用时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
