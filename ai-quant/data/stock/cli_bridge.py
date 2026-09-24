"""stock_predict CLI 桥接层：把单文件预测软件当作数据/信号/选股引擎。

- 数据：db_rows 直读缓存（回测用，无网络）；get_daily 增量刷新（实盘用）
- 选股：candidates（按市值扫描 + daily_pick_score 排名，含空头闸门/ST过滤）
- 信号：signals（CLI 多维评分 _composite_signals + RISK_PARAMS）
- 行情：quote（腾讯实时快照）
- 日历：trade_dates（缓存内全部交易日）

CLI 位于 scripts/cli/stock_predict.py（由仓库根 build_cli.py 生成，勿手改）。
"""
import importlib.util
import json
import os
import sqlite3
import sys
import time

BASE_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLI_DIR = os.path.join(BASE_DIR, "scripts", "cli")
CLI_PATH = os.path.join(CLI_DIR, "stock_predict.py")
DB_PATH = os.path.join(CLI_DIR, "stock_cache.db")
UNIVERSE_CACHE = os.path.join(BASE_DIR, "sim", "state", "universe_cache.json")

_cli = None


def load():
    """导入 CLI 模块（惰性，进程内单例）。"""
    global _cli
    if _cli is None:
        if CLI_DIR not in sys.path:
            sys.path.insert(0, CLI_DIR)
        spec = importlib.util.spec_from_file_location(
            "stock_predict", CLI_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["stock_predict"] = mod
        spec.loader.exec_module(mod)
        _cli = mod
    return _cli


def db_connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def latest_date():
    with db_connect() as con:
        r = con.execute("SELECT MAX(date) FROM daily_bars").fetchone()
        return r[0] if r and r[0] else None


def trade_dates(start=None):
    """缓存内全部交易日（升序）。"""
    with db_connect() as con:
        if start:
            rows = con.execute(
                "SELECT DISTINCT date FROM daily_bars WHERE date >= ? "
                "ORDER BY date", (str(start),)).fetchall()
        else:
            rows = con.execute(
                "SELECT DISTINCT date FROM daily_bars ORDER BY date"
            ).fetchall()
        return [r[0] for r in rows]


def db_rows(code, tail=None):
    """直读缓存日K（后复权，升序）。回测主路径，不触发网络。"""
    with db_connect() as con:
        if tail:
            rows = con.execute(
                "SELECT date,open,high,low,close,vol FROM ("
                "  SELECT date,open,high,low,close,vol FROM daily_bars "
                "  WHERE code=? ORDER BY date DESC LIMIT ?"
                ") ORDER BY date", (code, int(tail))).fetchall()
        else:
            rows = con.execute(
                "SELECT date,open,high,low,close,vol FROM daily_bars "
                "WHERE code=? ORDER BY date", (code,)).fetchall()
    return [{"date": r["date"], "open": r["open"], "high": r["high"],
             "low": r["low"], "close": r["close"], "vol": r["vol"] or 0.0}
            for r in rows]


def row_on(code, date):
    """指定日K线（回测成交/涨跌停校验用）。"""
    with db_connect() as con:
        r = con.execute(
            "SELECT date,open,high,low,close,vol FROM daily_bars "
            "WHERE code=? AND date=?", (code, str(date))).fetchone()
    if not r:
        return None
    return {"date": r["date"], "open": r["open"], "high": r["high"],
            "low": r["low"], "close": r["close"], "vol": r["vol"] or 0.0}


def prev_close(code, date):
    """date 之前最近一根收盘价（涨跌停校验用）。"""
    with db_connect() as con:
        r = con.execute(
            "SELECT close FROM daily_bars WHERE code=? AND date<? "
            "ORDER BY date DESC LIMIT 1", (code, str(date))).fetchone()
    return r["close"] if r else None


def is_etf(code):
    cli = load()
    try:
        return bool(cli._is_etf(code))
    except Exception:
        c = str(code).lower()
        return c.startswith(("sh51", "sh56", "sh58", "sz15", "sz16"))


def _scan_universe():
    """全库扫描股票池（慢，Pi 上约 90s）：缓存到 sim/state/universe_cache.json。"""
    with db_connect() as con:
        latest = con.execute("SELECT MAX(date) FROM daily_bars").fetchone()[0]
        if not latest:
            return None, []
        stats = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT code, COUNT(*), MAX(date) FROM daily_bars "
            "GROUP BY code")}
        info = {r[0]: (r[1] or "", r[2] or "", r[3] or 0.0)
                for r in con.execute(
                    "SELECT code, name, industry, mktcap FROM stocks")}
        try:
            delisted = {r[0] for r in con.execute(
                "SELECT code FROM delisted").fetchall()}
        except sqlite3.OperationalError:
            delisted = set()
        # 每只股票自己的最新收盘（各股最后交易日可能不同）
        last_close = {r[0]: r[1] for r in con.execute(
            "SELECT code, close FROM ("
            "  SELECT code, close, ROW_NUMBER() OVER ("
            "    PARTITION BY code ORDER BY date DESC) rn"
            "  FROM daily_bars"
            ") WHERE rn=1")}
    rows = []
    for code, (cnt, mx) in stats.items():
        if code in delisted:
            continue
        if code.startswith(("sh000", "sz399")):     # 指数（上证/深证等）
            continue
        name, industry, mktcap = info.get(code, ("", "", 0.0))
        if industry == "ETF" or is_etf(code):
            continue
        if code.startswith("bj"):
            continue
        if "ST" in name.upper() or "退" in name:
            continue
        rows.append([code, name or code[-6:], industry, mktcap,
                     last_close.get(code) or 0.0, mx, cnt])
    return latest, rows


