#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_full.py - 全A股日K批量回填（目标≥1000交易日）

复用 stock_gui.py 内嵌缓存层（同一 stock_cache.db / 同一套多源HTTP）。
- 主源：腾讯 **不复权**日K（800 根翻页）× 新浪后复权因子 = 真乘法后复权；
  备源东财 fqt=2。v6.1.5 热修⑪ 起口径修正——腾讯 hfq 实测为分段仿射
  `hfq≈a×不复权价+b`，段内日收益被逐股缩放（浦发 0.63/茅台 0.82/申华 1.75），
  不能再直写入库；详见 data_clean.check_adj 与 reports/数据异常报告_20260927.md
- 库内统一存乘法后复权（见 data_clean.py / stock_gui._bf_fetch_one）
- 写入前与库内重叠日期比对（收盘偏差>1% 判为口径冲突，跳过该只）
- 写入后 `_sync_adjust` 刷新显示缩放系数 k（与 GUI/CLI 同口径）
- 断点续传：本地已有 ≥950 根且最新日期够新的代码直接跳过
- 并发 8 线程 + 全局限流（_http_get 内置 0.16s 间隔），进度每20只打印
- 代码表缺失时自动刷新全市场代码表（含市值分层，GUI直接受益）

用法：
  python backfill_full.py            # 全量回填（断点续传）
  python backfill_full.py --limit 50 # 只回填50只（测试）
  python backfill_full.py --force    # 忽略断点，全部重拉
  python backfill_full.py --min-bars 2000 --fresh-days 3 --bar-count 2400
                                     # 目标/过期/单次根数可调（GUI「数据工具」同参数）
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import stock_gui as sg  # noqa: E402  （内嵌缓存层：DB/多源HTTP/代码表刷新）

MIN_BARS = 950           # 断点续传门槛（目标1000根，留余量）
FRESH_DAYS = 6           # 最新bar距今超过6个自然日视为过期（吸收长假）
BAR_COUNT = 1100         # 单只目标下限根数（与设置页「最大拉取样本量」取大者）


def _fresh_date():
    import datetime
    return (datetime.date.today() - datetime.timedelta(days=FRESH_DAYS)
            ).isoformat()


def _local_progress():
    """{code: (bars, last_date)}，一次查询。"""
    with sg.db_conn() as conn:
        rows = conn.execute(
            "SELECT code, COUNT(*), MAX(date) FROM daily_bars "
            "GROUP BY code").fetchall()
    return {r[0]: (r[1], r[2] or "") for r in rows}


# 腾讯三域名（配额按 域名×IP 组合计，轮换可延长连续批量请求寿命）
_TX_HOSTS = [
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
]


def _fetch_one(full):
    """单只：腾讯不复权翻页 + 新浪复权因子（真乘法后复权）；东财兜底。

    v6.1.5 热修⑪：统一走 `stock_gui._bf_fetch_one`，不再直写腾讯 hfq
    （仿射失真口径）。新浪因子不可得时抛异常，由上层重试/保留原数据。"""
    target = max(MIN_BARS, sg.CFG.MAX_FETCH_BARS, BAR_COUNT, 1600)
    rows, _ = sg._bf_fetch_one(full, page=800, target=target)
    return rows


def _store(full, rows):
    today = time.strftime("%Y-%m-%d")
    data = [r for r in rows if r["date"] < today and sg._bar_ok(r)]
    if not data:
        raise RuntimeError("过滤后无有效数据")
    # 口径冲突防护（v6.1.5 热修⑩）：与库内重叠日期比对收盘，
    # 偏差>1% 说明源口径不同（如 qfq/hfq 混用）→ 拒绝写入，避免接缝污染
    with sg.db_conn() as conn:
        old = {d: c for d, c in conn.execute(
            "SELECT date, close FROM daily_bars WHERE code=?",
            (full,)).fetchall()}
    if old:
        common = [r for r in data if r["date"] in old][-20:]
        bad = [r["date"] for r in common
               if old.get(r["date"]) and r["close"]
               and abs(r["close"] / old[r["date"]] - 1) > 0.01]
        if bad:
            raise RuntimeError(
                f"口径冲突({len(bad)}/{len(common)}处，如 {bad[0]})，已跳过")
    with sg.db_conn(commit=True) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO daily_bars"
            "(code,date,open,high,low,close,vol) VALUES(?,?,?,?,?,?,?)",
            [(full, r["date"], r["open"], r["high"], r["low"],
              r["close"], r["vol"]) for r in data])
    # 刷新显示缩放系数 k（raw/后复权同交易日配对），与 GUI/CLI 读库口径一致
    try:
        sg._sync_adjust(full, data)
    except Exception:
        pass
    return len(data)


