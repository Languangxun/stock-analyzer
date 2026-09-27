#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data_clean.py - 股票日K缓存数据清洗/复权口径迁移（独立脚本，直接改库）

背景（2026-09 全库体检结论）：
  东财/腾讯的"前复权"使用除权公式，现金分红做减法。长期高分红股
  历史前复权价会趋近 0 甚至为负（如潞安环能 2020-02 收盘 0.088 元），
  导致全库 961 只股票出现 3791 处假跳变（|单日涨跌|>21%，个别 +241%/+300%）。
  收益率、形态匹配、标签全部被污染。

修复口径：
  库内统一存【后复权 hfq】（乘法、恒正、收益率正确）；
  另外维护 adjust(code->K) 表：K=最新不复权价/后复权末价，
  读取层用 hfq*K 还原为"乘法前复权"用于展示（收益率不变）。

用法：
  python data_clean.py                  # 只扫描报告（不改库）
  python data_clean.py --fix            # 扫描+修复异常代码（hfq 口径）
  python data_clean.py --all-adj        # 全库复权口径迁移（hfq+adjust，断点续传）
  python data_clean.py --all-adj --workers 3 --limit 500
  python data_clean.py --db x.db
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "stock_cache.db")
# 清洗报告统一写入 reports/（避免污染仓库根目录）
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "reports",
                           "清洗报告_" + time.strftime("%Y%m%d") + ".md")
ADJ_REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "reports",
                               "复权体检_" + time.strftime("%Y%m%d") + ".md")
EM_HOSTS = ("push2his.eastmoney.com", "92.push2his.eastmoney.com",
            "93.push2his.eastmoney.com", "97.push2his.eastmoney.com")
TX_HOSTS = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
            "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
            "http://ifzq.gtimg.cn/appstock/app/fqkline/get")
DEPTH = 2400        # 迁移目标根数（腾讯 800/页 ×3 页）；也是截断修复目标
MAX_PAGES = 6       # 单只最多翻页数（≈4800 根上限）
SHALLOW_BEFORE = "2021-01-01"   # --repair：首根早于此且根数<DEPTH 视为疑似截断

_rate_lock = threading.Lock()
_last_req = [0.0]
MIN_INTERVAL = 0.25


def _throttle():
    with _rate_lock:
        dt = time.time() - _last_req[0]
        if dt < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - dt)
        _last_req[0] = time.time()


