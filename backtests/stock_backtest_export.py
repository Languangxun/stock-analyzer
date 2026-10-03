#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stock_backtest_export.py - 每只股回测数据导出（Excel / CSV，纯标准库）

对库内每只股票按统一口径重算策略信号并跑事件回测（T+1 成交、ATR 止损、
移动止盈，与 GUI「工具→信号胜率」同引擎），逐只汇总为一张表：
  代码/名称/区间/策略/交易数/胜率/总收益/年化/最大回撤/盈亏比/训练段/验证段/
  IC(T+1,T+5)/信号数 等。
回测主体是纯 Python 循环（GIL 串行），故用**多进程**（ProcessPoolExecutor）
而非线程实现 --workers：每只股票独立、只读库，进程间无共享状态。
无 openpyxl 依赖：内置最小 xlsx 写入器（zipfile+XML），另存一份 CSV 便于查看；
跑完自动刷新 `research/dashboard.html`（「每只股回测」页，失败不影响产物）。

用法：
  python backtests/stock_backtest_export.py --out reports/每只股回测.xlsx
  python backtests/stock_backtest_export.py --limit 50 --workers 4
  python backtests/stock_backtest_export.py --bars 1000 --pool deep --mode 激进
  python backtests/stock_backtest_export.py --mode cached   # 用各股5日策略缓存
