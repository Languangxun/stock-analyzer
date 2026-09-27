# -*- coding: utf-8 -*-
"""ETF缓存盘点 + 回填主流ETF（v6.1.5 热修⑪：走统一复权口径）。

库内统一存真乘法后复权：`_bf_fetch_one` = 腾讯不复权 × 新浪因子（ETF 无
有效因子时退腾讯 qfq），不再用新浪不复权直写（会与库内口径混接）。"""
from stock_gui import (db_conn, _is_etf, _bf_fetch_one, _bar_ok,
                       _sync_adjust)

with db_conn() as conn:
    codes = [r[0] for r in conn.execute("SELECT DISTINCT code FROM daily_bars")]
etfs = [c for c in codes if _is_etf(c)]
print("已缓存ETF:", etfs)

# 主流宽基/行业ETF
want = ["sh510300", "sh510500", "sh510050", "sz159915", "sh588000",
        "sh512100", "sz159949", "sh510880", "sz159922", "sh512880",
        "sh515790", "sz159992", "sh512690", "sz159928", "sh515030",
        "sh512480", "sz159819", "sh516160", "sz159869", "sh513050"]
got = 0
for c in want:
    try:
        rows, _raw = _bf_fetch_one(c, target=1600)
        rows = [r for r in rows if _bar_ok(r)]
        if len(rows) >= 50:
            with db_conn(commit=True) as conn:
                conn.execute("DELETE FROM daily_bars WHERE code=?", (c,))
                conn.executemany(
                    "INSERT OR REPLACE INTO daily_bars"
                    "(code,date,open,high,low,close,vol) VALUES(?,?,?,?,?,?,?)",
                    [(c, r["date"], r["open"], r["high"], r["low"],
                      r["close"], r["vol"]) for r in rows])
            _sync_adjust(c, rows)
            got += 1
            print(f"  {c}: {len(rows)}根")
        else:
            print(f"  {c}: 仅{len(rows)}根，跳过")
    except Exception as e:
        print(f"  {c}: 失败 {str(e)[:40]}")
with db_conn() as conn:
    n = conn.execute("SELECT COUNT(*) FROM daily_bars WHERE code IN (%s)"
                     % ",".join("?" * len(want)), want).fetchone()[0]
print(f"回填完成 {got}/{len(want)}，ETF总bars {n}")
