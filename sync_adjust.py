#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_adjust.py - 批量刷新 adjust 缩放系数（hfq → 乘法前复权 ≈ 现价）

库内价格一律为后复权(hfq)。跨股票比较价格水平（低价分位、整手成本、
价格上限）需要现价口径：qfq(t) = hfq(t) * k，k = 最新不复权价 / hfq 末价。
腾讯批量快照一次可取多只，失败自动降级为单只重试。

用法：
  python sync_adjust.py            # 刷新全部有日K的代码
  python sync_adjust.py --limit 50 # 测试
"""
import argparse
import sqlite3
import sys
import time
import urllib.request

DB = "stock_cache.db"
QT = "https://qt.gtimg.cn/q="
UA = {"User-Agent": "Mozilla/5.0"}


def fetch_batch(codes):
    url = QT + ",".join(codes)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("gbk", "ignore")
    out = {}
    for line in raw.split(";"):
        line = line.strip()
        if not line.startswith("v_"):
            continue
        try:
            code = line[2:line.index("=")]
            parts = line[line.index('"') + 1:line.rindex('"')].split("~")
            px = float(parts[3])
            if px > 0:
                out[code] = px
        except Exception:
            continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=80)
    args = ap.parse_args()
    con = sqlite3.connect(DB)
    last = dict(con.execute(
        "select code, max(date) from daily_bars group by code"))
    hfq = {}
    for code, d, c in con.execute(
            "select code, date, close from daily_bars where close is not null"):
        if last.get(code) == d:
            hfq[code] = float(c)
    codes = sorted(c for c in hfq if c.startswith(
        ("sh", "sz", "bj")))
    if args.limit:
        codes = codes[:args.limit]
    con.execute("CREATE TABLE IF NOT EXISTS adjust("
                "code TEXT PRIMARY KEY, k REAL, ts REAL)")
    n_ok = n_fail = 0
    t0 = time.time()
    for i in range(0, len(codes), args.batch):
        batch = codes[i:i + args.batch]
        try:
            px = fetch_batch(batch)
        except Exception:
            px = {}
        missing = [c for c in batch if c not in px]
        for c in missing[:8]:
            try:
                px.update(fetch_batch([c]))
            except Exception:
                pass
        for c in batch:
            p = px.get(c)
            h = hfq.get(c)
            if p and h and h > 0:
                con.execute("INSERT OR REPLACE INTO adjust VALUES(?,?,?)",
                            (c, p / h, time.time()))
                n_ok += 1
            else:
                n_fail += 1
        con.commit()
        if (i // args.batch) % 10 == 0:
            print(f"  {i + len(batch)}/{len(codes)} ok={n_ok} fail={n_fail}"
                  f" {time.time() - t0:.0f}s", flush=True)
    con.close()
    print(f"完成：ok={n_ok} fail={n_fail} 用时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