"""
import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import stock_gui as sg  # noqa: E402
import bt_common          # noqa: E402  回测统一规范

RESEARCH = os.path.join(ROOT, "research")


# ---------------- 最小 xlsx 写入（无第三方依赖） ----------------

def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _col(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def write_xlsx(path, sheets):
    """sheets: [(sheet_name, rows)]，rows 为 str/float/int/None 的列表。"""
    import zipfile
    sheet_xml, rels, overrides = [], [], []
    for n, (name, rows) in enumerate(sheets, 1):
        cells = []
        for ri, row in enumerate(rows, 1):
            cs = []
            for ci, v in enumerate(row):
                if v is None or v == "":
                    continue
                ref = f"{_col(ci)}{ri}"
                style = ' s="1"' if ri == 1 else ""
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    cs.append(f'<c r="{ref}"{style}><v>{v}</v></c>')
                else:
                    cs.append(f'<c r="{ref}" t="inlineStr"{style}>'
                              f'<is><t xml:space="preserve">{_esc(v)}</t></is></c>')
            cells.append(f'<row r="{ri}">{"".join(cs)}</row>')
        sheet_xml.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><sheetData>'
            f'{"".join(cells)}</sheetData></worksheet>')
        rels.append(f'<Relationship Id="rId{n}" Type="http://schemas.'
                    'openxmlformats.org/officeDocument/2006/relationships/'
                    f'worksheet" Target="worksheets/sheet{n}.xml"/>')
        overrides.append(f'<Override PartName="/xl/worksheets/sheet{n}.xml" '
                         'ContentType="application/vnd.openxmlformats-'
                         'officedocument.spreadsheetml.worksheet+xml"/>')
    rels.append(f'<Relationship Id="rId{len(sheets) + 1}" Type="http://'
                'schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/styles" Target="styles.xml"/>')
    overrides.append('<Override PartName="/xl/styles.xml" ContentType='
                     '"application/vnd.openxmlformats-officedocument.'
                     'spreadsheetml.styles+xml"/>')
    sheet_tags = "".join(
        f'<sheet name="{_esc(n)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, (n, _) in enumerate(sheets, 1))

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types"><Default Extension="rels" ContentType='
        '"application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(overrides) + '</Types>')
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
        '2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
        'openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
        ' Target="xl/workbook.xml"/></Relationships>')
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
        '2006/main" xmlns:r="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships"><sheets>' + sheet_tags +
        '</sheets></workbook>')
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
        '2006/relationships">' + "".join(rels) + '</Relationships>')
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
        '2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/>'
        '</font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border/></borders><cellStyleXfs count="1">'
        '<xf/></cellStyleXfs><cellXfs count="2"><xf xfId="0"/><xf xfId="0" '
        'fontId="1" applyFont="1"/></cellXfs></styleSheet>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        for i, xml in enumerate(sheet_xml, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", xml)
        z.writestr("xl/styles.xml", styles)


# ---------------- 数据/回测 ----------------

def _pct(v):
    return round(v * 100, 2) if isinstance(v, (int, float)) else None


def load_codes(db, pool, bars, min_bars, limit):
    import sqlite3
    conn = sqlite3.connect(db, timeout=60)
    names = {r[0]: (r[1] or "") for r in
             conn.execute("SELECT code, name FROM stocks")}
    rows = conn.execute(
        "SELECT code, COUNT(*) FROM daily_bars GROUP BY code").fetchall()
    conn.close()
    need = max(min_bars, bars) if pool == "deep" else min_bars
    out = []
    for code, n in sorted(rows):
        if n < need or code.startswith("bj"):
            continue
        if pool == "main" and not (code.startswith("sh6")
                                   or code.startswith("sz0")):
            continue
        out.append((code, names.get(code, ""), n))
    if limit:
        out = out[:limit]
    return out


def _load_rows(code, bars):
    """直接读库（hfq）：收益率/信号口径与显示缩放无关，避免逐只联网。"""
    with sg.db_conn() as conn:
        rs = conn.execute(
            "SELECT date,open,high,low,close,vol FROM daily_bars "
            "WHERE code=? ORDER BY date DESC LIMIT ?",
            (code, int(bars))).fetchall()
    rs.reverse()
    return [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "vol": r[5]} for r in rs]


def _event_note(rows, code=""):
    """事件股检测：返回说明串（'' = 正常）。

    命中任一即视为事件股（收益不可交易/不可复制）：
      ① 单日复权涨跌 |ret| > 44%：超任意板块涨跌停（复牌无涨跌幅限制日）；
      ② 相邻K线日历间隔 > 90 天（≈ 停牌超过 60 个交易日）；
      ③ 数据停更 > 90 天 / 退市登记（v6.2.1 热修④）——退市股残留在缓存，
        「近N根」窗口内没有跳变/长停不触发①②，整段幽灵行情会被当正常
        回测（如 2005~2007 的包头铝业 +1443%），必须显式标注。
    典型：sz000578 盐湖集团 2007-07-20 停牌 → 2008-03-11 借壳复牌 +603%。"""
    import datetime as _d
    notes = []
    if _JOB_GMAX and rows:
        try:
            gap = (_d.date.fromisoformat(_JOB_GMAX)
                   - _d.date.fromisoformat(rows[-1]["date"])).days
        except (TypeError, ValueError):
            gap = 0
        if gap > 90:
            notes.append(f"数据停更{gap}天至{rows[-1]['date']}"
                         + ("（退市登记）" if code in _JOB_DELISTED else ""))
    for i in range(1, len(rows)):
        c0, c1 = rows[i - 1]["close"], rows[i]["close"]
        try:
            if c0 and c1 and abs(c1 / c0 - 1.0) > 0.44:
                notes.append(f"跳变{rows[i]['date']}({c1 / c0 - 1:+.0%})")
        except (TypeError, ZeroDivisionError):
            pass
        try:
            g = (_d.date.fromisoformat(rows[i]["date"])
                 - _d.date.fromisoformat(rows[i - 1]["date"])).days
            if g > 90:
                notes.append(f"停牌{g}天至{rows[i]['date']}")
        except (TypeError, ValueError):
            pass
        if len(notes) >= 4:
            notes.append("…")
            break
    return "；".join(notes)


def _signals_with_cache(rows, algo, rp, pre_cache):
    """v6.1.8 P1：生成信号；composite 算法按 algo 缓存 _composite_precompute 结果，
    同一只股票多档（composite×4）共用 1 次指标计算（MACD/KDJ/RSI/BOLL/ADX/MA20/
    chip_snapshots），省 ~75% composite 算量。

    v6.2.3：非 composite 信号同样按 algo 缓存——指标型信号（MACD/KDJ/…/LGBM/
    量比/板块轮动）不依赖风险参数 rp，4 档若选中同一算法可复用，避免逐档重生。
    pre_cache: dict[algo] -> _composite_precompute 输出 / ("sig", algo) -> 信号表。"""
    if algo == "composite":
        pre = pre_cache.get("composite")
        if pre is None:
            try:
                pre = sg._composite_precompute(rows)
            except Exception:
                pre = None
            pre_cache["composite"] = pre
        try:
            return sg._composite_signals(rows, rp, pre=pre) if pre else []
        except Exception:
            return []
    key = ("sig", algo)
    if key in pre_cache:
        return pre_cache[key]
    try:
        sigs = sg.strategy_signals_full(
            rows, {"algo": algo, "params": rp}, industry="")
    except Exception:
        sigs = []
    pre_cache[key] = sigs
    return sigs


def export_one(code, mode, bars, picks=None):
    """返回结果行列表（每只 1 行；tiers 多档模式每只 4 行）或 []。
    v6.1.8 P1：tiers 模式下
      ① ATR(14) 只算 1 次（4 档共用）；
      ② composite 算法的 `_composite_precompute` 算 1 次（4 档共用）。
    v6.2.3：③ 删除「backtest_signals + _bt_segments」双跑——原来每档先
    `backtest_signals`（_bt_simulate 全期+训练+验证）再 `_bt_segments`
    覆盖（_bt_events 全期+训练+验证），同一档回测跑两遍；现只跑
    `_bt_segments`（GUI 消融弹窗同引擎），IC 由 `_signal_ic` 直接算。"""
    rows = _load_rows(code, bars)
    if not rows or len(rows) < 60:
        return []
    base = {"code": code, "bars": len(rows), "start": rows[0]["date"],
            "end": rows[-1]["date"], "event": _event_note(rows, code)}
    pre_cache = {}      # algo -> _composite_precompute 输出 / ("sig", algo) -> 信号

    def _row(strat_algo, tier, label, rp, atrs=None):
        sigs = _signals_with_cache(rows, strat_algo, rp, pre_cache)
        n_buy = sum(1 for s in sigs if s[2] == "BUY")
        r = dict(base, algo=strat_algo, mode=tier, label=label,
                 n_sig=len(sigs), n_buy=n_buy, n_sell=len(sigs) - n_buy)
        if sigs:
            # v6.1.6：与「策略消融」/GUI 信号胜率面板同引擎同切分
            # （_bt_events，val=max(200,n/4)），指标与选型口径一致。
            # v6.1.8 P1：tiers 模式预先算好 ATR，4 档共用。
            ev_full, ev_tr, ev_va, _sp = sg._bt_segments(rows, sigs, rp,
                                                         atrs=atrs)
            if ev_full:
                tr = ev_tr or {}
                va = ev_va or {}
                ic1 = sg._signal_ic(rows, sigs, 1)[0]
                ic5 = sg._signal_ic(rows, sigs, 5)[0]
                r.update({
                    "trades": ev_full.get("trades"),
                    "winrate": _pct(ev_full.get("winrate")),
                    "total": _pct(ev_full.get("total")),
                    "ann": _pct(ev_full.get("ann")),
                    "mdd": _pct(ev_full.get("mdd")),
                    "pl": (round(ev_full["profit_loss"], 2)
                           if ev_full.get("profit_loss")
                           not in (None, float("inf")) else None),
                    "avg_win": _pct(ev_full.get("avg_win")),
                    "avg_loss": _pct(ev_full.get("avg_loss")),
                    "float": _pct(ev_full.get("floating")),
                    "train": _pct(tr.get("total")),
                    "val": _pct(va.get("total")),
                    "ic1": (round(ic1, 4) if ic1 is not None else None),
                    "ic5": (round(ic5, 4) if ic5 is not None else None),
                })
        return r

    if mode == "tiers":
        src = (picks or {}).get(code)
        pool = (sg._ablation_pool(src, 8)
                if isinstance(src, list) and src else [])
        # v6.1.8 P1：4 档共用 1 次 ATR(14)（ATR 不依赖 rp/sigs，原 4× 重算）
        atrs_shared = sg._precompute_atr(rows, 0, len(rows))
        rows_out = []
        for tier in ("保守", "稳健", "激进"):
            pk = src.get(tier) if isinstance(src, dict) else None
            if pk is None and pool:      # 兼容全候选格式：就地按档选型
                pk, _note = sg._pick_one_from_pool(pool, tier)
            if pk:
                # v6.2.3：三档（保守/稳健/激进），选型决定 algo/信号源，
                # 风控参数取候选自身 params（缺失回退该档固定参数）。
                rp = pk.get("params") or sg.CFG.RISK_PARAMS.get(
                    tier, sg.CFG.RISK_PARAMS["稳健"])
                rows_out.append(_row(pk.get("algo", "composite"), tier,
                                     pk.get("label", ""), rp,
                                     atrs=atrs_shared))
            else:   # 不在消融清单：固定多维评分回退
                rows_out.append(_row("composite", tier,
                                     f"多维评分·{tier}（无消融候选回退）",
                                     sg.CFG.RISK_PARAMS.get(
                                         tier, sg.CFG.RISK_PARAMS["稳健"]),
                                     atrs=atrs_shared))
        return rows_out
    if mode in sg.CFG.RISK_PARAMS:       # 单档：固定用该档风险参数
        strat = sg.load_strategy(code)
        algo = (strat or {}).get("algo") or "composite"
        rp = dict(sg.CFG.RISK_PARAMS[mode])
        return [_row(algo, mode,
                     f"{sg.ALGO_LABEL.get(algo, algo)}·{mode}", rp)]
    strat = sg.load_strategy(code) if mode == "cached" else None
    algo = (strat or {}).get("algo") or "composite"
    rp = (strat or {}).get("params") or sg.CFG.RISK_PARAMS["稳健"]
    mk = (strat or {}).get("mode") or ("缓存" if strat else "稳健")
    return [_row(algo, mk, (strat or {}).get("label", ""), rp)]


# ---------------- 多进程 worker（GIL 无关；每只独立、只读库） ----------------

_JOB_MODE = "tiers"
_JOB_BARS = 1000
_JOB_GMAX = ""                  # 全库最新交易日（判断数据停更）
_JOB_DELISTED = frozenset()     # delisted 表登记的退市代码


def _init_worker(mode, bars, db, gmax="", delisted=()):
    """子进程初始化：注入回测参数并锁定库路径（覆盖 forkserver/fork 差异）。
    gmax/delisted：v6.2.1 热修④——数据停更/退市股事件标注所需。"""
    global _JOB_MODE, _JOB_BARS, _JOB_GMAX, _JOB_DELISTED
    _JOB_MODE, _JOB_BARS = mode, bars
    _JOB_GMAX, _JOB_DELISTED = gmax or "", frozenset(delisted or ())
    if db:
        sg.DB_PATH = db


def _work_one(item):
    """单只回测任务：item=(code, name, 该股四档选型源)。异常不终止整池。"""
    code, name, src = item
    try:
        got = export_one(code, _JOB_MODE, _JOB_BARS, {code: src})
    except Exception as e:
        print(f"  [!] {code} {str(e)[:70]}", flush=True)
        return []
    for r in got:
        r["name"] = name
    return got


COLS = [
    ("代码", "code"), ("名称", "name"), ("K线根数", "bars"),
    ("起始日", "start"), ("结束日", "end"), ("档位", "mode"),
    ("策略", "algo"), ("标签", "label"), ("信号数", "n_sig"),
    ("BUY", "n_buy"), ("SELL", "n_sell"), ("交易数", "trades"),
    ("胜率%", "winrate"), ("总收益%", "total"), ("年化%", "ann"),
    ("最大回撤%", "mdd"), ("盈亏比", "pl"), ("平均盈利%", "avg_win"),
    ("平均亏损%", "avg_loss"), ("浮动收益%", "float"),
    ("训练段收益%", "train"), ("验证段收益%", "val"),
    ("IC(T+1)", "ic1"), ("IC(T+5)", "ic5"), ("事件", "event"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=sg.DB_PATH)
    ap.add_argument("--out", default="",
                    help="输出 xlsx 路径（默认建版本化运行目录 "
                         "research/perstock_backtest_v<版本>_<时间戳>/）")
    ap.add_argument("--bars", type=int, default=1000, help="每只回测近N根")
    ap.add_argument("--pool", default="all",
                    choices=["all", "main", "deep"])
    ap.add_argument("--mode", default="tiers",
                    choices=["tiers", "保守", "稳健", "激进",
                             "cached"],
                    help="tiers=四档（按消融选型，分表输出，默认）；"
                         "保守/稳健/激进=单档固定风险参数；cached=当前缓存策略")
    ap.add_argument("--picks", default=os.path.join(
        ROOT, "research", "perstock_tier_picks.json"),
        help="四档选型 JSON（tier_picks_from_ablation.py 产物；"
             "也兼容 strategy_ablation_per_stock.json 全候选格式）")
    ap.add_argument("--workers", type=int, default=0,
                    help="并行进程数（0=自动取 min(8, cpu)；"
                         "run_backtest.sh 与桌面快捷方式用 ≤8）")
    ap.add_argument("--chunksize", type=int, default=0,
                    help="每进程批量回测股票数（0=自动，库≥4000 时 20/worker，"
                         "否则 10；提交粒度变粗→IPC 开销降低）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-bars", type=int, default=250)
    args = ap.parse_args()
    if args.workers == 0:
        import multiprocessing as _mp
        args.workers = max(1, min(8, _mp.cpu_count() or 4))
    if args.chunksize == 0:
        # 经验值：每 worker 约 30~50 个 chunk 时负载均衡最佳；
        # 总数 6897 / workers=8 ≈ 862 / worker → chunksize ≈ 30
        # 但股票间耗时差异大（小票/长信号/无信号差异 3-5×），保守取 20。
        args.chunksize = 20

    os.environ["STOCK_DB"] = args.db
    bars = max(250, min(2400, args.bars))
    picks = {}
    if args.mode == "tiers":
        try:
            with open(args.picks, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):      # 瘦身三档格式 {code: {档位: {...}}}
                picks = d
            else:                        # 兼容全候选格式（list）
                for s in d:
                    if s and s.get("code"):
                        picks[s["code"]] = s.get("all_candidates") or []
        except Exception as e:
            print(f"[警告] 读取四档选型失败（{e}），全部按多维评分回退")
        else:
            print(f"四档选型 {len(picks)} 只（{os.path.basename(args.picks)}）")
    codes = load_codes(args.db, args.pool, bars, args.min_bars, args.limit)
    print(f"库 {args.db}\n待回测 {len(codes)} 只（近{bars}根，"
          f"策略={args.mode}，pool={args.pool}，workers={args.workers}）",
          flush=True)
    if not codes:
        print("无标的可导出")
        return

    t0 = time.time()
    # v6.2.1 热修④：全库最新交易日 + 退市登记，供事件检测判「数据停更」
    import sqlite3 as _sq
    _conn = _sq.connect(args.db, timeout=60)
    try:
        gmax = _conn.execute("SELECT MAX(date) FROM daily_bars").fetchone()[0] or ""
        try:
            delisted = [r[0] for r in _conn.execute("SELECT code FROM delisted")]
        except _sq.OperationalError:
            delisted = []
    finally:
        _conn.close()
    rows_out = []
    done = 0
    src_map = picks if isinstance(picks, dict) else {}
    tasks = [(c, n, src_map.get(c)) for c, n, _n in codes]
    workers = max(1, min(24, int(args.workers)))
    chunksize = max(5, int(args.chunksize))
    print(f"进程池 {workers} 进程并行回测（chunk={chunksize}）...", flush=True)

    # v6.1.8 P1：改用 map + chunksize 批量提交——
    # 旧版每只 1 次 submit（6897 次 IPC + 进程内队列维护）；
    # chunksize=20 把任务切成 ~345 chunk，IPC 与子进程 overhead 显著下降，
    # 同时各 worker 一次吃 20 只股票也避免小任务切换（实测 ~25% 提速）。
    with ProcessPoolExecutor(max_workers=workers,
                             initializer=_init_worker,
                             initargs=(args.mode, bars, args.db,
                                       gmax, delisted)) as exp:
        for got in exp.map(_work_one, tasks, chunksize=chunksize):
            rows_out.extend(got)
            done += 1
            if done % 200 == 0 or done == len(codes):
                el = time.time() - t0
                eta = el / done * (len(codes) - done)
                print(f"回测 {done}/{len(codes)} "
                      f"({done * 100 // len(codes)}%) "
                      f"耗时{el:.0f}s ETA{eta:.0f}s", flush=True)

    header = [c[0] for c in COLS]
    tiers = [t for t in ("保守", "稳健", "激进")
             if any(r.get("mode") == t for r in rows_out)]

    def _table(rs):
        rs = sorted(rs, key=lambda r: (r.get("total") is None,
                                       -(r.get("total") or -1e9)))
        return [header] + [[r.get(k) for _, k in COLS] for r in rs]

    meta = [["每只股回测导出", ""],
            ["生成时间", time.strftime("%Y-%m-%d %H:%M:%S")],
            ["库", args.db], ["回测根数", bars], ["策略", args.mode],
            ["消融选型来源", (os.path.basename(args.picks)
                          if args.mode == "tiers" else "-")],
            ["标的池", args.pool], ["并发", args.workers],
            ["有效标的", len({r["code"] for r in rows_out})],
            ["数据行数", len(rows_out)],
            ["耗时(秒)", round(time.time() - t0)],
            ["口径", "T日收盘信号→T+1成交；ATR止损+移动止盈；"
                     "训练段=前75%，验证段=后25%；IC=信号方向与未来收益Spearman"],
            ["三档选型", "保守/稳健/激进 = 消融候选池按该档目标（多指标"
                     " rank + 训练段末尾近端一致性）逐股选优；"
                     "激进目标偏年化+Calmar，保守/稳健偏 Calmar+PF"],
            ["⚠ 口径提醒",
             "「总收益%/年化%/胜率%」是【全周期】回测，含用于选型的训练段，"
             "受“从~30个候选里挑最好”的赢家诅咒影响，数值严重偏乐观"
             "（全库实测：全期中位 +44~55%、正收益 92~98%，而验证段中位 "
             "-0.7%、正收益仅 46-47%）。跨股/跨档比较请优先看「训练段收益%」"
             "与「验证段收益%」两列，验证段是选型之外的留出数据"],
            ["事件股口径", "单日复权|涨跌|>44% 或 相邻K线间隔>90天(≈停牌>60个交易日)"
                       " 或 数据停更>90天/退市登记 → 「事件」列标注；此类收益来自"
                       "重组复牌/退市整理等公司行动，"
                       "不可交易、不可复制，仪表盘默认从统计中剔除（可在页内勾选查看）"],
            ["注意", "全表为历史统计，含样本内选型偏差，不构成投资建议"]]

    run_dir = None
    out = args.out
    if not out:
        run_dir = bt_common.new_run_dir(RESEARCH, "perstock_backtest")
        out = os.path.join(run_dir, "每只股回测.xlsx")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if out.lower().endswith(".xlsx"):
        sheets = [(t, _table([r for r in rows_out if r.get("mode") == t]))
                  for t in tiers] or [("每只股回测", _table(rows_out))]
        sheets.append(("说明", meta))
        write_xlsx(out, sheets)
        csv_path = os.path.splitext(out)[0] + ".csv"
    else:
        csv_path = out
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        cw = csv.writer(f)
        cw.writerows(_table(rows_out))
    ev_codes = {r["code"] for r in rows_out if r.get("event")}
    if run_dir:
        bt_common.write_run_meta(
            run_dir, argv=sys.argv, elapsed=time.time() - t0,
            mode=args.mode, bars=bars, pool=args.pool, workers=args.workers,
            picks=os.path.basename(args.picks) if args.mode == "tiers" else None,
            codes=len({r["code"] for r in rows_out}), rows=len(rows_out),
            tiers=tiers, event_codes=len(ev_codes),
            xlsx=os.path.basename(out),
            csv=os.path.basename(csv_path))
    print(f"完成：{len({r['code'] for r in rows_out})} 只 / "
          f"{len(rows_out)} 行（档位 {tiers}；事件股 {len(ev_codes)} 只已标注，"
          f"仪表盘默认剔除）\n  Excel: {out}\n  CSV  : {csv_path}")
    # 自动刷新本地网页仪表盘「每只股回测」页（失败不影响导出产物）
    try:
        from v61_dashboard import build_dashboard
        print(f"  网页  {build_dashboard(RESEARCH)}")
    except Exception as e:
        print(f"网页仪表盘刷新失败（忽略）: {e}")
    print(f"总耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
