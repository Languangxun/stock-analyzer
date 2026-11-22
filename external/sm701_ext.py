#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""external/sm701_ext.py - sm70x 外接算法适配层（v7.4.0）

sm701（桌面算法库，sm70x 系列）作为**外接算法**接入本项目：模型定位 /
副本库只读连接 / 特征工程加载 / 概率复算（全市场 + 单股）/ 买卖点信号与
状态。本模块是唯一入口；sm701 目录（数据 + 模型，约 7GB）保存在仓库外
（默认 ~/桌面/sm701，副本优先），git 不分发；适配层代码随主库上传，换
机器装好 sm701 或用 SM70_DIR（兼容 SM701_DIR）指定路径即可复用。

接入约定（v7.4.0）:
  ini [predict] sm701_ext=1       外接算法接入开关（默认开）。关 → hk/us/fut
                                  只看行情不出买卖点、A股消融剔除 sm701 候选。
  ini [predict] sm701_ablation=1  sm701 参加 A股单股策略消融候选（默认开；
                                  关 → 仅多市场直出/独立使用，A股消融不跑它）。
  market: cn（A股消融候选/单市场直出）/ hk / us / futures / crypto（生产
  三模型已接入；crypto h10 依赖 crypto_hourly 分时特征，缺失时自动只用
  h1/h5 等权）。

v7.5.2 加密 UTC 口径:
  - crypto 日K按 UTC 自然日切分（当日 bar 要到次日 00:00 UTC 才完成），
    最新日判断 / 盘中 30 分钟缓存桶均改用 UTC；北京时区凌晨不再把
    UTC 当日未完成 bar 当成“昨日”缓存，也不会整天反复判过期。
  - `sm70x_series()` 返回已连接的 sm701 / sm702 / 其它 sm70x 系列目录名，
    仅供关于页署名（开源仓库无模型时不显示任何 sm70x 标识）。

v7.4.0 兼容性增强:
  - 模型文件 CRLF 容错：sm701 仓库在 Windows 检出后 `production/prod_*.txt`
    可能被 git 转成 CRLF，LightGBM 直接报 Model format error；加载时统一
    规范化为 LF，并按 (mtime, size) 缓存 Booster（原实现每次复算都重读
    3 个 5~17MB 模型）。
  - 数据/预测产物缺失降级：sm701 目录可以只有代码+模型（新克隆默认没有
    stock_cache.db / valid_predictions*.csv.gz）；此时 A股用主库行情实时
    复算、历史段留空，不再整体判定“无模型”。非 cn 市场优先读 sm701 库
    对应表，其次读仓库同级/桌面 global_daily.db，最后读主库同前缀行情。
  - 路径除 SM701_* 外新增 SM70_DIR / SM70_DB / SM70_MODEL / SM70_PRED
    别名，并探测桌面其它 sm70* 目录（sm702 等后续系列模型）。