def _load_universe_cache():
    try:
        with open(UNIVERSE_CACHE, encoding="utf-8") as f:
            d = json.load(f)
        return d if d.get("latest") and d.get("rows") else None
    except Exception:
        return None


def _save_universe_cache(latest, rows):
    try:
        os.makedirs(os.path.dirname(UNIVERSE_CACHE), exist_ok=True)
        tmp = UNIVERSE_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"latest": latest, "ts": time.time(), "rows": rows},
                      f, ensure_ascii=False)
        os.replace(tmp, UNIVERSE_CACHE)
    except OSError:
        pass


def universe(min_bars=400, recent_days=15, exclude_etf=True,
             exclude_bj=True, min_price=2.0, max_scan=None, boards="all",
             refresh=False):
    """股票池 [(code, name, industry, mktcap)]，按市值降序（无市值的排后）。

    boards="main" 只保留沪深主板（sh60/sz00），剔除创业板/科创板/北交所。
    首次扫描慢（Pi ~90s），结果缓存到 sim/state/universe_cache.json，
    DB 最新日期不变时后续调用秒开；refresh=True 强制重扫。
    """
    latest = latest_date()
    if not latest:
        return []
    cache = None if refresh else _load_universe_cache()
    if not cache or cache.get("latest") != latest:
        latest, rows = _scan_universe()
        if not rows:
            return []
        _save_universe_cache(latest, rows)
        cache = {"latest": latest, "rows": rows}
    cutoff = time.strftime(
        "%Y-%m-%d", time.localtime(
            time.mktime(time.strptime(latest, "%Y-%m-%d"))
            - int(recent_days) * 86400))
    out = []
    for code, name, industry, mktcap, close, last, cnt in cache["rows"]:
        if cnt < min_bars or last < cutoff:
            continue
        if exclude_etf and industry == "ETF":
            continue
        if exclude_bj and code.startswith("bj"):
            continue
        if boards == "main" and not code.startswith(("sh60", "sz00")):
            continue
        if close < min_price:
            continue
        out.append((code, name, industry, mktcap))
    out.sort(key=lambda x: -x[3])
    return out[:max_scan] if max_scan else out


def candidates(top_n=30, max_scan=300, min_bars=400, min_price=2.0,
               recent_days=15, risk_mode="稳健"):
    """每日候选：缓存扫描 + daily_pick_score 排名（口径同 CLI daily_picks）。"""
    cli = load()
    codes = universe(min_bars=min_bars, recent_days=recent_days,
                     min_price=min_price, max_scan=max_scan)
    try:
        ind5_map, ind5_med, ind5_lead = cli._picks_ind_ctx()
    except Exception:
        ind5_map, ind5_med, ind5_lead = {}, 0.0, set()
    buy_th = risk_params(risk_mode)["buy_th"]
    picks = []
    for code, name, industry, mktcap in codes:
        rows = db_rows(code, tail=400)
        if len(rows) < min_bars:
            continue
        try:
            r = cli.daily_pick_score(rows, ind_ctx={
                "r5": ind5_map.get(industry),
                "med": ind5_med,
                "lead": industry in ind5_lead})
        except Exception:
            continue
        if r is None:
            continue
        score, reasons, band, gates = r
        if gates.get("ma_trend", 0) <= -2:
            continue
        if score < buy_th:
            continue
        prev = rows[-2]["close"] if len(rows) > 1 else 0
        chg = (rows[-1]["close"] / prev - 1) * 100 if prev else 0.0
        picks.append({
            "code": code, "name": name, "industry": industry,
            "close": rows[-1]["close"], "chg": round(chg, 2),
            "score": score, "reasons": " ".join(reasons) or "-",
            "band": band, "mktcap": mktcap,
        })
    picks.sort(key=lambda x: -x["score"])
    return picks[:top_n]


def risk_params(mode="稳健"):
    cli = load()
    params = cli.CFG.RISK_PARAMS
    return dict(params.get(mode) or params["稳健"])


def signals(rows, mode="稳健"):
    """CLI 多维评分买卖点：[(i, date, BUY/SELL, reason)]（因果）。"""
    cli = load()
    return cli._composite_signals(rows, risk_params(mode))


def precompute(rows):
    cli = load()
    return cli._composite_precompute(rows, 400, True)


def atr(rows):
    cli = load()
    return cli._precompute_atr(rows, 0, len(rows))


def series(code, tail=None, min_bars=100):
    """实盘路径：增量刷新后的日K（可能触发网络）。"""
    return load().get_daily(code, min_bars=min_bars, tail=tail)


def quote(code):
    """腾讯实时行情快照 {name,price,prev_close,open,high,low,time}。"""
    return load().fetch_quote(code)


def index_series(code="sh000001"):
    """指数日K（缓存内，如 sh000001）。"""
    return db_rows(code)