def _get(url, decode="utf-8", retries=3, timeout=20, headers=None):
    last = None
    for i in range(retries):
        _throttle()
        try:
            req = urllib.request.Request(
                url, headers=headers or {
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://quote.eastmoney.com/"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode(decode, "ignore")
        except Exception as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


def _limit_pct(code, name, d):
    """该股当日涨跌幅限制(%)，None=不限（与 stock_gui._limit_pct 同规则）。"""
    if d < "1996-12-16":
        return None
    if code.startswith("bj"):
        return 30.0
    board = code[2:4] if len(code) >= 4 else ""
    if board == "68":
        return 20.0
    if board == "30":
        return 20.0 if d >= "2020-08-24" else 10.0
    if name and "ST" in name.upper() and d < "2026-07-06":
        return 5.0
    return 10.0


def _is_etf(code):
    pre = code[2:4] if len(code) >= 4 else ""
    return pre in ("51", "56", "58", "15", "16", "18")


def _secid(full):
    return ("1." if full.startswith("sh") else "0.") + full[2:]


def _parse_em(kd):
    out = []
    for line in (kd.get("data") or {}).get("klines") or []:
        p = line.split(",")
        if len(p) < 6:
            continue
        try:
            if float(p[2]) <= 0:
                continue
            out.append((p[0], float(p[1]), float(p[3]), float(p[4]),
                        float(p[2]), float(p[5])))
        except ValueError:
            continue
    return out


def _fetch_em_kline(full, fqt=2, count=8000):
    """东财日K。fqt=2 后复权（迁移主源），fqt=0 不复权，fqt=1 勿用。"""
    for host in EM_HOSTS:
        try:
            txt = _get(
                f"https://{host}/api/qt/stock/kline/get"
                f"?secid={_secid(full)}&fields1=f1,f2,f3"
                f"&fields2=f51,f52,f53,f54,f55,f56"
                f"&klt=101&fqt={fqt}&beg=0&end=20500101&lmt={count}")
            out = _parse_em(json.loads(txt))
            if out:
                return out
        except Exception:
            continue
    return []


_TX_RAW = {}


def _parse_qt(qt):
    """腾讯快照 qt → 最新原始价（不复权）。"""
    if not isinstance(qt, dict):
        return None
    for v in qt.values():
        if isinstance(v, (list, tuple)) and len(v) > 3:
            try:
                p = float(v[3])
                if p > 0:
                    return p
            except (TypeError, ValueError):
                continue
    return None


def _fetch_tx_kline(full, fq="hfq", pages=None, page=800, target=None):
    """腾讯日K（后复权，翻页补历史），fq='' 为不复权。

    target 给定则按需翻页直到根数≥target（最多 MAX_PAGES 页）或源侧无更早
    数据；不给定则固定翻 pages 页（默认 2）。旧实现固定 2 页（≤1600 根），
    对 2400 根长历史代码会截断——见 v6.1.5 热修⑪。"""
    if pages is None:
        pages = 2
        if target:
            pages = min(MAX_PAGES, max(1, (int(target) + page - 1)
                                       // page + 1))
    out, end, have = [], "", set()
    key = {"hfq": "hfqday", "qfq": "qfqday"}.get(fq, "day")
    for _ in range(pages):
        if target and len(out) >= target:
            break
        param = (f"?param={full},day,,{end},{page},{fq}" if (end and fq)
                 else f"?param={full},day,,{end},{page}," if end
                 else f"?param={full},day,,,{page},{fq}" if fq
                 else f"?param={full},day,,,{page},")
        got = None
        for host in TX_HOSTS:
            try:
                txt = _get(host + param, retries=1, timeout=10)
                d = (json.loads(txt).get("data") or {}).get(full) or {}
                p = _parse_qt(d.get("qt"))
                if p:
                    _TX_RAW[full] = p
                bars = d.get(key) or d.get("day") or []
                if bars:
                    got = bars
                    break
            except Exception:
                continue
        if not got:
            break
        rows = []
        for b in got:
            try:
                if float(b[2]) <= 0:
                    continue
                rows.append((b[0], float(b[1]), float(b[3]), float(b[4]),
                             float(b[2]), float(b[5])))
            except (ValueError, IndexError):
                continue
        add = [r for r in rows if r[0] not in have]
        if not add:
            break
        out = add + out
        have.update(r[0] for r in add)
        if len(got) < page - 10:
            break
        try:
            d0 = datetime.strptime(out[0][0], "%Y-%m-%d").date()
            end = (d0 - __import__("datetime").timedelta(days=1)).isoformat()
        except Exception:
            break
    return out


_SINA_FACTOR_CACHE = {}
_K_HINT = {}          # _hfq_mul 顺带算出的显示缩放 K=raw/hfq（免一次网络配对）


def _fetch_sina_factors(full):
    """新浪后复权因子 [(date,factor)] 升序；无事件返回 []；失败抛异常。

    `finance.sina.com.cn/realstock/company/<code>/hfq.js` 给出每次除权的
    累计复权因子，f(t)=最近一个 d<=t 的因子。用它×不复权日K 得到**真正的
    乘法后复权**（段内日收益=不复权收益、除权日按因子跳变），可消除腾讯
    hfq 的分段仿射失真（见 check_adj / reports/数据异常报告_20260927.md）。"""
    if full in _SINA_FACTOR_CACHE:
        return _SINA_FACTOR_CACHE[full]
    txt = _get("https://finance.sina.com.cn/realstock/company/"
               f"{full}/hfq.js",
               headers={"User-Agent": "Mozilla/5.0",
                        "Referer": "https://finance.sina.com.cn/"})
    m = re.search(r"=\s*(\{.*\})", txt, re.S)
    if not m:
        raise ValueError("新浪复权因子解析失败")
    obj = json.loads(m.group(1))
    ev = sorted((it["d"], float(it["f"]))
                for it in (obj.get("data") or []) if it.get("d"))
    _SINA_FACTOR_CACHE[full] = ev
    return ev


def _hfq_mul(full, target=DEPTH):
    """不复权日K × 新浪后复权因子 → 乘法后复权 rows（失败返回 []）。

    同时把显示缩放 K=raw/hfq 记入 `_K_HINT`，供 migrate_one 免一次网络配对。"""
    raw = _fetch_tx_kline(full, fq="", target=target)
    if not raw:
        return []
    # 基金/指数无需新浪因子（实测基金因子恒为1）：ETF/LOF 直接用腾讯 qfq
    # （日收益与不复权一致、除权日含分红，孤立折算跳变由 ETF 校验豁免），
    # 指数用不复权。省一次因子请求。
    if _is_etf(full):
        q = _fetch_tx_kline(full, fq="qfq", target=target)
        rr = q or raw
        if rr and rr[-1][4]:
            _K_HINT[full] = raw[-1][4] / rr[-1][4]
        return rr
    if _is_index(full):
        _K_HINT[full] = 1.0
        return raw
    events = _fetch_sina_factors(full)
    real = [e for e in events if abs(e[1] - 1.0) > 1e-9]
    if not real:            # 股票无除权事件 → 不复权即后复权
        _K_HINT[full] = 1.0
        return raw
    import bisect
    ds = [e[0] for e in events]
    fs = [e[1] for e in events]
    out = []
    for r in raw:
        i = bisect.bisect_right(ds, r[0]) - 1
        f = fs[i] if i >= 0 else fs[0]
        out.append((r[0], r[1] * f, r[2] * f, r[3] * f, r[4] * f, r[5]))
    i = bisect.bisect_right(ds, raw[-1][0]) - 1
    f_last = fs[i] if i >= 0 else fs[0]
    if f_last:
        _K_HINT[full] = 1.0 / f_last
    return out


def fetch_raw_last(full):
    """最新不复权价：腾讯快照(qt) → 东财 fqt=0 → 腾讯不复权K线。"""
    if _TX_RAW.get(full):
        return _TX_RAW[full]
    try:
        rows = _fetch_em_kline(full, fqt=0, count=5)
        if rows:
            return rows[-1][4]
    except Exception:
        pass
    try:
        rows = _fetch_tx_kline(full, fq="", pages=1, page=5)
        if rows:
            return rows[-1][4]
    except Exception:
        pass
    try:                    # hfq 请求响应里也带 qt 快照
        _fetch_tx_kline(full, fq="hfq", pages=1, page=5)
    except Exception:
        pass
    return _TX_RAW.get(full)


def _bar_valid(b):
    o, h, l, cl = b[1], b[2], b[3], b[4]
    if None in (o, h, l, cl) or min(o, h, l, cl) <= 0:
        return False
    if h < l or h < max(o, cl) or l > min(o, cl):
        return False
    return True


def _is_index(code):
    """指数不受涨跌停约束（sh000xxx / sz399xxx）。"""
    return code.startswith(("sh000", "sz399"))


def _anomaly_flags(rows, code, name=""):
    """逐 bar 涨跌幅越界标记（与 stock_gui._bars_anomalous 同规则）。

    rows: (date,open,high,low,close,vol) 升序；flags[i] 对应 rows[i+1]。
    豁免：不足30根、指数、名称含"退"、序列前10根（注册制新股前5日不设限）、
    相邻日历间隔>30天（长期停牌复牌/退市整理首日）。主板 ST 旧规5%按现名
    回溯会误伤非ST历史段 → 放宽到10%；ETF/LOF 除权折算容差再放宽12pp
    （孤立跳变放行，连续才算异常），均与 GUI 清洗/回填判定一致。"""
    if len(rows) < 30:
        return []
    if _is_index(code) or "退" in (name or ""):
        return []
    is_etf = _is_etf(code)
    extra = 12.0 if is_etf else 0.0
    dates = [date.fromisoformat(r[0]) for r in rows]
    flags = []
    for idx, (prev, cur) in enumerate(zip(rows, rows[1:])):
        pc, cl = prev[4], cur[4]
        if not pc or pc <= 0 or not cl or cl <= 0:
            flags.append(False)
            continue
        if idx < 10:
            flags.append(False)
            continue
        if (dates[idx + 1] - dates[idx]).days > 30:
            flags.append(False)
            continue
        lim = _limit_pct(code, name, cur[0])
        if lim is None:
            flags.append(False)
            continue
        if lim < 10.0:
            lim = 10.0
        flags.append(abs(cl / pc - 1) * 100 > lim + 3.0 + extra)
    return flags


def _anomalous(rows, code, name=""):
    """返回 (越界根数, 是否判异常)。ETF 需连续跳变（孤立折算放行）。"""
    flags = _anomaly_flags(rows, code, name)
    if not flags:
        return 0, False
    if _is_etf(code):
        consec = any(a and b for a, b in zip(flags, flags[1:]))
        return sum(flags), consec
    n = sum(flags)
    return n, n > 0


def _validate(rows, code, name):
    """hfq 序列健康检查：≥min_bars、结构合法、无涨跌停越界（豁免同扫描）。

    校验不过的代码保留库内原数据，绝不写入半截/污染序列。"""
    if len(rows) < 100:
        return False, len(rows), "历史不足100根"
    for b in rows:
        if not _bar_valid(b):
            return False, 0, f"结构异常({b[0]})"
    viol, bad = _anomalous(rows, code, name)
    if bad:
        return False, viol, f"涨跌幅越界{viol}处"
    return True, viol, ""


def ensure_tables(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS adjust("
                 "code TEXT PRIMARY KEY, k REAL, ts REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS adj_done("
                 "code TEXT PRIMARY KEY, ts REAL, bars INTEGER, last_date TEXT)")


def set_adjust(conn, code, k):
    if k and k > 0:
        conn.execute("INSERT OR REPLACE INTO adjust VALUES(?,?,?)",
                     (code, float(k), time.time()))


def migrate_one(conn, full, name, log=None, force=False, min_bars=100,
                depth=DEPTH, allow_shrink=False):
    """把单只代码迁移为后复权(hfq)存储 + 写入显示缩放 K。返回状态串。

    迁移深度 = max(min_bars, 库内现有根数, depth)，按需翻页（v6.1.5 热修⑪：
    旧实现固定 2 页 ≤1600 根，把 2400 根长历史截断）。绝不缩水：新序列首根
    晚于旧首根（且不更长）或末根早于旧末根时保留库内原数据。"""
    row = conn.execute("SELECT ts FROM adj_done WHERE code=?",
                       (full,)).fetchone()
    if row and not force:
        return "skip"
    old_n, old_first, old_last = conn.execute(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM daily_bars WHERE code=?",
        (full,)).fetchone()
    target = max(min_bars, depth or 0, old_n or 0)
    # 候选源按口径正确性顺序**短路**求值（避免每只都去试已宕机的东财）：
    # ①新浪因子×不复权（真乘法后复权；ETF 内部退 qfq、指数用不复权）
    # ②腾讯 hfq（分段仿射，仅新浪失败/校验不过时兜底）③东财 fqt=2
    rows, src = [], "none"
    for cs, fetch in (
            ("sina", lambda: _hfq_mul(full, target=target)),
            ("tx", lambda: _fetch_tx_kline(full, fq="hfq", target=target)),
            ("em", lambda: _fetch_em_kline(full, fqt=2, count=8000))):
        try:
            rr = fetch()
        except Exception:
            rr = []
        if not rr or len(rr) < min_bars:
            continue
        ok, _v, _why = _validate(rr, full, name)
        if not ok:
            continue
        rows, src = rr, cs
        break
    if not rows:
        return "nodata"
    today = time.strftime("%Y-%m-%d")
    after_close = (date.today().weekday() < 5
                   and time.strftime("%H:%M") >= "15:05")
    bars = [r for r in rows
            if r[0] < today or (after_close and r[0] == today)]
    if not bars:
        return "nodata"
    if old_n and not allow_shrink:
        if old_first and bars[0][0] > old_first and len(bars) <= old_n:
            return f"keep_head({bars[0][0]}>{old_first})"
        if old_last and bars[-1][0] < old_last:
            return f"keep_tail({bars[-1][0]}<{old_last})"
    # 显示缩放 K 必须用「同一交易日」的 raw/hfq 收盘配对（2026-09-16 修复）。
    # 新浪乘法口径下 K=1/f_last 或 raw_last/qfq_last 已由 _hfq_mul 顺带算出。
    k = None
    if src == "sina":
        k = _K_HINT.pop(full, None)
    raw_map = {}
    if not k:
        for _fetch_raw in (lambda: _fetch_tx_kline(full, fq="", pages=1,
                                                   page=20),
                           lambda: _fetch_em_kline(full, fqt=0, count=20)):
            try:
                for r in _fetch_raw():
                    if r[4] > 0:
                        raw_map[r[0]] = r[4]
            except Exception:
                pass
            if raw_map:
                break
        for r in reversed(bars[-8:]):
            rc = raw_map.get(r[0])
            if rc and r[4] and r[4] > 0:
                k = rc / r[4]
                break
    conn.execute("DELETE FROM daily_bars WHERE code=?", (full,))
    conn.executemany(
        "INSERT OR REPLACE INTO daily_bars"
        "(code,date,open,high,low,close,vol) VALUES(?,?,?,?,?,?,?)",
        [(full, r[0], r[1], r[2], r[3], r[4], r[5]) for r in bars])
    if k:
        set_adjust(conn, full, k)
    conn.execute("INSERT OR REPLACE INTO adj_done VALUES(?,?,?,?)",
                 (full, time.time(), len(bars), bars[-1][0]))
    conn.commit()
    if log:
        ks = f"K={k:.4f}" if k else "K=?"
        log(f"  [{src}] {full} {len(bars)}根 "
            f"{bars[0][0]}~{bars[-1][0]} {ks}")
    return "ok"


def migrate_all(db, workers=2, limit=None, force=False, log=print,
                min_bars=100, depth=DEPTH, allow_shrink=False,
                shallow_before=None):
    """全库迁移（可由多进程/多机分片：--shard i/n）。

    shallow_before 给定时只迁移「根数<depth 且首根早于该日期」的代码，
    用于修复被旧版浅迁移截断的长历史（--repair）。"""
    conn = sqlite3.connect(db, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_tables(conn)
    names = {r[0]: (r[1] or "") for r in
             conn.execute("SELECT code, name FROM stocks").fetchall()}
    sql = ("SELECT code FROM daily_bars GROUP BY code HAVING COUNT(*)>=? ")
    params = [min_bars]
    if shallow_before:
        sql += "AND COUNT(*)<? AND MIN(date)<? "
        params += [depth, shallow_before]
    sql += "ORDER BY code"
    codes = [r[0] for r in conn.execute(sql, params).fetchall()
             if not r[0].startswith("bj")]
    if limit:
        codes = codes[:limit]
    todo = [c for c in codes
            if force or not conn.execute(
                "SELECT 1 FROM adj_done WHERE code=?", (c,)).fetchone()]
    log(f"待迁移 {len(todo)}/{len(codes)} 只（workers={workers}, "
        f"depth={depth}）")
    lock = threading.Lock()
    done = [0]

    def one(c):
        lconn = sqlite3.connect(db, timeout=60)
        lconn.execute("PRAGMA journal_mode=WAL")
        try:
            st = migrate_one(lconn, c, names.get(c, ""), force=force,
                             min_bars=min_bars, depth=depth,
                             allow_shrink=allow_shrink)
        except Exception as e:
            st = f"err({str(e)[:60]})"
        finally:
            lconn.close()
        with lock:
            done[0] += 1
            if done[0] % 50 == 0 or done[0] == len(todo):
                log(f"  迁移 {done[0]}/{len(todo)} ({done[0]*100//max(1,len(todo))}%)")
        if st.startswith(("err", "reject", "keep_")) :
            with lock:
                bad.append((c, st))

    bad = []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, todo))
    log(f"迁移完成：成功{len(todo)-len(bad)} 异常{len(bad)}")
    for c, st in bad[:40]:
        log(f"  ! {c} {st}")
    conn.close()
    return bad


def scan(conn):
    """全库扫描，返回 {code: {...问题列表}} 与全局统计。"""
    names = {r[0]: (r[1] or "") for r in
             conn.execute("SELECT code, name FROM stocks").fetchall()}
    rows = conn.execute(
        "SELECT code,date,open,high,low,close,vol FROM daily_bars "
        "ORDER BY code,date").fetchall()
    by = {}
    for c, d, o, h, l, cl, v in rows:
        by.setdefault(c, []).append((d, o, h, l, cl, v or 0.0))

    today = date.today()
    recent_cut = (today - timedelta(days=365)).isoformat()
    issues = {}
    stats = {"codes": len(by), "bars": len(rows), "bad_bars": 0,
             "refetch": 0, "refetch_bars": 0, "refetch_bj": 0,
             "suspicious": 0, "delisted": 0, "zero_vol": 0, "stale": 0,
             "low_price": 0, "neg_price": 0, "stale_vs_market": 0,
             "stale_bj": 0, "orphan": 0}
    market_last = max((b[-1][0] for b in by.values()), default="")

    def add(c, kind, detail):
        issues.setdefault(c, []).append((kind, detail))

    for c, bars in by.items():
        name = names.get(c, "")
        n = len(bars)
        # ---- 1) 结构异常 ----
        bad = [b for b in bars if not _bar_valid(b)]
        if bad:
            add(c, "bad_bars", f"{len(bad)}根结构异常(如 {bad[0][0]})")
            stats["bad_bars"] += len(bad)
            stats["neg_price"] += sum(
                1 for b in bad if min(b[1], b[2], b[3], b[4]) < 0)
        # ---- 2) 涨跌幅越界（除权公式前复权残留/坏数据）----
        viol, anomalous = _anomalous(bars, c, name)
        stats["refetch_bars"] += viol
        if c.startswith("bj"):
            if anomalous:
                stats["refetch_bj"] += 1   # 北交所源停更，项目不覆盖
        elif anomalous and not _is_etf(c):
            add(c, "refetch", f"{viol}处涨跌幅越界")
            stats["refetch"] += 1
        elif anomalous:
            add(c, "refetch", "ETF连续越界跳变")
            stats["refetch"] += 1
        # ---- 3) 末根/退市/相对市场陈旧 ----
        d1 = date.fromisoformat(bars[-1][0])
        age = (today - d1).days
        if age > 180:
            add(c, "delisted", f"最后bar {bars[-1][0]} (距今{age}天)")
            stats["delisted"] += 1
        elif market_last and bars[-1][0] < market_last:
            dl = (date.fromisoformat(market_last) - d1).days
            if dl > 10:
                if c.startswith("bj"):
                    stats["stale_bj"] += 1    # 北交所源已停更（项目不覆盖）
                else:
                    add(c, "stale_vs_market",
                        f"最后bar {bars[-1][0]}，落后市场 {dl} 天")
                    stats["stale_vs_market"] += 1
        # ---- 2b) 价格失真（前复权做减法→历史近零；末价规则排除
        #      ETF/指数/退市，低价基金与退市整理股是真实价格）----
        if (bars[-1][4] is not None and bars[-1][4] < 0.5
                and not _is_etf(c) and not _is_index(c)
                and age <= 180 and "退" not in name):
            add(c, "low_price", f"末价 {bars[-1][4]:.3f} 偏低")
            stats["low_price"] += 1
        # ---- 4) 停牌缺口（只报近一年；陈年长停牌属正常历史，不刷屏）----
        gaps = []
        for a, b in zip(bars, bars[1:]):
            if b[0] >= recent_cut and \
                    (date.fromisoformat(b[0])
                     - date.fromisoformat(a[0])).days > 20:
                gaps.append(f"{a[0]}~{b[0]}")
        if gaps:
            add(c, "suspend", f"长缺口{len(gaps)}处: {gaps[:3]}")
            stats["suspicious"] += 1
        zv = sum(1 for b in bars if b[5] <= 0)
        if zv:
            stats["zero_vol"] += zv
        # ---- 5) 价格粘性（连续≥20日收盘不变）----
        run = 1
        for (a, b) in zip(bars, bars[1:]):
            run = run + 1 if a[4] == b[4] and a[4] else 1
            if run >= 20:
                add(c, "stale", f"连续{run}日收盘不变(至 {b[0]})")
                stats["stale"] += 1
                break
    # ---- 6) 孤立代码（daily_bars 有、stocks 无；指数/北交所除外）----
    for c, bars in by.items():
        if c in names or _is_index(c) or c.startswith("bj"):
            continue
        add(c, "orphan", f"{len(bars)}根不在代码表(疑似错误前缀/已退市)")
        stats["orphan"] += 1
    return issues, stats, names


def _affine_fit(xs, ys):
    """一元线性拟合 y≈a·x+b，返回 (a,b,R²)；退化时返回 None。"""
    n = len(xs)
    if n < 3:
        return None
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    den = n * sxx - sx * sx
    if abs(den) < 1e-12:
        return None
    a = (n * sxy - sx * sy) / den
    b = (sy - a * sx) / n
    sst = sum((y - sy / n) ** 2 for y in ys)
    sse = sum((y - (a * x + b)) ** 2 for x, y in zip(xs, ys))
    return a, b, (1 - sse / sst if sst > 0 else 1.0)


def check_adj(db, sample=200, workers=3, log=print):
    """复权失真体检（只读，不改库）：库内序列日收益率是否等于不复权收益率。

    背景（2026-09-27 实测）：腾讯 hfq 并非"乘法后复权"，而是分段
    `hfq ≈ a×不复权价 + b` 的仿射变换——段内 a,b 恒定，导致**整段日收益
    被缩放 s=a·P/(a·P+b)**（实测浦发 0.63、茅台 0.82、申华 1.75），
    与 data_clean 修复口径宣称的"收益率正确"不符。本体检抽样对比
    「库内收盘 vs 腾讯不复权日K近800根」，用末段 120 根拟合 affine 并取
    收益比中位数 s（个股/指数对比腾讯不复权、ETF/LOF 对比腾讯 qfq；有效
    收益样本<20 的低波动标的不判）；|s-1|>2% 记为失真。报告 reports/复权体检_*.md。"""
    import random
    from statistics import median
    import statistics as _st
    conn = sqlite3.connect(db, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    codes = [r[0] for r in conn.execute(
        "SELECT code FROM daily_bars WHERE code NOT LIKE 'bj%' "
        "GROUP BY code HAVING COUNT(*)>=120").fetchall()]
    if sample and sample > 0 and len(codes) > sample:
        random.seed(20260927)
        codes = sorted(random.sample(codes, sample))
    log(f"复权失真体检：抽样 {len(codes)} 只（库内收盘 vs 腾讯不复权近800根）")
    stored = {}
    for c in codes:
        stored[c] = dict(conn.execute(
            "SELECT date, close FROM daily_bars WHERE code=?", (c,)).fetchall())
    conn.close()
    lock = threading.Lock()
    rows, skipped, lowvol = [], [0], [0]

    def one(c):
        # 库内基准：个股/指数对比不复权收益；ETF/LOF 落库口径是腾讯 qfq
        ref_fq = "qfq" if _is_etf(c) else ""
        try:
            ref = _fetch_tx_kline(c, fq=ref_fq, pages=1)
        except Exception:
            ref = []
        m = {r[0]: r[4] for r in ref if r[4] and r[4] > 0}
        ds = [d for d in sorted(m) if d in stored.get(c, {})][-120:]
        if len(ds) < 120:
            with lock:
                skipped[0] += 1
            return
        xs = [m[d] for d in ds]                 # 基准（不复权/qfq）
        ys = [stored[c][d] for d in ds]         # 库内(后复权)
        fit = _affine_fit(xs, ys)
        rr = [xs[i + 1] / xs[i] - 1 for i in range(len(xs) - 1)]
        hh = [ys[i + 1] / ys[i] - 1 for i in range(len(ys) - 1)]
        ratios = [h / r for h, r in zip(hh, rr) if abs(r) > 0.002]
        if len(ratios) < 20:                    # 低波动标的（国债ETF等）不判
            with lock:
                lowvol[0] += 1
            return
        s = median(ratios)
        with lock:
            rows.append({"code": c, "s": s, "r2": fit[2] if fit else 0.0,
                         "n": len(ds)})

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, codes))
    bad = sorted([r for r in rows if abs(r["s"] - 1) > 0.02],
                 key=lambda r: -abs(r["s"] - 1))
    ss = [r["s"] for r in rows]
    log(f"完成：有效 {len(rows)} 只，跳过 {skipped[0]} 只，"
        f"低波动不判 {lowvol[0]} 只，检出失真 {len(bad)} 只（|s-1|>2%）")
    if ss:
        log(f"- 收益缩放 s 中位 {median(ss):.3f}")
        if len(ss) >= 10:
            log(f"- 分布 P10 {_st.quantiles(ss, n=10)[0]:.3f} / "
                f"P90 {_st.quantiles(ss, n=10)[-1]:.3f}")
        log(f"- 放大(s>1.02) {sum(1 for s in ss if s > 1.02)} 只 / "
            f"缩小(s<0.98) {sum(1 for s in ss if s < 0.98)} 只")
        log("")
        log("| 代码 | 收益缩放s | 拟合R² |")
        log("|---|---|---|")
        for r in bad[:40]:
            log(f"| {r['code']} | {r['s']:.3f} | {r['r2']:.4f} |")
    return {"rows": rows, "bad": bad, "skipped": skipped[0]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="执行修复(默认只报告)")
    ap.add_argument("--all-adj", action="store_true",
                    help="全库复权口径迁移(存hfq+adjust, 断点续传)")
    ap.add_argument("--repair", action="store_true",
                    help="修复旧版浅迁移截断：根数<depth 且首根早于2021 的代码")
    ap.add_argument("--check-adj", action="store_true",
                    help="复权失真体检：抽样对比库内序列与不复权日K的收益率")
    ap.add_argument("--sample", type=int, default=200,
                    help="--check-adj 抽样只数（0=全部）")
    ap.add_argument("--depth", type=int, default=DEPTH,
                    help=f"迁移目标根数（默认{DEPTH}；腾讯800/页按需翻页）")
    ap.add_argument("--allow-shrink", action="store_true",
                    help="允许迁移结果短于库内现有历史（默认禁止缩水）")
    ap.add_argument("--workers", type=int, default=2,
                    help="--all-adj 并发数（免费源限流，建议2~3）")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()
    if args.repair:
        args.all_adj = True

    mode = ("截断修复" if args.repair else
            "复权体检" if args.check_adj else
            "迁移" if args.all_adj else "修复" if args.fix else "只扫描")
    lines = [f"# 数据清洗报告 {time.strftime('%Y-%m-%d %H:%M')}",
             f"库: `{args.db}`  模式: **{mode}**", ""]

    def log(s):
        print(s, flush=True)
        lines.append(s)

    if args.check_adj:
        check_adj(args.db, sample=args.sample, workers=args.workers, log=log)
        os.makedirs(os.path.dirname(ADJ_REPORT_PATH), exist_ok=True)
        with open(ADJ_REPORT_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"\n报告已保存: {ADJ_REPORT_PATH}")
        return

    if args.all_adj:
        if args.repair:
            log(f"截断修复筛选：根数<{args.depth} 且首根早于 "
                f"{SHALLOW_BEFORE}（排除北交所）")
        bad = migrate_all(args.db, workers=args.workers, limit=args.limit,
                          force=args.force or args.repair, log=log,
                          depth=args.depth,
                          allow_shrink=args.allow_shrink,
                          shallow_before=(SHALLOW_BEFORE
                                          if args.repair else None))
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"\n报告已保存: {REPORT_PATH}")
        return

    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    t0 = time.time()
    issues, stats, names = scan(conn)
    log(f"扫描完成({time.time() - t0:.0f}s)：{stats['codes']}只代码 "
        f"{stats['bars']}根日K")
    log(f"- 结构异常bar: {stats['bad_bars']}")
    log(f"- 涨跌幅越界(疑似前复权失真,需迁移): {stats['refetch']}只 / "
        f"{stats['refetch_bars']}处（北交所源停更忽略 "
        f"{stats['refetch_bj']}只）")
    log(f"- 末价<0.5元(非ETF/退市): {stats['low_price']}")
    log(f"- 负价bar: {stats['neg_price']}")
    log(f"- 长停牌缺口(近一年): {stats['suspicious']}")
    log(f"- 零成交bar: {stats['zero_vol']}")
    log(f"- 疑似退市(>180天): {stats['delisted']}")
    log(f"- 落后市场最新日>10天: {stats['stale_vs_market']}"
        f"（北交所源停更忽略 {stats['stale_bj']}只）")
    log(f"- 孤立代码(不在代码表): {stats['orphan']}")
    log(f"- 价格粘性: {stats['stale']}")
    log("")
    if issues:
        log("## 问题明细（按代码）")
        log("")
        log("| 代码 | 名称 | 问题 |")
        log("|---|---|---|")
        for c in sorted(issues):
            nm = names.get(c, "")
            for kind, detail in issues[c]:
                log(f"| {c} | {nm} | {kind}: {detail} |")
    else:
        log("未发现问题数据。")
    if args.fix:
        log("")
        log("## 修复动作（整只按 hfq 迁移）")
        conn.execute("CREATE TABLE IF NOT EXISTS delisted("
                     "code TEXT PRIMARY KEY, last_date TEXT, ts REAL)")
        ensure_tables(conn)
        fixed = kept = 0
        for c, items in issues.items():
            kinds = {k for k, _ in items}
            if kinds & {"bad_bars", "refetch", "low_price", "stale",
                        "stale_vs_market"} and not c.startswith("bj"):
                # force=True（v6.1.5 热修⑩）：被 scan 标记的代码按定义就是
                # 有问题的，migrate_one 默认会因 adj_done 已存在而 skip →
                # 修复链失效。热修⑪：深度按 depth 翻页且禁止缩水，修复
                # 不再把长历史截断成短历史。
                st = migrate_one(conn, c, names.get(c, ""), log=log,
                                 force=True, depth=args.depth,
                                 allow_shrink=args.allow_shrink)
                if st == "ok":
                    fixed += 1
                elif st.startswith("keep_"):
                    kept += 1
            dl = next((d.split(" ")[0] for k, d in items
                       if k == "delisted"), None)
            if dl:
                conn.execute("INSERT OR REPLACE INTO delisted VALUES(?,?,?)",
                             (c, dl, time.time()))
        conn.commit()
        log(f"修复完成：成功 {fixed} 只，保留原数据(防缩水) {kept} 只")
    conn.close()
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n报告已保存: {REPORT_PATH}")


if __name__ == "__main__":
    main()
