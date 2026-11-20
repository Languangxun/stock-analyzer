#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_full.py - 全A股日K批量回填（目标≥5300根，可回溯 2005 年）

复用 stock_gui.py 内嵌缓存层（同一 stock_cache.db / 同一套多源机制）。

数据源（--src，逐只回退，失败才换下一源）：
  auto（默认）  通达信直连 → 腾讯/同花顺/东财 链 → 东财
  tdx           只走通达信（7709 新帧：700 根/请求，2003 年起全历史）
  tencent       只走腾讯→同花顺→东财 链（stock_gui._bf_fetch_one）
  eastmoney     只走东财（腾讯限流时的备用通道）

口径：库内统一存**乘法后复权**。
  各源一律取**不复权**日K：个股 × 新浪 hfq 因子；ETF / 指数等新浪因子
  恒等的标的改用**通达信除权事件自算因子**（分红送配 + 份额折算 suogu），
  因此 ETF 不再退回「腾讯前复权」、也就不会随新分红整段漂移
  （旧版 `--src eastmoney --bar-count 5300` 大量「口径冲突」的根因）。
  东财 fqt=2 为加法仿射口径，任何路径都不再入库。

写入策略：
  - 与库内重叠日期逐根比对：比值一致（离散度≤1%）→ 视为定基差异，
    先把整只存量按中位比值缩放，再写入新数据（序列自洽、收益不变）；
    比值离散 → 真正的口径冲突，跳过该只并记入失败清单；
  - 复权后收益越界（`_bars_anomalous`）也判为异常，拒绝写入，防止
    因子算错把跳变写进库。
- 短历史不再当失败：新股/次新 ETF 上市首根就是源侧最早一根时，
  ≥20 根即入库，并在状态文件里记 exhausted，下轮不再重拉。
- 断点续传：本地 ≥min_bars 根且最新日期够新 → 跳过；exhausted 且长期
  无新K（退市/长停）→ 跳过。
- 断点/失败清单：backfill_state.json（exhausted 标记）、
  backfill_failed.json（失败代码 + 原因，`--retry-failed` 可只重试它们）。
- 并发默认 6 线程；腾讯链路仍受全局节流（--throttle）保护，通达信为
  长连接、不受该节流影响（主站全灭自动冷却 120s 并切下一源）。

用法：
  python backfill_full.py                     # 全量回填（断点续传）
  python backfill_full.py --src tdx --limit 50
  python backfill_full.py --bar-count 5300 --min-bars 5300
  python backfill_full.py --force             # 忽略断点，全部重拉
  python backfill_full.py --retry-failed      # 只重试上轮失败的代码