def main():
    global MIN_BARS, FRESH_DAYS, BAR_COUNT
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前N只(测试)")
    ap.add_argument("--force", action="store_true", help="忽略断点全部重拉")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--throttle", type=float, default=0.45,
                    help="全局请求最小间隔秒(防501限流)")
    ap.add_argument("--min-bars", type=int, default=MIN_BARS,
                    help=f"断点续传门槛（默认{MIN_BARS}根）")
    ap.add_argument("--fresh-days", type=int, default=FRESH_DAYS,
                    help=f"最新bar距今超过该自然日数视为过期（默认{FRESH_DAYS}）")
    ap.add_argument("--bar-count", type=int, default=BAR_COUNT,
                    help=f"单只目标根数下限（默认{BAR_COUNT}）")
    args = ap.parse_args()

    MIN_BARS = max(100, int(args.min_bars))
    FRESH_DAYS = max(1, int(args.fresh_days))
    BAR_COUNT = max(300, int(args.bar_count))

    sg._MIN_INTERVAL = args.throttle     # 批量模式放缓全局节流
    # 探测可用腾讯域，排到轮换队列最前（主域可能被批量限流501）
    for k, u in enumerate(list(_TX_HOSTS)):
        if sg._probe_kline_url(u):
            if k:
                _TX_HOSTS[0], _TX_HOSTS[k] = u, _TX_HOSTS[0]
            print(f"腾讯源就绪: {u.split('/')[2]}")
            break
    else:
        print("警告: 所有腾讯域探测失败，仍尝试轮换")

    fresh = _fresh_date()
    # 1) 代码表
    with sg.db_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
    if n < 1000:
        print(f"代码表仅{n}只，先刷新全市场代码表...")
        sg.refresh_all_codes(progress=lambda s: print("  " + s, flush=True))
    with sg.db_conn() as conn:
        codes = [r[0] for r in conn.execute(
            "SELECT code FROM stocks WHERE code NOT LIKE 'bj%' "
            "ORDER BY code").fetchall()]
    if args.limit:
        codes = codes[:args.limit]
    print(f"代码表 {len(codes)} 只，目标每只≥{MIN_BARS}根日K")

    # 2) 断点续传过滤
    have = {} if args.force else _local_progress()
    todo = [c for c in codes
            if not (have.get(c, (0, ""))[0] >= MIN_BARS
                    and have.get(c, (0, ""))[1] >= fresh)]
    print(f"本地已达标 {len(codes) - len(todo)} 只，待回填 {len(todo)} 只")
    if not todo:
        print("全部已缓存，无需回填")
        return

    # 3) 并发回填（501/429限流 → 全局暂停，时长递增：2分钟→5分钟）
    stat = {"ok": 0, "skip": 0, "fail": 0}
    t0 = time.time()
    failed = []
    pause_until = [0.0]
    pause_level = [0]

    def work(c):
        if time.time() < pause_until[0]:
            time.sleep(pause_until[0] - time.time())
        try:
            n = _store(c, _fetch_one(c))
            pause_level[0] = 0
            stat["ok"] += 1
            return ("ok", c, n)
        except Exception as e:
            msg = str(e)
            sg._BF_RAW_HINT.pop(c, None)   # v6.2.3：失败路径清理 raw 提示
            if "501" in msg or "429" in msg or "503" in msg:
                pause_level[0] = min(pause_level[0] + 1, 4)
                wait = 120 if pause_level[0] < 3 else 300
                pause_until[0] = max(pause_until[0], time.time() + wait)
                print(f"  [限流] 全局暂停{wait}s (第{pause_level[0]}次)",
                      flush=True)
            stat["fail"] += 1
            return ("fail", c, msg[:80])

    done_n = [0]
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for kind, c, info in ex.map(work, todo):
            done_n[0] += 1
            if kind == "fail":
                failed.append((c, info))
                print(f"  [失败] {c}: {info}", flush=True)
            if done_n[0] % 20 == 0 or done_n[0] == len(todo):
                el = time.time() - t0
                eta = el / done_n[0] * (len(todo) - done_n[0])
                print(f"回填 {done_n[0]}/{len(todo)} "
                      f"({done_n[0] * 100 // len(todo)}%) "
                      f"成功{stat['ok']} 失败{stat['fail']} "
                      f"耗时{el:.0f}s ETA {eta:.0f}s", flush=True)

    # 4) 失败重试一轮（换源概率）
    if failed:
        print(f"重试 {len(failed)} 只失败的代码...")
        retry = [c for c, _ in failed]
        stat["fail"] = 0
        for c in retry:
            try:
                _store(c, _fetch_one(c))
                stat["ok"] += 1
            except Exception as e:
                stat["fail"] += 1
                print(f"  [仍失败] {c}: {str(e)[:80]}", flush=True)

    # 5) 汇总
    have = _local_progress()
    total_bars = sum(v[0] for v in have.values())
    deep = sum(1 for v in have.values() if v[0] >= MIN_BARS)
    print("=" * 48)
    print(f"完成：本次成功{stat['ok']} 失败{stat['fail']}")
    print(f"库内：{len(have)}只代码 共{total_bars}根日K "
          f"(≥{MIN_BARS}根的 {deep} 只)")
    print(f"总耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