宿主注入：init_host(gui_globals) 由 stock_gui 调用，把 np / db_conn /
DB_PATH / _market_of / tier_load_panel 以名称惰性解析传入；本模块**不
import stock_gui**，避免双向依赖。build_cli 生成的 CLI 同样可用
（需 external/ 与主文件同目录）。
"""
import csv
import glob
import gzip
import logging
import os
import sqlite3
import threading
import time

try:
    import numpy as np          # noqa: F401  (init_host 会用宿主 np 覆盖)
except ImportError:
    np = None                   # noqa: F401

log = logging.getLogger("external.sm701_ext")

# ---- 宿主惰性解析表 ----
_HOST = None


def init_host(gui_globals):
    """注入宿主 stock_gui 的模块字典；调用后本模块 np 亦与宿主同步
    （宿主 numpy 可缺失时为 None，各入口按原口径降级）。"""
    global _HOST
    _HOST = gui_globals
    globals()["np"] = gui_globals.get("np")


def _market_of(full):
    return _HOST["_market_of"](full)


def _db_conn(*args, **kwargs):
    return _HOST["db_conn"](*args, **kwargs)


def _DB_PATH():
    return _HOST["DB_PATH"]


def _env_first(*names):
    """按顺序取第一个非空环境变量（v7.4.0：SM70_* 优先，兼容 SM701_*）。"""
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


def tier_load_panel():
    return _HOST["tier_load_panel"]()


# 独立只读接入桌面 sm701 副本（默认优先 `sm701 (副本)`），不复制 7GB 扩展库：
#   stock_cache.db          日K/股票/行业等数据（只读连接）
#   lgbm_train_backtest.py  特征工程 _compute_feats（模型/历史预测同口径）
# v6.5.2：优先接入生产定稿三周期模型（`production/` 目录）——
#   production/features_tech.json        tech 40 特征名/列序
#   production/prod_h1|h5|h10_tech.txt    T+1/T+5/T+10 三模型（实时复算等权平均）
#   production/valid_predictions.csv.gz   2024-01 起历史样本外分数（三模型）
# 回退旧单模型：lgbm_output/lgbm_model.txt + valid_predictions.csv.gz。
# 路径可用 SM701_DIR / SM701_DB / SM701_MODEL / SM701_PRED 覆盖
# （设置 SM701_MODEL 时强制单模型口径，跳过 production/）。
_SM701_CACHE = {}
_SM701_LOCK = threading.RLock()
_SM701_CRYPTO_HOURLY = "crypto_hourly_daily_probs.csv.gz"
_SM701_WARMUP_BARS = 180


_SM701_MK = {
    # 市场 -> (模型前缀, 历史预测文件, 副本库行情表)；cn 表=daily_bars
    "cn":      {"mpre": "",     "pred": "valid_predictions.csv.gz",
                "table": "daily_bars"},
    "hk":      {"mpre": "hk_",  "pred": "valid_predictions_hk.csv.gz",
                "table": "hk_daily"},
    "us":      {"mpre": "us_",  "pred": "valid_predictions_us.csv.gz",
                "table": "us_daily"},
    "futures": {"mpre": "fut_", "pred": "valid_predictions_fut.csv.gz",
                "table": "fut_daily"},
    # v7.4.0：加密生产集（E1 beta 中性标签，T+1 00:00 UTC 执行）；
    # h10 额外依赖 crypto_hourly 的 11 个分时特征，见 _sm701_intraday_feats。
    "crypto":  {"mpre": "crypto_", "pred": "valid_predictions_crypto.csv.gz",
                "table": "crypto_daily"},
}


def _sm701_code(full):
    """本项目前缀代码 → sm701 预测文件/副本库 code 形态：
    hk00700→'00700'、usaapl→'AAPL'、fua0→'A0'、cryptobtcusdt→'BTCUSDT'；
    cn 原样。"""
    mk = _market_of(full)
    if mk == "hk":
        return full[2:].zfill(5)
    if mk in ("us", "futures"):
        return full[2:].upper()
    if mk == "crypto":
        body = full[6:].upper()
        # 允许 cryptoBTC / cryptoBTCUSDT 两种输入，统一成 sm701 交易对
        if body.isalnum() and not body.endswith(("USDT", "USD", "BTC", "ETH")):
            body += "USDT"
        return body
    return full


def _sm701_production_set(base, mpre=""):
    """桌面 sm701 `production/` 三周期 tech 模型集（mpre=市场前缀）；缺失
    任一文件返回 None。v6.6.5 多市场：prod_{hk|us|fut}_h{1,5,10}_tech.txt。"""
    pdir = os.path.join(base, "production")
    man = os.path.join(pdir, "features_tech.json")
    models = {h: os.path.join(pdir, f"prod_{mpre}h{h}_tech.txt")
              for h in ("1", "5", "10")}
    if not (os.path.isfile(man)
            and all(os.path.isfile(v) for v in models.values())):
        return None
    try:
        import json
        with open(man, encoding="utf-8") as f:
            feats = json.load(f).get("features") or []
    except Exception:
        log.exception("读取 sm701 生产特征清单失败")
        return None
    if not feats:
        return None
    pred = os.path.join(pdir, "valid_predictions" +
                        ("" if not mpre else "_" + mpre[:-1]) + ".csv.gz")
    return {"dir": pdir, "features": feats, "models": models, "pred": pred}


def _sm701_model_mtimes(paths):
    """模型文件 mtime 元组（生产三模型逐个；否则单模型），供缓存失效。"""
    paths = paths or {}
    prod = paths.get("prod")
    files = (list(prod["models"].values()) if prod else [paths.get("model")])
    out = []
    for p in files:
        try:
            out.append(os.path.getmtime(p))
        except (OSError, TypeError):
            out.append(0.0)
    return tuple(out)


_COLUMN_CACHE = {}
_GLOBAL_MK = {"cn": "CN", "hk": "HK", "us": "US", "futures": "FUT",
              "crypto": "CRYPTO"}
_GLOBAL_QUOTES = ("USDT", "USD", "BTC", "ETH")


def _db_columns(db, table):
    """只读库表列名（缓存）；表不存在/库不可读返回空元组。"""
    if not db or not os.path.isfile(db):
        return ()
    key = (os.path.abspath(db), table)
    hit = _COLUMN_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        con = _sm701_conn(db)
        try:
            cols = tuple(r[1] for r in con.execute(
                f"pragma table_info({table})"))
        finally:
            con.close()
    except sqlite3.Error:
        cols = ()
    _COLUMN_CACHE[key] = cols
    return cols


def _sm701_has_table(db, table):
    """只读库是否含某表（结果缓存；库不存在/不可读返回 False）。"""
    return bool(_db_columns(db, table))


def _sm701_global_code(sm_code, mk):
    """sm701 代码 → global_daily.db 代码：crypto BTCUSDT→BTC/USDT，其余同名。"""
    if mk != "crypto":
        return sm_code
    for q in _GLOBAL_QUOTES:
        if sm_code.endswith(q) and len(sm_code) > len(q):
            return sm_code[:-len(q)] + "/" + q
    return sm_code


def _sm701_db_kind(db, mk):
    """判断库/市场可用性：('table', 行情表) / ('global', 市场名) / (None, None)。

    v7.4.0 支持两种外部行情库：
      - sm701 原生：hk_daily/us_daily/fut_daily/crypto_daily（cn=daily_bars）
      - global_daily.db：统一 daily_bars(market,code,date,volume...) +
        stocks(market,code,name)，market=HK/US/FUT/CRYPTO。
    """
    if not db or not os.path.isfile(db):
        return None, None
    cols = _db_columns(db, "daily_bars")
    if "market" in cols:
        # 统一多市场库（global_daily.db）；cn 仍走宿主主库，避免空市场
        return (None, None) if mk == "cn" else ("global", _GLOBAL_MK[mk])
    table = _SM701_MK[mk]["table"]
    if _sm701_has_table(db, table):
        return "table", table
    if mk == "cn" and "date" in cols and "close" in cols:
        return "table", "daily_bars"
    return None, None


def _sm701_market_latest(db, mk):
    """该市场数据最新交易日；库/表缺失返回空串。"""
    kind, val = _sm701_db_kind(db, mk)
    if not kind:
        return ""
    sql = (f"select max(date) from {val}" if kind == "table"
           else "select max(date) from daily_bars where market=?")
    try:
        con = _sm701_conn(db)
        try:
            row = con.execute(sql, () if kind == "table" else (val,)).fetchone()
        finally:
            con.close()
        return (row[0] if row else "") or ""
    except sqlite3.Error:
        return ""


def _sm701_read_bars(db, mk, sm_code, start="2010-01-01"):
    """读单只标的日K（sm701 原生表或 global_daily 统一表），返回 DataFrame。"""
    import pandas as pd
    kind, val = _sm701_db_kind(db, mk)
    if not kind:
        return pd.DataFrame()
    if kind == "table":
        sql = (f"SELECT code,date,open,high,low,close,vol FROM {val} "
               f"WHERE code=? AND date>=? ORDER BY date")
        params = [sm_code, start]
    else:
        sql = ("SELECT code,date,open,high,low,close,volume AS vol "
               "FROM daily_bars WHERE market=? AND code=? AND date>=? "
               "ORDER BY date")
        params = [val, _sm701_global_code(sm_code, mk), start]
    con = _sm701_conn(db)
    try:
        return pd.read_sql_query(sql, con, params=params)
    except sqlite3.Error:
        log.debug("sm70x 行情读取失败: %s %s", mk, db, exc_info=True)
        return pd.DataFrame()
    finally:
        con.close()


def sm701_market_pool(market):
    """该市场有足够历史的代码（sm701 代码口径）+ 名称；无库返回 []。

    兼容 sm701 原生表与 global_daily.db 统一表；名称取库内 stocks 表
    （global 库的 HK/FUT/CRYPTO 名称较全，US 多为空 → 调用方回退代码）。
    v7.4.0：最多取 3000 只（按日K根数降序），避免 global 库近万只标的
    拖慢市场切换/搜索补全；任意代码仍可手动输入。
    """
    mk = market
    cap = 3000
    try:
        paths = sm701_paths(market=mk)
    except Exception:
        return []
    db = paths.get("db")
    kind, val = _sm701_db_kind(db, mk)
    if not kind:
        return []
    if kind == "table":
        sql = (f"SELECT code, COUNT(*) c FROM {val} GROUP BY code "
               f"HAVING c>=200 ORDER BY c DESC LIMIT {cap}")
        nsql = "SELECT code,name FROM stocks WHERE name IS NOT NULL AND name<>''"
        npar = ()
    else:
        sql = ("SELECT code, COUNT(*) c FROM daily_bars WHERE market=? "
               f"GROUP BY code HAVING c>=200 ORDER BY c DESC LIMIT {cap}")
        nsql = ("SELECT code,name FROM stocks WHERE market=? "
                "AND name IS NOT NULL AND name<>''")
        npar = (val,)
    con = _sm701_conn(db)
    try:
        codes = [r[0] for r in con.execute(sql, () if kind == "table" else (val,))]
        names = dict(con.execute(nsql, npar))
    except sqlite3.Error:
        log.debug("sm70x 市场池读取失败: %s", mk, exc_info=True)
        codes, names = [], {}
    finally:
        con.close()
    return [(c, names.get(c) or c) for c in sorted(codes)]


def _sm701_dirs():
    """sm70x 候选目录：环境变量 > sm701 副本 > sm701 > 桌面其它 sm70*。"""
    home = os.path.expanduser("~")
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    env_dir = _env_first("SM70_DIR", "SM701_DIR")
    if env_dir:
        out.append(env_dir)
    for base in (
        os.path.join(home, "桌面", "sm701 (副本)"),
        os.path.join(home, "Desktop", "sm701 (副本)"),
        os.path.join(here, "sm701 (副本)"),
        os.path.join(os.path.dirname(here), "sm701 (副本)"),
        os.path.join(home, "桌面", "sm701"),
        os.path.join(home, "Desktop", "sm701"),
        os.path.join(here, "sm701"),
        os.path.join(os.path.dirname(here), "sm701"),
    ):
        if base and base not in out:
            out.append(base)
    # v7.4.0：sm 系列兼容——桌面其它 sm70* 目录（如 sm702）按版本号降序兜底
    extra = []
    for parent in (os.path.join(home, "桌面"), os.path.join(home, "Desktop")):
        try:
            extra.extend(glob.glob(os.path.join(parent, "sm70*")))
        except OSError:
            continue
    for base in sorted(set(extra), reverse=True):
        if base not in out:
            out.append(base)
    return out


def _sm701_pick_db(base, mk, env_db=""):
    """按市场选只读数据源：显式 SM70_DB > 目录内库 > 桌面全局/主库。

    兼容 sm701 原生行情表与 global_daily.db 统一表；都没有时返回 None，
    由调用方回退宿主主库（A股/带前缀的多市场行情）。
    """
    if env_db:
        return os.path.abspath(os.path.expanduser(env_db))
    for name in ("stock_cache.db", "global_daily.db"):
        db = os.path.join(base, name)
        if _sm701_db_kind(db, mk)[0]:
            return db
    home = os.path.expanduser("~")
    roots = (os.path.join(home, "桌面"), os.path.join(home, "Desktop"),
             os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for parent in roots:
        for name in ("global_daily.db", "stock_cache.db"):
            db = os.path.join(parent, name)
            if _sm701_db_kind(db, mk)[0]:
                return db
        # 浏览器重复下载常见的 `stock_cache (1).db`
        try:
            for db in sorted(glob.glob(os.path.join(parent, "stock_cache*.db"))):
                if _sm701_db_kind(db, mk)[0]:
                    return db
        except OSError:
            pass
    return None


def sm701_paths(refresh=False, market="cn"):
    """定位 sm70x 副本及模型/预测产物；优先副本，其次原目录。

    v6.6.5 多市场：models/pred 按市场（prod_{hk|us|fut}_*），按市场缓存。
    v7.4.0：数据库/历史预测可缺失——db=None 表示回退主库实时复算，
    pred="" 表示历史段留空；只要模型+特征工程脚本在，仍算“有模型”。
    """
    mk = market if market in _SM701_MK else "cn"
    cache_key = "paths_" + mk
    if not refresh and _SM701_CACHE.get(cache_key):
        return dict(_SM701_CACHE[cache_key])
    env_model = _env_first("SM70_MODEL", "SM701_MODEL")
    env_pred = _env_first("SM70_PRED", "SM701_PRED")
    env_db = _env_first("SM70_DB", "SM701_DB")
    mpre = _SM701_MK[mk]["mpre"]
    pred_file = _SM701_MK[mk]["pred"]
    for raw in _sm701_dirs():
        base = os.path.abspath(os.path.expanduser(raw))
        script = os.path.join(base, "lgbm_train_backtest.py")
        if not os.path.isfile(script):
            continue
        # SM70_MODEL 显式指定时强制单模型口径（仅 cn；多市场跳过 production 探测）
        prod = None
        if mk == "cn" and not env_model:
            prod = _sm701_production_set(base)
        elif mk != "cn":
            prod = _sm701_production_set(base, mpre)
        if mk != "cn" and not prod:
            continue            # 该市场无生产模型集
        if prod:
            model = prod["models"]["1"]
            pred_paths = [env_pred] if (env_pred and mk == "cn") else [
                prod["pred"], os.path.join(base, "lgbm_output", pred_file)]
        else:
            model = (env_model or
                     os.path.join(base, "lgbm_output", "lgbm_model.txt"))
            pred_paths = [env_pred or os.path.join(
                base, "lgbm_output", "valid_predictions.csv.gz")]
        pred = next((p for p in pred_paths
                     if p and os.path.isfile(p)), "")
        if not os.path.isfile(model):
            continue
        out = {"dir": base, "db": _sm701_pick_db(base, mk, env_db),
               "model": model, "pred": pred, "script": script,
               "prod": prod, "market": mk, "series": os.path.basename(base),
               "table": _SM701_MK[mk]["table"]}
        _SM701_CACHE[cache_key] = out
        return dict(out)
    raise RuntimeError(
        f"未找到 sm70x {mk} 市场模型集（需 production/prod_{mpre}h*_tech.txt "
        f"+ lgbm_train_backtest.py）；可用 SM70_DIR 指定目录")


def _sm701_conn(path=None):
    """只读打开 sm701/global 扩展库，避免与副本训练进程争用写锁。
    path 为空时依次取 sm701 库、宿主主库。"""
    import urllib.request as _ur
    path = path or sm701_paths().get("db") or _DB_PATH()
    uri = "file:" + _ur.pathname2url(os.path.abspath(path)) + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30)


def _sm701_py_module(path, name):
    """按绝对路径加载 sm701 目录内的 Python 模块（按路径缓存）。"""
    import importlib.util
    key = "pymod:" + os.path.abspath(path)
    cached = _SM701_CACHE.get(key)
    if cached:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 sm701 模块: " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _SM701_CACHE[key] = mod
    return mod


def _sm701_module(paths=None):
    """加载 sm701 的特征工程模块，实时预测与离线训练保持同一口径。"""
    paths = paths or sm701_paths()
    return _sm701_py_module(paths["script"], "_stock_predict_sm701_lgbm")


def _sm701_stock_info():
    """sm701 股票表（补足主库缺失的代码名/行业/PE/换手率）。"""
    cached = _SM701_CACHE.get("info")
    if cached is not None:
        return cached
    with _sm701_conn() as conn:
        rows = conn.execute(
            "select code,name,industry,pe,turnover from stocks").fetchall()
        try:
            extra_ind = dict(conn.execute(
                "select code,industry_name from stock_industry "
                "where industry_name is not null and industry_name<>''"))
        except sqlite3.Error:
            extra_ind = {}
    rows = [(c, n, ind or extra_ind.get(c, ""), pe, tu)
            for c, n, ind, pe, tu in rows]
    out = {c: (n or c, ind or "", pe, tu)
           for c, n, ind, pe, tu in rows}
    _SM701_CACHE["info"] = out
    return out


def _sm701_fill_history(F, codes, cal, path):
    """把历史样本外概率写入面板矩阵；标准库流式读取，避免整包 pandas。"""
    import csv
    import gzip
    code_pos = {c: i for i, c in enumerate(codes)}
    date_pos = {d: j for j, d in enumerate(cal)}
    loaded = 0
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fields = set(reader.fieldnames or ())
        pcol = "prob" if "prob" in fields else (
            "prob_lgbm" if "prob_lgbm" in fields else
            "prob_fuse" if "prob_fuse" in fields else "")
        if not pcol:
            raise RuntimeError("sm701 预测文件缺少 prob/prob_lgbm/prob_fuse 列")
        for row in reader:
            i = code_pos.get(row.get("code"))
            j = date_pos.get(row.get("date"))
            if i is None or j is None:
                continue
            try:
                F[i, j] = float(row[pcol])
            except (TypeError, ValueError):
                continue
            loaded += 1
    return loaded


_SM701_BOOSTERS = {}            # abspath -> ((mtime, size), Booster)
_SM701_BOOSTER_MAX = 24         # 最多常驻 24 个模型（5 市场 × 3 周期 + 余量）
_SM701_WARNED = set()


def _sm701_booster(path):
    """加载 LightGBM 模型（v7.4.0）。

    sm701 仓库在 Windows 检出时 `production/prod_*.txt` 常被 git 转成
    CRLF，LightGBM 文本解析器会报 `Model format error, expect a tree here`；
    这里统一规范化为 LF 再用 model_str 加载（失败才回落 model_file）。
    按 (mtime, size) 缓存 Booster——原实现每次实时复算都重读 3 个
    5~17MB 模型文件。
    """
    import lightgbm as lgb
    key = os.path.abspath(path)
    try:
        st = os.stat(key)
    except OSError as exc:
        raise RuntimeError(f"sm701 模型不可读: {key}") from exc
    sig = (st.st_mtime, st.st_size)
    hit = _SM701_BOOSTERS.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    with open(key, "rb") as f:
        raw = f.read()
    norm = raw.replace(b"\r\n", b"\n")
    try:
        booster = lgb.Booster(model_str=norm.decode("utf-8", "replace"))
    except Exception:
        if key not in _SM701_WARNED:
            _SM701_WARNED.add(key)
            log.warning("sm701 模型 LF 规范化加载失败，回落 model_file: %s",
                        key, exc_info=True)
        booster = lgb.Booster(model_file=key)
    if len(_SM701_BOOSTERS) >= _SM701_BOOSTER_MAX:
        _SM701_BOOSTERS.pop(next(iter(_SM701_BOOSTERS)), None)
    _SM701_BOOSTERS[key] = (sig, booster)
    return booster


def _sm701_model_files(paths):
    prod = (paths or {}).get("prod")
    if prod:
        return [prod["models"][h] for h in sorted(prod["models"], key=int)]
    return [(paths or {}).get("model")]


def _sm701_model_features(paths):
    """模型集所需特征名并集（用于判断加密分时特征是否要补）。"""
    out = set()
    for p in _sm701_model_files(paths):
        if p:
            out.update(_sm701_booster(p).feature_name())
    return out


def _sm701_predict_feats(feats, paths):
    """按 sm701 模型对特征矩阵打分。

    生产档（`paths["prod"]`）为 h1/h5/h10 三模型概率等权平均；否则单模型
    `paths["model"]`。每个模型按**自己的 feature_name** 选列（v7.4.0：
    crypto h10 = tech40 + 11 个分时特征）；某模型所需列缺失时跳过该模型
    并告警，剩余模型继续等权（如无 crypto_hourly 时 crypto 只跑 h1/h5）。
    """
    import pandas as pd
    acc = None
    used = 0
    for path in _sm701_model_files(paths):
        if not path:
            continue
        booster = _sm701_booster(path)
        names = booster.feature_name()
        missing = [k for k in names if k not in feats]
        if missing:
            tag = (path, tuple(missing))
            if tag not in _SM701_WARNED:
                _SM701_WARNED.add(tag)
                log.warning("sm701 模型缺少特征，已跳过: %s（缺 %s）",
                            os.path.basename(path), ",".join(missing[:6]))
            continue
        X = pd.DataFrame({k: np.asarray(feats[k], dtype=np.float32)
                          for k in names}, dtype=np.float32)
        pv = booster.predict(X)
        acc = pv if acc is None else acc + pv
        used += 1
    if not used or acc is None:
        raise RuntimeError("sm701 模型集无可用模型（特征不齐）")
    if used < len([p for p in _sm701_model_files(paths) if p]):
        log.debug("sm701 概率按 %d/%d 个模型等权", used,
                  len([p for p in _sm701_model_files(paths) if p]))
    return acc / float(used)


def _sm701_intraday_feats(df, paths):
    """crypto h10 的 11 个分时特征（v7.4.0）。

    口径与 sm701 `train_crypto_intraday.build_intraday` 完全一致：读
    `crypto_hourly` 最近 60 天，锚定当日 23:00 bar；无库/无表/数据不足
    返回 {}（调用方随后按缺特征跳过 h10）。
    """
    import pandas as pd
    db = (paths or {}).get("db")
    if not db or not len(df) or not _sm701_has_table(db, "crypto_hourly"):
        return {}
    sm_code = str(df["code"].astype(str).iloc[0])
    last = str(df["date"].astype(str).iloc[-1])
    start = (pd.Timestamp(last) - pd.Timedelta(days=60)
             ).strftime("%Y-%m-%d 00:00")
    try:
        con = _sm701_conn(db)
        try:
            hdf = pd.read_sql_query(
                "SELECT code,ts,open,high,low,close,vol FROM crypto_hourly "
                "WHERE code=? AND ts>=? ORDER BY code,ts", con,
                params=[sm_code, start])
        finally:
            con.close()
    except sqlite3.Error:
        log.debug("crypto_hourly 读取失败(忽略)", exc_info=True)
        return {}
    if len(hdf) < 24:
        return {}
    hdf["code"] = hdf["code"].astype(str)
    hdf["hour"] = hdf["ts"].str[11:13].astype(int)
    hdf["date"] = hdf["ts"].str[:10]
    script = os.path.join(paths["dir"], "train_crypto_intraday.py")
    if not os.path.isfile(script):
        return {}
    try:
        mod = _sm701_py_module(script, "_stock_predict_sm701_intraday")
        idf = mod.build_intraday(hdf)[["code", "date"] + list(mod.ID_ALL)]
    except Exception:
        log.warning("crypto 分时特征计算失败(忽略，h10 跳过)", exc_info=True)
        return {}
    keys = pd.DataFrame({"code": df["code"].astype(str).values,
                         "date": df["date"].astype(str).values})
    idm = keys.merge(idf, on=["code", "date"], how="left")
    return {k: idm[k].to_numpy() for k in mod.ID_ALL}


_DB_LATEST_CACHE = {}           # (path, table) -> (ts, date)；MAX(date) 无
                                # date 索引，主库/副本全表扫 0.6~2s，60s
                                # 缓存供 sm701 缓存 key 等高频调用方复用

# 主库 daily_bars 按项目代码前缀区分市场（v7.5.2：市场各自取最新日，
# 避免加密周末推进把 A 股“最新日”顶高、或反之）。
_MAIN_CODE_PREFIX = {"cn": ("sh", "sz", "bj"), "hk": ("hk",),
                     "us": ("us",), "futures": ("fu",),
                     "crypto": ("crypto",)}


def _sm701_utc_today():
    """UTC 自然日（YYYY-MM-DD）——crypto 24/7 日K/盘中缓存口径。"""
    return time.strftime("%Y-%m-%d", time.gmtime())


def _main_latest_market(market):
    """主库该市场最新日K日期（按代码前缀，60s 缓存；失败返回空串）。"""
    prefixes = _MAIN_CODE_PREFIX.get(market)
    key = ("main-mk", market)
    hit = _DB_LATEST_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < 60:
        return hit[1]
    try:
        with _db_conn() as conn:
            if prefixes:
                # 前缀区间走 (code,date) 索引；比 LIKE/OR 全表扫快一个量级
                # （cn 三前缀 ~1.1s，crypto 仅 ~3ms）。
                d = max(
                    conn.execute(
                        "select max(date) from daily_bars "
                        "where code>=? and code<?",
                        (p, p[:-1] + chr(ord(p[-1]) + 1))
                    ).fetchone()[0] or ""
                    for p in prefixes)
            else:
                d = conn.execute(
                    "select max(date) from daily_bars").fetchone()[0] or ""
    except sqlite3.Error:
        return ""
    _DB_LATEST_CACHE[key] = (now, d)
    return d


def _db_latest_date(path=None, table="daily_bars"):
    """行情库最新交易日；path 为空时读主库。失败返回空串。

    v6.6.5：加 60s 进程内缓存——本函数被 sm701 缓存 key 高频调用，
    全表 MAX(date) 在主库/副本上每次 0.6~2s；数据推进频率为日级，
    60s 缓存安全。v7.4.0：table 可按市场取（crypto_daily 等）。
    """
    try:
        key = (os.path.abspath(path) if path else "main", table)
        hit = _DB_LATEST_CACHE.get(key)
        now = time.time()
        if hit and now - hit[0] < 60:
            return hit[1]
        if path:
            import urllib.request as _ur
            uri = ("file:" + _ur.pathname2url(os.path.abspath(path))
                   + "?mode=ro")
            conn = sqlite3.connect(uri, uri=True, timeout=30)
            try:
                d = conn.execute(
                    f"select max(date) from {table}").fetchone()[0] or ""
            finally:
                conn.close()
        else:
            with _db_conn() as conn:
                d = conn.execute(
                    f"select max(date) from {table}").fetchone()[0] or ""
        _DB_LATEST_CACHE[key] = (now, d)
        return d
    except sqlite3.Error:
        return ""


def _sm701_cache_epoch(src_max, market="cn"):
    """当日数据的缓存桶：src_max 为市场“今天”时按 30 分钟分桶（盘中数据
    会变），历史日期恒为 0（收盘定型，稳定缓存）。

    v7.5.2：crypto 按 UTC 自然日判断（24/7）——原实现用本机日期，
    北京时区 00:00~08:00 时 UTC 当日 < 本机“明天”，盘中数据被永久
    缓存；08:00~24:00 又可能把 UTC 前一日当历史。其余市场仍用本机日。
    配合 key 用数据指纹替代库文件 mtime——预取/回填写库改变 mtime 但
    数据未推进，不应触发 sm701 全市场重算（v6.6.5 修复：曾致 sm701
    股票每次分析卡 100s+）。"""
    today = _sm701_utc_today() if market == "crypto" else time.strftime(
        "%Y-%m-%d")
    if src_max and src_max >= today:
        return int(time.time() // 1800)
    return 0


def _sm701_live_predict(target_dates, cal, progress=None):
    """用副本 LightGBM 模型实时复算目标日期全市场概率。

    返回 {(code, date): prob}；cal 为交易日日历（用于取 180 根预热起点）。
    结果按（预热起点 + 目标日期集合 + 取数库最新日K日期/当日30分钟桶/模型
    mtime）缓存，组合矩阵与单股信号两条链共用同一次复算。
    v6.6.4：sm701 副本日K落后主库时改用主库最新行情复算——两库日K同源
    （重叠段逐值一致），否则目标日停在副本末日会让荐股整页为空。
    v6.6.5：缓存 key 由「取数库 mtime」改为「取数库最新日K日期 +
    当日 30 分钟桶」——mtime 在每次预取/回填写库后必变，而数据未推进
    时复算结果不变，曾导致 sm701 股票每次分析都触发 100s+ 全市场复算
    （主图长时间空白）。"""
    import pandas as pd
    if np is None:
        raise RuntimeError("外接算法实时评分需要 numpy")
    paths = sm701_paths()
    date_pos = {d: j for j, d in enumerate(cal)}
    wanted = sorted({d for d in target_dates if d in date_pos})
    if not wanted:
        return {}
    first = min(date_pos[d] for d in wanted)
    start_i = max(0, first - (_SM701_WARMUP_BARS - 1))
    start = cal[start_i]
    db = paths.get("db")
    sm_max = _db_latest_date(db) if db else ""
    src_db, src_max = (db or _DB_PATH()), sm_max
    main_max = _main_latest_market("cn")
    if not db or main_max > sm_max:
        src_db, src_max = _DB_PATH(), main_max
    use_main = os.path.abspath(src_db) == os.path.abspath(_DB_PATH())
    key = (start, tuple(wanted), os.path.abspath(src_db), src_max,
           _sm701_cache_epoch(src_max), _sm701_model_mtimes(paths))
    cached = _SM701_CACHE.get("live")
    if cached and cached[0] == key:
        return cached[1]

    def _qual(col):
        return (f"({col} GLOB 'sh60[0135]*' OR {col} GLOB 'sh68[89]*' "
                f"OR {col} GLOB 'sz00[0123]*' OR {col} GLOB 'sz30[01]*' "
                f"OR {col} GLOB 'bj[489]*')")

    if progress:
        progress(f"sm701：复算 {wanted[0]} ~ {wanted[-1]} 概率"
                 f"（数据至 {src_max or '-'}）...")
    if use_main:
        sql = (f"SELECT code,date,open,high,low,close,vol FROM daily_bars "
               f"WHERE date>=? AND {_qual('code')} ORDER BY code,date")
        with _db_conn() as conn:
            df = pd.read_sql_query(sql, conn, params=[start])
    else:
        sql = (f"SELECT b.code,b.date,b.open,b.high,b.low,b.close,b.vol "
               f"FROM daily_bars b JOIN stocks s ON s.code=b.code "
               f"WHERE b.date>=? AND {_qual('s.code')} ORDER BY b.code,b.date")
        with _sm701_conn(src_db) as conn:
            df = pd.read_sql_query(sql, conn, params=[start])
    if df.empty:
        raise RuntimeError("sm701/主库均无可用日K")
    df["code"] = df["code"].astype("category")
    feats = _sm701_module(paths)._compute_feats(df)
    prob = _sm701_predict_feats(feats, paths)
    code_arr = df["code"].astype(str).to_numpy()
    date_arr = df["date"].to_numpy()
    wanted_set = set(wanted)
    out = {}
    for code, date, pv in zip(code_arr, date_arr, prob):
        if date not in wanted_set:
            continue
        out[(str(code), str(date))] = float(pv)
    _SM701_CACHE["live"] = (key, out)
    return out


def _sm701_live_fill(F, codes, cal, target_dates, progress=None):
    """实时概率写入面板矩阵（sm701_score_matrix 用）。"""
    preds = _sm701_live_predict(target_dates, cal, progress=progress)
    code_pos = {c: i for i, c in enumerate(codes)}
    date_pos = {d: j for j, d in enumerate(cal)}
    loaded = 0
    for (code, date), pv in preds.items():
        i = code_pos.get(code)
        j = date_pos.get(date)
        if i is None or j is None:
            continue
        F[i, j] = pv
        loaded += 1
    return loaded


def sm701_score_matrix(progress=None):
    """返回与 tier_load_panel 对齐的 sm701 概率矩阵（代码 × 交易日）。"""
    if np is None:
        raise RuntimeError("外接算法需要 numpy")
    codes, cal, _, _ = tier_load_panel()
    paths = sm701_paths()
    try:
        pred_mt = os.path.getmtime(paths["pred"])
    except OSError:
        pred_mt = 0.0
    db = paths.get("db")
    try:
        db_mt = os.path.getmtime(db) if db else 0.0
    except OSError:
        db_mt = 0.0
    main_max = _main_latest_market("cn")
    key = (tuple(codes[:3]), len(codes), cal[0], cal[-1], db_mt,
           _sm701_model_mtimes(paths), pred_mt, main_max,
           _sm701_cache_epoch(main_max))
    cached = _SM701_CACHE.get("scores")
    if cached and cached[0] == key:
        return cached[1]
    with _SM701_LOCK:
        cached = _SM701_CACHE.get("scores")
        if cached and cached[0] == key:
            return cached[1]
        F = np.full((len(codes), len(cal)), np.nan, np.float32)
        hist_max = ""
        if os.path.isfile(paths["pred"]):
            n = _sm701_fill_history(F, codes, cal, paths["pred"])
            if n:
                hist_max = max(cal[j] for j in range(len(cal))
                               if np.isfinite(F[:, j]).any())
        missing = ([d for d in cal if d > hist_max] if hist_max else
                   list(cal[-_SM701_WARMUP_BARS:]))
        if missing:
            try:
                _sm701_live_fill(F, codes, cal, missing, progress=progress)
            except Exception:
                log.exception("sm701 实时概率复算失败")
                if not np.isfinite(F).any():
                    raise
        _SM701_CACHE["scores"] = (key, F)
    return F


def _sm_series_name(base):
    """目录名 → sm70x 系列名（`sm701 (副本)`→sm701；非 sm70N 返回空串）。"""
    name = os.path.basename(os.path.abspath(base))
    if name.endswith(" (副本)"):
        name = name[:-len(" (副本)")]
    return name if name.startswith("sm70") and name[4:].isdigit() else ""


def sm70x_series():
    """已连接的 sm70x 系列目录名（sm701/sm702/…），供关于页署名。

    v7.5.2：只返回真实存在且带模型/数据入口的目录——开源仓库没有
    sm70x 目录时返回空列表，关于页不显示任何外接算法标识/品牌图。
    自定义 SM70_DIR 只要含 `lgbm_train_backtest.py` + production 也计入。
    """
    hit = _SM701_CACHE.get("series")
    now = time.time()
    if hit and now - hit[0] < 60:
        return list(hit[1])
    out = []
    env_dir = _env_first("SM70_DIR", "SM701_DIR")
    env_abs = (os.path.abspath(os.path.expanduser(env_dir))
               if env_dir else "")
    for raw in _sm701_dirs():
        base = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(base):
            continue
        name = _sm_series_name(base)
        # 只认 sm70N 目录或显式 SM70_DIR，避免把 sm702_export_* 等备份
        # 目录当成“已连接系列”。
        if not name and base != env_abs:
            continue
        has_entry = any(os.path.exists(os.path.join(base, f)) for f in (
            "lgbm_train_backtest.py", "production", "features.py",
            "stock_cache.db", "global_daily.db", "crypto_daily.db",
            "crypto_hourly.db"))
        if not has_entry:
            continue
        label = name or os.path.basename(base)
        if label.lower() not in (x.lower() for x in out):
            out.append(label)
    out.sort()
    _SM701_CACHE["series"] = (now, out)
    return list(out)


def sm701_status(market="cn"):
    """sm70x 外接算法数据/模型状态，供报告和排障展示。

    v7.4.0：库/历史预测可以缺失（新克隆只有代码+模型）——`db_fallback`
    表示回退主库行情、`pred_ok=False` 表示全量实时复算；并标注各市场
    是否具备生产模型集。
    v7.5.2：`live_db_date` 同时比较 sm70x 库与主库该市场前缀的最新日
    （crypto 按 UTC 口径另由行情层处理）；`series_all` 供关于页署名。
    """
    try:
        p = sm701_paths(market=market)
        db = p.get("db")
        db_date = _sm701_market_latest(db, market) if db else ""
        main_date = _main_latest_market(market)
        live_db_date = max(db_date, main_date)
        out = {"ok": True, "dir": p["dir"], "series": p.get("series", ""),
               "series_all": sm70x_series(),
               "market": p["market"], "db": db or "",
               "db_fallback": not db, "db_date": db_date,
               "main_date": main_date, "live_db_date": live_db_date,
               "model": p["model"], "pred": p["pred"],
               "pred_ok": bool(p.get("pred"))}
        prod = p.get("prod")
        if prod:
            out["prod_dir"] = prod["dir"]
            out["prod_models"] = {h: os.path.basename(v)
                                  for h, v in prod["models"].items()}
            out["prod_n_feat"] = len(prod["features"])
        out["markets"] = {mk: sm701_has_model(mk) for mk in _SM701_MK}
        return out
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# 外接算法信号阈值：模型概率滞回映射
# （v6.4.1：不消融，直接用 sm701 概率；≥0.55 买入、<0.50 卖出）
_SM701_SIG_BUY = 0.55
_SM701_SIG_SELL = 0.50
_SM701_LIVE_MAX = 250        # 无历史预测文件时，单股最多实时复算最近 N 个交易日


def _sm701_live_one(full, dates, progress=None):
    """多市场单股实时复算：优先读**副本库同源行情表**（hk_daily/us_daily/
    fut_daily/crypto_daily——特征与训练同源同口径），取 ≥180 根预热 +
    目标日段，三周期模型等权。

    v7.4.0：库/表缺失时回退主库同前缀行情（`cryptobtcusdt` 等），不再因
    新克隆没有 stock_cache.db 而整市场不可用；crypto 有 crypto_hourly 时
    补 11 个分时特征（h10 用），没有则 h10 自动跳过、h1/h5 等权。
    副本库缺最新日时用主库补（重叠段副本优先）。缓存按（full, dates,
    数据源最新日/模型 mtime）。"""
    import pandas as pd
    mk = _market_of(full)
    paths = sm701_paths(market=mk)
    sm_code = _sm701_code(full)
    dates_u = list(dict.fromkeys(d for d in dates if d))
    if not dates_u:
        return {}
    db = paths.get("db")
    sm_max = _sm701_market_latest(db, mk) if db else ""
    main_max = _main_latest_market(mk)
    src_max = max(sm_max, main_max)
    key = (full, tuple(dates_u), os.path.abspath(db) if db else "main",
           src_max, _sm701_cache_epoch(src_max, mk),
           _sm701_model_mtimes(paths))
    cache = _SM701_CACHE.setdefault("live_one", {})
    hit = cache.get(key)
    if hit is not None:
        return dict(hit)
    df = _sm701_read_bars(db, mk, sm_code) if db else pd.DataFrame()
    # 主库补最新日/兜底（副本滞后时；重叠日以副本为准）
    try:
        with _db_conn() as conn:
            extra = pd.read_sql_query(
                "SELECT code,date,open,high,low,close,vol FROM daily_bars "
                "WHERE code=? ORDER BY date", conn, params=[full])
        if not extra.empty:
            if df.empty:
                df = extra
            else:
                have = set(df["date"])
                add = extra[~extra["date"].isin(have)]
                if not add.empty:
                    df = pd.concat([df, add], ignore_index=True)
    except Exception:
        log.debug("主库补最新行情失败(忽略)", exc_info=True)
    if df.empty:
        raise RuntimeError(f"sm701/主库均无 {full}（{sm_code}）行情")
    df = df.sort_values("date").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "vol"):
        df[c] = df[c].astype("float64")
    # 统一成 sm701 代码口径：主库补进来的行若用项目前缀（cryptobtcusdt），
    # 会被 groupby 当成第二只股票、切断特征历史（v7.4.0 修复）。
    df["code"] = pd.Categorical([sm_code] * len(df))
    mod = _sm701_module(paths)
    feats = mod._compute_feats(df)
    if mk == "crypto" and (_sm701_model_features(paths) - set(feats)):
        feats.update(_sm701_intraday_feats(df, paths))
    date_arr = [str(d) for d in df["date"]]
    pos = {d: i for i, d in enumerate(date_arr)}
    want_idx = [i for d in dates_u if (i := pos.get(d)) is not None]
    if not want_idx:
        return {}
    need_from = min(want_idx) - (_SM701_WARMUP_BARS - 1)
    if need_from < 0:
        need_from = 0
    sub = df.iloc[need_from:].reset_index(drop=True)
    sub_feats = {k: v[need_from:] for k, v in feats.items()}
    probs = _sm701_predict_feats(sub_feats, paths)
    sub_dates = [str(d) for d in sub["date"]]
    pmap = {d: float(p) for d, p in zip(sub_dates, probs) if p == p}
    out = {d: pmap[d] for d in dates_u if d in pmap}
    cache[key] = dict(out)
    return out


def _sm701_cn_matrix_probs(full):
    """A股无历史预测文件时的整段概率：复用 sm701_score_matrix 的一次
    全市场复算（与荐股/消融共用缓存），避免单股分析各自触发全市场重算。"""
    if np is None or not _HOST or not _HOST.get("tier_load_panel"):
        return {}
    try:
        codes, cal, _, _ = tier_load_panel()
    except Exception:
        log.debug("tier_load_panel 不可用，跳过矩阵复用", exc_info=True)
        return {}
    try:
        i = list(codes).index(full)
    except ValueError:
        return {}
    try:
        F = sm701_score_matrix()
    except Exception:
        log.debug("sm701 全市场矩阵不可用", exc_info=True)
        return {}
    return {d: float(F[i, j]) for j, d in enumerate(cal)
            if np.isfinite(F[i, j])}


def _sm701_crypto_hourly_map(paths, sm_code):
    """本机 quant 加密小时模型日聚合概率 {date: prob_hourly}（v7.5.6）。

    文件由本机 `sm701/output/crypto_hourly_daily_probs.csv.gz` 提供（quant
    机模型 + crypto_hourly.db 离线生成，仅本机使用，不入库/不上传）。缺失
    或代码不在文件中时返回 {}，调用方回落日线模型。
    """
    base = (paths or {}).get("dir")
    if not base:
        return {}
    path = os.path.join(base, "output", _SM701_CRYPTO_HOURLY)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    key = (path, mt)
    cache = _SM701_CACHE.setdefault("crypto_hourly_map", {})
    hit = cache.get(sm_code)
    if hit and hit[0] == key:
        return dict(hit[1])
    out = {}
    try:
        import csv
        import gzip
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            rd = csv.reader(f)
            head = next(rd, [])
            try:
                di = head.index("date")
                ci = head.index("code")
                pi = next(head.index(c) for c in
                          ("prob_hourly", "prob", "prob_avg")
                          if c in head)
            except (StopIteration, ValueError):
                di = ci = pi = -1
            if pi >= 0:
                need = max(di, ci, pi)
                for row in rd:
                    if len(row) <= need or row[ci] != sm_code:
                        continue
                    try:
                        out[row[di]] = float(row[pi])
                    except ValueError:
                        continue
    except Exception:
        log.debug("读取 crypto_hourly_daily_probs 失败(忽略)", exc_info=True)
        return {}
    cache[sm_code] = (key, dict(out))
    return out


def sm701_prob_map(full, dates=None):
    """单股 sm701 概率序列 {date: prob}：历史落盘 + 新交易日实时复算。

    历史段流式读对应市场 valid_predictions[_市场].csv.gz（只留该股，按文件
    mtime 缓存）；dates 中晚于历史末日的交易日走实时复算——cn 全市场口径
    （_sm701_live_predict），hk/us/fut/crypto 单股口径（_sm701_live_one）。
    v7.4.0：历史预测文件缺失时改走实时复算——A股复用 sm701_score_matrix
    的一次全市场矩阵，非 cn 最多复算最近 250 个交易日（不再返回空）。"""
    if np is None:
        return {}
    mk = _market_of(full)
    if mk not in _SM701_MK:
        return {}
    try:
        paths = sm701_paths(market=mk)
    except Exception:
        return {}
    sm_code = _sm701_code(full)
    try:
        pred_mt = os.path.getmtime(paths["pred"])
    except OSError:
        pred_mt = 0.0
    hourly_mt = 0.0
    if mk == "crypto":
        try:
            hourly_mt = os.path.getmtime(os.path.join(
                paths.get("dir") or "", "output", _SM701_CRYPTO_HOURLY))
        except OSError:
            pass
    src_max = max((_sm701_market_latest(paths.get("db"), mk)
                   if paths.get("db") else ""), _main_latest_market(mk))
    key = (pred_mt, _sm701_model_mtimes(paths), src_max,
           _sm701_cache_epoch(src_max, mk), hourly_mt)
    cache = _SM701_CACHE.setdefault("probmap", {})
    hit = cache.get(full)
    if hit and hit[0] == key:
        out = dict(hit[1])
    else:
        import csv
        import gzip
        out = {}
        if os.path.isfile(paths["pred"]):
            with gzip.open(paths["pred"], "rt", encoding="utf-8",
                           newline="") as f:
                rd = csv.reader(f)
                head = next(rd, [])
                try:
                    ci, di = head.index("code"), head.index("date")
                    pi = next(head.index(c) for c in
                              ("prob", "prob_lgbm", "prob_fuse", "prob_avg")
                              if c in head)
                except (StopIteration, ValueError):
                    ci = di = pi = -1
                if pi >= 0:
                    need = max(ci, di, pi)
                    for row in rd:
                        if len(row) <= need or row[ci] != sm_code:
                            continue
                        try:
                            out[row[di]] = float(row[pi])
                        except ValueError:
                            continue
        if mk == "crypto":
            hourly = _sm701_crypto_hourly_map(paths, sm_code)
            if hourly:
                out = dict(hourly)
        cache[full] = (key, dict(out))
    if dates:
        uniq = list(dict.fromkeys(d for d in dates if d))
        hist_max = max(out) if out else ""
        if hist_max:
            missing = [d for d in uniq if d > hist_max]
        else:
            # 无历史预测文件：只复算最近窗口，避免把整段历史拖进
            # 全市场实时复算（A股一次会读上百万行，旧实现会卡数十秒）
            missing = sorted(uniq)[-_SM701_LIVE_MAX:]
        if missing:
            try:
                if mk == "cn":
                    got = {} if hist_max else _sm701_cn_matrix_probs(full)
                    if got:
                        out.update(got)
                    else:
                        # 兜底：只复算最近 5 个交易日，避免把整段历史拖进
                        # 全市场实时复算（A股一次会读上百万行）
                        preds = _sm701_live_predict(
                            sorted(missing)[-5:], uniq)
                        out.update({d: pv for (code, d), pv in preds.items()
                                    if code == full and pv is not None})
                else:
                    out.update(_sm701_live_one(full, missing))
            except Exception:
                log.exception("sm701 单股实时概率复算失败")
    return out


def _sig_sm701_from_probs(rows, probs, tag="sm701",
                          buy_th=None, sell_th=None):
    """sm701 概率序列 → 买卖点（probs 可为 {date: prob} 或按行对齐的序列）。

    与 `_sig_sm701` 同一滞回口径，供研究消融在父进程一次性预载概率后
    直接生成信号，避免每个 worker 重复扫描 70MB+ 预测文件。
    v7.5.11：buy_th/sell_th 可由设置内调（默认 0.55/0.50）。"""
    if not rows or probs is None:
        return []
    try:
        buy_th = _SM701_SIG_BUY if buy_th is None else float(buy_th)
        sell_th = _SM701_SIG_SELL if sell_th is None else float(sell_th)
    except (TypeError, ValueError):
        buy_th, sell_th = _SM701_SIG_BUY, _SM701_SIG_SELL
    if sell_th >= buy_th:
        sell_th = max(0.01, buy_th - 0.01)
    seq = isinstance(probs, dict)
    out = []
    state = 0
    for i, r in enumerate(rows):
        p = probs.get(r.get("date")) if seq else (
            probs[i] if i < len(probs) else None)
        if p is None:
            continue
        if state <= 0 and p >= buy_th:
            out.append((i, r["date"], "BUY",
                        f"{tag}概率{p:.2f}≥{buy_th:.2f}"))
            state = 1
        elif state >= 1 and p < sell_th:
            out.append((i, r["date"], "SELL",
                        f"{tag}概率{p:.2f}<{sell_th:.2f}"))
            state = 0
    return out


def _sig_sm701(rows, full, **_kw):
    """sm701 概率信号（外接算法直出，A股消融候选亦用此口径）：
    概率 ≥0.55 触发 BUY、<0.50 触发 SELL（滞回），同一方向不重复。

    v7.5.10：MA60 趋势闸门移到宿主侧（`_crypto_trend_filter`）在**信号
    压缩之后**执行——若在此处先丢 BUY，连续 SELL 会被 `_dedup_signals`
    合并成一条很久以前的信号，图上近期买卖点全部消失。"""
    if not rows or not full:
        return []
    try:
        probs = sm701_prob_map(full, [r.get("date") for r in rows])
    except Exception:
        log.exception("sm701 单股概率读取失败")
        return []
    mk = _market_of(full)
    tag = "sm701小时" if mk == "crypto" else "sm701"
    cfg = (_HOST or {}).get("CFG")
    buy_th = getattr(cfg, "SM701_SIG_BUY", _SM701_SIG_BUY)
    sell_th = getattr(cfg, "SM701_SIG_SELL", _SM701_SIG_SELL)
    return _sig_sm701_from_probs(rows, probs, tag=tag,
                                 buy_th=buy_th, sell_th=sell_th)


# ---- sm702 加密止损/否决模型（v7.5.11）----
# 只读 sm702 的输出：outputs_crypto/predictions_wf.csv.gz（历史 walk-forward）
# 与 latest_signals.csv（最新一日）。用作 crypto BUY 的否决层与仓位倍数，
# 模型/数据仍在仓库外，不上传。
_SM702_CACHE = {}
_SM702_LOCK = threading.RLock()


def sm702_dir():
    """sm702 根目录（SM702_DIR 优先；桌面副本/原目录兜底）。"""
    cands = [(os.environ.get("SM702_DIR") or "").strip(),
             os.path.join(os.path.expanduser("~"), "桌面", "sm702"),
             os.path.join(os.path.expanduser("~"), "sm702")]
    for p in cands:
        if p and os.path.isdir(p):
            return p
    return ""


def _sm702_read(path, code):
    op = gzip.open if path.endswith(".gz") else open
    out = {}
    try:
        with op(path, "rt", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if str(row.get("code") or "") != code:
                    continue
                d = str(row.get("date") or "")[:10]
                if not d:
                    continue
                try:
                    prob = float(row.get("prob"))
                except (TypeError, ValueError):
                    continue
                rank = row.get("prob_rank")
                try:
                    rank = float(rank) if rank not in (None, "") else None
                except (TypeError, ValueError):
                    rank = None
                try:
                    th = float(row.get("threshold"))
                except (TypeError, ValueError):
                    th = None
                sig = str(row.get("signal") or "").strip()
                out[d] = {"prob": prob, "rank": rank, "threshold": th,
                          "signal": sig in ("1", "1.0", "True", "true")}
    except Exception:
        log.debug("sm702 读取失败 %s", path, exc_info=True)
    return out


def sm702_pred_map(full):
    """crypto 代码 → {date: {prob, rank, threshold, signal}}；无模型返回 {}。"""
    if not full or _market_of(full) != "crypto":
        return {}
    base = sm702_dir()
    if not base:
        return {}
    code = _sm701_code(full)
    p1 = os.path.join(base, "outputs_crypto", "predictions_wf.csv.gz")
    p2 = os.path.join(base, "outputs_crypto", "latest_signals.csv")
    key = (code, os.path.getmtime(p1) if os.path.exists(p1) else 0,
           os.path.getmtime(p2) if os.path.exists(p2) else 0)
    with _SM702_LOCK:
        hit = _SM702_CACHE.get("map")
        if hit and hit[0] == key:
            return dict(hit[1])
        out = {}
        for p in (p1, p2):
            if not os.path.exists(p):
                continue
            got = _sm702_read(p, code)
            for d, rec in got.items():
                out.setdefault(d, rec)     # 历史优先；latest 只补缺日
        _SM702_CACHE["map"] = (key, dict(out))
        return out


def sm702_mult(rec, typ="BUY", mult_scale=10.0, mult_max=3.0,
               min_rate=0.55):
    """sm702 概率 → 仓位倍数（quant crypto_position_adapter 口径）。

    多头胜率=1-prob、空头胜率=prob；胜率低于 min_rate 返回 0（否决）。"""
    if not rec:
        return 1.0
    try:
        p = float(rec.get("prob"))
    except (TypeError, ValueError):
        return 1.0
    win = (1.0 - p) if typ != "SELL" else p
    try:
        min_rate = float(min_rate)
        if win < min_rate:
            return 0.0
        m = (win - 0.5) * float(mult_scale)
        return max(0.0, min(float(mult_max), m))
    except (TypeError, ValueError):
        return 1.0


def sm702_veto(rows, i, full, q=0.30, persist=1):
    """crypto BUY 是否被 sm702 否决（连续 persist 日处于止损概率 top-q）。"""
    mp = sm702_pred_map(full)
    if not mp or not rows or i < 0:
        return False
    try:
        q = max(0.0, min(1.0, float(q)))
        persist = max(1, int(persist))
    except (TypeError, ValueError):
        q, persist = 0.30, 1
    for k in range(persist):
        j = i - k
        if j < 0:
            return False
        rec = mp.get(rows[j].get("date"))
        if not rec:
            return False
        rank = rec.get("rank")
        if rank is not None:
            top = rank >= 1.0 - q
        else:
            th = rec.get("threshold")
            top = bool(rec.get("signal")) or (
                th is not None and rec.get("prob", 0.0) >= th)
        if not top:
            return False
    return True


# ---- 外接算法开关辅助（该市场是否有 sm701 生产模型集）----
def sm701_has_model(market, _cache={}):
    """该市场是否有 sm701 生产模型集（结果缓存；crypto 恒 False）。"""
    if market not in _SM701_MK:
        return False
    if market not in _cache:
        try:
            sm701_paths(market=market)
            _cache[market] = True
        except Exception:
            _cache[market] = False
    return _cache[market]


# ---- 外接算法风险参数（多市场直出档，v7.3.0 接替已删除的 test 档参数）----
# 买卖点来自模型概率；本组参数只作用于 ATR 止损/移动止盈（沿用原
# test 档=原激进的宽松风控）。
EXT_RISK = {"atr_mult": 2.5, "trail_trigger": 1.05,
            "trail_ratio": 0.90, "buy_th": 1, "cooldown": 1,
            "sell_cooldown": 1}