"""
import argparse
import json
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
MIN_ROWS = 20            # 低于此根数视为源侧无有效数据（新股上市首日等）
SRC_MODE = "auto"

STATE_PATH = os.path.join(HERE, "backfill_state.json")
FAILED_PATH = os.path.join(HERE, "backfill_failed.json")


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


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as e:
        print(f"  [警告] 写 {os.path.basename(path)} 失败: {e}", flush=True)


class _NoData(RuntimeError):
    """所有源都确认「源侧根本没这只代码的数据」（退市/未上市/源侧下架），
    与限流、连不上的临时故障区分开：这类代码不该走完整条回退链反复空烧。"""


_NO_DATA_HINTS = ("无有效数据", "返回空", "仅0根", "无此代码")


def _as_nodata(e):
    """把各源「空数据」异常转成 _NoData（临时故障保持原样）。"""
    msg = str(e)
    if isinstance(e, _NoData) or any(h in msg for h in _NO_DATA_HINTS):
        return _NoData(msg[:80])
    return None


def _fetch_one(full):
    """单只：按 SRC_MODE 逐源回退。返回 (rows, exhausted, raw_last, src)。"""
    target = max(MIN_BARS, sg.CFG.MAX_FETCH_BARS, BAR_COUNT, 1600)
    errs = []
    nodata = 0

    def _try_tdx():
        raw, exhausted = sg._tdx_bars_raw(full, count=target)
        if not raw:
            raise _NoData("tdx无此代码（退市/未上市）")
        if len(raw) < MIN_ROWS:
            raise _NoData(f"仅{len(raw)}根")
        return sg._tdx_adjust(full, raw), exhausted, raw[-1]["close"], "tdx"

    def _try_tencent():
        rows, raw_last = sg._bf_fetch_one(full, page=800, target=target)
        if len(rows) < MIN_ROWS:
            raise _NoData(f"仅{len(rows)}根")
        return rows, False, raw_last, "tx-chain"

    def _try_eastmoney():
        raw = sg._fetch_eastmoney(full, count=target, fqt=0)
        if len(raw) < MIN_ROWS:
            raise _NoData(f"仅{len(raw)}根")
        rows = sg._tdx_adjust(full, raw)      # 个股新浪因子 / ETF 除权因子
        return rows, False, raw[-1]["close"], "eastmoney"

    chain = {"auto": [("tdx", _try_tdx), ("tx-chain", _try_tencent),
                      ("eastmoney", _try_eastmoney)],
             "tdx": [("tdx", _try_tdx)],
             "tencent": [("tx-chain", _try_tencent)],
             "eastmoney": [("eastmoney", _try_eastmoney)]}[SRC_MODE]
    for name, fn in chain:
        try:
            return fn()
        except Exception as e:
            nd = _as_nodata(e)
            nodata += 1 if nd else 0
            errs.append(f"{name}:{str(nd or e)[:70]}")
    msg = " | ".join(errs) or "无可用源"
    if nodata == len(chain):
        raise _NoData(msg)          # 全链都确认无数据 → 不当故障重试
    raise RuntimeError(msg)


def _segments_ok(rows, full, name, gap_days=10):
    """按停牌缺口切段后逐段做涨跌幅校验。

    停牌（如股改/重大事项）复牌首日不受涨跌停约束，跨停牌期的真实涨跌
    能轻松超过 limit+3pp——直接套 `_bars_anomalous` 会误杀（实测 600859
    2006-11-20→12-18 停牌 28 天复牌 +48.5%）。缺口>gap_days 处切开，
    只对段内连续交易日做越界判定。"""
    import datetime
    segs, cur, prev = [], [], None
    for r in rows:
        if prev is not None:
            try:
                gap = (datetime.date.fromisoformat(r["date"])
                       - datetime.date.fromisoformat(prev["date"])).days
            except ValueError:
                gap = 0
            if gap > gap_days:
                segs.append(cur)
                cur = []
        cur.append(r)
        prev = r
    segs.append(cur)
    return not any(sg._bars_anomalous(s, full, name)
                   for s in segs if len(s) >= 30)


def _ratio_segments(pairs, tol=0.004, min_len=3, max_segs=6):
    """把重叠比值 (date, 新/旧) 聚成定比分段 → [(起, 止(exclusive|None), 比值)]。

    比值整体恒定（换源定基差）→ 单段；比值在个别事件日跳档（重叠窗口内
    又出过一次分红/份额折算，存量数据是旧基期）→ 分段。段内离散 > tol
    （0.4%）或段过短/段数过多 → 返回 None，判为真口径冲突。"""
    segs, cur, vals = [], None, []
    for d, v in pairs:
        if cur is None:
            cur, vals = d, [v]
            continue
        med = sorted(vals)[len(vals) // 2]
        if abs(v / med - 1) <= tol:
            vals.append(v)
        else:
            segs.append((cur, d, vals))
            cur, vals = d, [v]
    segs.append((cur, None, vals))
    if len(segs) > max_segs:
        return None
    out = []
    for a, b, vals in segs:
        if len(vals) < min_len:
            return None
        med = sorted(vals)[len(vals) // 2]
        if max(abs(v / med - 1) for v in vals) > tol:
            return None
        out.append((a, b, med))
    return out


def _store(full, rows, raw_last=None):
    """写入库内（基期不同则先把存量按重叠比值分段对齐）。返回写入根数。

    与库内重叠日期逐根比对，比值 new/old 按稳定性分段：
      - 整体恒定 → 换源定基差，存量整只缩放到新基期；
      - 在个别事件日跳档 → 重叠窗口内又出过一次分红/份额折算（存量是旧
        基期），按段缩放到新基期；两种情形收益序列都不变。
      - 段内离散>0.4%、段太短或段数过多 → 真口径冲突，跳过并记入失败清单。
    """
    today = time.strftime("%Y-%m-%d")
    data = [r for r in rows if r["date"] < today and sg._bar_ok(r)]
    if not data:
        raise RuntimeError("过滤后无有效数据")
    data.sort(key=lambda r: r["date"])
    name = ""
    try:
        with sg.db_conn() as conn:
            r = conn.execute("SELECT name FROM stocks WHERE code=?",
                             (full,)).fetchone()
            name = (r[0] if r else "") or ""
    except Exception:
        pass
    if not _segments_ok(data, full, name):
        raise RuntimeError("复权后收益越界（疑似因子错误），已跳过")
    with sg.db_conn() as conn:
        old_rows = [{"date": d, "close": c} for d, c in conn.execute(
            "SELECT date,close FROM daily_bars WHERE code=? ORDER BY date",
            (full,))]
    if old_rows:
        old = {r["date"]: r["close"] for r in old_rows}
        pairs = [(r["date"], r["close"] / old[r["date"]]) for r in data
                 if old.get(r["date"]) and old[r["date"]]]
        pairs = pairs[-250:]
        if pairs:
            segs = _ratio_segments(pairs)
            if segs is None:
                raise RuntimeError(
                    f"口径冲突({len(pairs)}处重叠，比值无稳定分段)，已跳过")
            with sg.db_conn(commit=True) as conn:
                for lo, hi, r in segs:
                    if abs(r - 1.0) < 1e-4:
                        continue
                    if hi is None:
                        conn.execute(
                            "UPDATE daily_bars SET open=open*?, high=high*?, "
                            "low=low*?, close=close*? "
                            "WHERE code=? AND date>=?",
                            (r, r, r, r, full, lo))
                    else:
                        conn.execute(
                            "UPDATE daily_bars SET open=open*?, high=high*?, "
                            "low=low*?, close=close*? "
                            "WHERE code=? AND date>=? AND date<?",
                            (r, r, r, r, full, lo, hi))
    with sg.db_conn(commit=True) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO daily_bars"
            "(code,date,open,high,low,close,vol) VALUES(?,?,?,?,?,?,?)",
            [(full, r["date"], r["open"], r["high"], r["low"],
              r["close"], r["vol"]) for r in data])
    # 刷新显示缩放系数 k（raw/后复权同交易日配对）
    try:
        if raw_last and data[-1]["close"] > 0:
            sg._set_adjust(full, raw_last / data[-1]["close"])
        else:
            sg._sync_adjust(full, data)
    except Exception:
        pass
    return len(data)


def main():
    global MIN_BARS, FRESH_DAYS, BAR_COUNT, SRC_MODE
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前N只(测试)")
    ap.add_argument("--force", action="store_true", help="忽略断点全部重拉")
    ap.add_argument("--src", choices=["auto", "tdx", "tencent", "eastmoney"],
                    default="auto",
                    help="auto=通达信→腾讯链→东财（默认）；tdx/tencent/"
                         "eastmoney=只走该源")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--throttle", type=float, default=0.45,
                    help="HTTP源全局最小间隔秒(防501限流，不影响通达信)")
    ap.add_argument("--min-bars", type=int, default=MIN_BARS,
                    help=f"断点续传门槛（默认{MIN_BARS}根）")
    ap.add_argument("--fresh-days", type=int, default=FRESH_DAYS,
                    help=f"最新bar距今超过该自然日数视为过期（默认{FRESH_DAYS}）")
    ap.add_argument("--bar-count", type=int, default=BAR_COUNT,
                    help=f"单只目标根数下限（默认{BAR_COUNT}）")
    ap.add_argument("--retry-failed", action="store_true",
                    help="只重试 backfill_failed.json 里的代码")
    args = ap.parse_args()

    MIN_BARS = max(100, int(args.min_bars))
    FRESH_DAYS = max(1, int(args.fresh_days))
    BAR_COUNT = max(300, int(args.bar_count))
    SRC_MODE = args.src
    sg._MIN_INTERVAL = args.throttle     # 批量模式放缓全局节流（HTTP源）
    print(f"回填源: {SRC_MODE}  目标≥{MIN_BARS}根  单只≥{BAR_COUNT}根")

    fresh = _fresh_date()
    # 1) 代码表
    with sg.db_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
    if n < 1000:
        print(f"代码表仅{n}只，先刷新全市场代码表...")
        sg.refresh_all_codes(progress=lambda s: print("  " + s, flush=True))
    with sg.db_conn() as conn:
        # 只回填 cn（sh/sz）；hk/us/fu/crypto 是多市场并表代码，走 cn 源只会
        # 白打请求并触发限流
        codes = [r[0] for r in conn.execute(
            "SELECT code FROM stocks WHERE (code LIKE 'sh%' OR code LIKE 'sz%') "
            "ORDER BY code").fetchall()]
    if args.limit:
        codes = codes[:args.limit]
    print(f"代码表 {len(codes)} 只，目标每只≥{MIN_BARS}根日K")

    # 2) 断点续传过滤
    failed_old = _load_json(FAILED_PATH, [])
    state = {} if args.force else _load_json(STATE_PATH, {})
    have = {} if args.force else _local_progress()
    if args.retry_failed:
        todo = [c for c, *_ in failed_old if c in set(codes)]
        print(f"--retry-failed：只重试上轮失败的 {len(todo)} 只")
    else:
        def done(c):
            nb, last = have.get(c, (0, ""))
            if nb >= MIN_BARS and last >= fresh:
                return True
            e = state.get(c) or {}
            # 上次已取到上市首根（次新 ETF/退市/长停）：本地不比上次旧则跳过；
            # 有新 K 落地（last 前进）说明仍在交易，才再拉一次
            return bool(e.get("exhausted")) and last <= (e.get("last") or "")
        todo = [c for c in codes if not done(c)]
        print(f"本地已达标 {len(codes) - len(todo)} 只，待回填 {len(todo)} 只")
    if not todo:
        print("全部已缓存，无需回填")
        return

    # 3) 并发回填（HTTP源 501/429 → 全局暂停；连续失败也降温）
    stat = {"ok": 0, "fail": 0, "short": 0, "nodata": 0, "src": {}}
    t0 = time.time()
    failed = []
    pause_until = [0.0]
    pause_level = [0]
    consec_fail = [0]
    lock = __import__("threading").Lock()
    done_n = [0]

    def work(c):
        if time.time() < pause_until[0]:
            time.sleep(pause_until[0] - time.time())
        try:
            rows, exhausted, raw_last, src = _fetch_one(c)
            n = _store(c, rows, raw_last)
            with lock:
                pause_level[0] = 0
                consec_fail[0] = 0
                stat["ok"] += 1
                stat["src"][src] = stat["src"].get(src, 0) + 1
                if n < MIN_BARS:
                    stat["short"] += 1
                state[c] = {"bars": n, "last": rows[-1]["date"],
                            "exhausted": bool(exhausted),
                            "ts": time.strftime("%Y-%m-%d %H:%M")}
            return ("ok", c, f"{src} {n}根")
        except _NoData as e:
            msg = str(e)
            sg._BF_RAW_HINT.pop(c, None)
            nb, last = have.get(c, (0, ""))
            nodata_done = bool(last) and last < fresh
            with lock:
                consec_fail[0] = 0
                stat["nodata"] += 1
                if nodata_done:
                    # 本地早已停更（退市/长停）+ 源侧也没数据：标记完结，不再重拉
                    state[c] = {"bars": nb, "last": last, "exhausted": True,
                                "ts": time.strftime("%Y-%m-%d %H:%M")}
            return ("nodata", c, msg[:90])
        except Exception as e:
            msg = str(e)
            sg._BF_RAW_HINT.pop(c, None)
            with lock:
                consec_fail[0] += 1
                stat["fail"] += 1
                if "501" in msg or "429" in msg or "503" in msg:
                    pause_level[0] = min(pause_level[0] + 1, 4)
                    wait = 120 if pause_level[0] < 3 else 300
                    pause_until[0] = max(pause_until[0],
                                         time.time() + wait)
                    print(f"  [限流] 全局暂停{wait}s "
                          f"(第{pause_level[0]}次)", flush=True)
                elif consec_fail[0] >= 8:
                    wait = 180
                    pause_until[0] = max(pause_until[0],
                                         time.time() + wait)
                    consec_fail[0] = 0
                    print(f"  [连续失败] 全局暂停{wait}s 降温", flush=True)
            return ("fail", c, msg[:90])

    def report(kind, c, info):
        done_n[0] += 1
        if kind == "fail":
            failed.append((c, info))
            print(f"  [失败] {c}: {info}", flush=True)
        elif kind == "nodata":
            print(f"  [无数据] {c}: {info}", flush=True)
        if done_n[0] % 20 == 0 or done_n[0] == len(todo):
            el = time.time() - t0
            eta = el / done_n[0] * (len(todo) - done_n[0])
            print(f"回填 {done_n[0]}/{len(todo)} "
                  f"({done_n[0] * 100 // len(todo)}%) "
                  f"成功{stat['ok']} 失败{stat['fail']} "
                  f"无数据{stat['nodata']} "
                  f"耗时{el:.0f}s ETA {eta:.0f}s", flush=True)
            _save_json(STATE_PATH, state)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for kind, c, info in ex.map(work, todo):
            report(kind, c, info)

    # 4) 失败重试一轮（换源/冷却结束后的第二机会）
    if failed and not args.retry_failed:
        print(f"重试 {len(failed)} 只失败的代码...", flush=True)
        retry = [c for c, _ in failed]
        failed = []
        for c in retry:
            kind, c2, info = work(c)
            if kind == "fail":
                failed.append((c2, info))
                print(f"  [仍失败] {c2}: {info}", flush=True)

    # 5) 汇总
    _save_json(STATE_PATH, state)
    _save_json(FAILED_PATH, [[c, info, time.strftime("%Y-%m-%d %H:%M")]
                             for c, info in failed])
    have = _local_progress()
    total_bars = sum(v[0] for v in have.values())
    deep = sum(1 for v in have.values() if v[0] >= MIN_BARS)
    print("=" * 48)
    print(f"完成：本次成功{stat['ok']} 失败{stat['fail']} "
          f"无数据{stat['nodata']} "
          f"（其中不足{MIN_BARS}根的 {stat['short']} 只）")
    print("命中源: " + " ".join(f"{k}={v}"
                                for k, v in sorted(stat["src"].items())))
    print(f"库内：{len(have)}只代码 共{total_bars}根日K "
          f"(≥{MIN_BARS}根的 {deep} 只)")
    if failed:
        print(f"失败清单 {len(failed)} 只已写入 {os.path.basename(FAILED_PATH)}"
              f"（--retry-failed 只重试它们）")
    print(f"总耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
