#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""external/sm701_ext.py - sm701 外接算法适配层（v7.3.0 自 stock_gui.py 隔离）

sm701（桌面算法库）作为**外接算法**接入本项目：模型定位 / 副本库只读连接 /
特征工程加载 / 概率复算（全市场 + 单股）/ 买卖点信号与状态。本模块是唯一
入口；sm701 目录（数据 + 模型，约 7GB）保存在仓库外（默认 ~/桌面/sm701，
副本优先），git 不分发；适配层代码随主库上传，换机器装好 sm701 或用
SM701_DIR 指定路径即可复用。

接入约定（v7.3.0）:
  ini [predict] sm701_ext=1       外接算法接入开关（默认开）。关 → hk/us/fut
                                  只看行情不出买卖点、A股消融剔除 sm701 候选。
  ini [predict] sm701_ablation=1  sm701 参加 A股单股策略消融候选（默认开；
                                  关 → 仅多市场直出/独立使用，A股消融不跑它）。
  market: cn（A股消融候选/单市场直出）/ hk / us / futures；crypto 无生产
  模型，调用方恒回退多维评分。

宿主注入：init_host(gui_globals) 由 stock_gui 调用，把 np / db_conn /
DB_PATH / _market_of / tier_load_panel 以名称惰性解析传入；本模块**不
import stock_gui**，避免双向依赖。build_cli 生成的 CLI 同样可用
（需 external/ 与主文件同目录）。
"""
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
}
# crypto 无生产模型：信号回退多维评分并标注（见 analyze 市场分支）


def _sm701_code(full):
    """本项目前缀代码 → sm701 预测文件/副本库 code 形态：
    hk00700→'00700'、usaapl→'AAPL'、fua0→'A0'；cn 原样。"""
    mk = _market_of(full)
    if mk == "hk":
        return full[2:].zfill(5)
    if mk == "us":
        return full[2:].upper()
    if mk == "futures":
        return full[2:].upper()
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


def sm701_paths(refresh=False, market="cn"):
    """定位 sm701 副本及模型/预测产物；优先副本，其次原目录。
    v6.6.5 多市场：models/pred 按市场（prod_{hk|us|fut}_*），按市场缓存。"""
    mk = market if market in _SM701_MK else "cn"
    cache_key = "paths_" + mk
    if not refresh and _SM701_CACHE.get(cache_key):
        return dict(_SM701_CACHE[cache_key])
    env_dir = (os.environ.get("SM701_DIR") or "").strip()
    env_model = (os.environ.get("SM701_MODEL") or "").strip()
    env_pred = (os.environ.get("SM701_PRED") or "").strip()
    home = os.path.expanduser("~")
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if env_dir:
        candidates.append(env_dir)
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
        if base and base not in candidates:
            candidates.append(base)
    mpre = _SM701_MK[mk]["mpre"]
    pred_file = _SM701_MK[mk]["pred"]
    for base in candidates:
        base = os.path.abspath(os.path.expanduser(base))
        db = (os.environ.get("SM701_DB") or
              os.path.join(base, "stock_cache.db"))
        script = os.path.join(base, "lgbm_train_backtest.py")
        if not (os.path.isfile(db) and os.path.isfile(script)):
            continue
        # SM701_MODEL 显式指定时强制单模型口径（仅 cn；多市场跳过 production 探测）
        prod = None if (env_model or mk != "cn") \
            else _sm701_production_set(base)
        if mk != "cn":
            prod = _sm701_production_set(base, mpre)
            if not prod:
                continue        # 该市场无生产模型集
        if prod:
            model = prod["models"]["1"]
            pred = env_pred if (env_pred and mk == "cn") else \
                (prod["pred"] if os.path.isfile(prod["pred"])
                 else os.path.join(base, "lgbm_output", pred_file))
        else:
            model = (env_model or
                     os.path.join(base, "lgbm_output", "lgbm_model.txt"))
            pred = env_pred or os.path.join(base, "lgbm_output",
                                            "valid_predictions.csv.gz")
        if not os.path.isfile(model) or not os.path.isfile(pred):
            continue
        out = {"dir": base, "db": db, "model": model, "pred": pred,
               "script": script, "prod": prod, "market": mk,
               "table": _SM701_MK[mk]["table"]}
        _SM701_CACHE[cache_key] = out
        return dict(out)
    raise RuntimeError(
        f"未找到 sm701 {mk} 市场模型集（需 production/prod_{mpre}h*_tech.txt "
        f"+ {pred_file}）；可用 SM701_DIR 指定目录")


def _sm701_conn(path=None):
    """只读打开 sm701 扩展库，避免与副本训练进程争用写锁。"""
    import urllib.request as _ur
    path = path or sm701_paths()["db"]
    uri = "file:" + _ur.pathname2url(os.path.abspath(path)) + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30)


def _sm701_module(paths=None):
    """加载 sm701 的特征工程模块，实时预测与离线训练保持同一口径。"""
    import importlib.util
    paths = paths or sm701_paths()
    cached = _SM701_CACHE.get("module")
    if cached and cached[0] == paths["script"]:
        return cached[1]
    spec = importlib.util.spec_from_file_location(
        "_stock_predict_sm701_lgbm", paths["script"])
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 sm701 特征工程: " + paths["script"])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _SM701_CACHE["module"] = (paths["script"], mod)
    return mod


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


def _sm701_predict_feats(feats, paths):
    """按 sm701 模型对特征矩阵打分。

    生产档（`paths["prod"]`）为 h1/h5/h10 三模型概率等权平均；否则单模型
    `paths["model"]`（用模型自带 feature_name 选列，兼容任意特征组）。"""
    import pandas as pd
    import lightgbm as lgb
    prod = (paths or {}).get("prod")
    if prod:
        names = prod["features"]
        X = pd.DataFrame({k: np.asarray(feats[k], dtype=np.float32)
                          for k in names}, dtype=np.float32)
        acc = None
        for h in sorted(prod["models"], key=int):
            pv = lgb.Booster(model_file=prod["models"][h]).predict(X)
            acc = pv if acc is None else acc + pv
        return acc / float(len(prod["models"]))
    booster = lgb.Booster(model_file=paths["model"])
    names = booster.feature_name()
    X = pd.DataFrame({k: np.asarray(feats[k], dtype=np.float32)
                      for k in names}, dtype=np.float32)
    return booster.predict(X)


_DB_LATEST_CACHE = {}           # path -> (ts, date)；MAX(date) 无 date 索引，
                                # 主库/副本全表扫 0.6~2s，60s 缓存供
                                # sm701 缓存 key 等高频调用方复用（v6.6.5）


def _db_latest_date(path=None):
    """行情库最新交易日；path 为空时读主库。失败返回空串。
    v6.6.5：加 60s 进程内缓存——本函数被 sm701 缓存 key 高频调用，
    全表 MAX(date) 在主库/副本上每次 0.6~2s；数据推进频率为日级，
    60s 缓存安全。"""
    try:
        key = os.path.abspath(path) if path else "main"
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
                    "select max(date) from daily_bars").fetchone()[0] or ""
            finally:
                conn.close()
        else:
            with _db_conn() as conn:
                d = conn.execute(
                    "select max(date) from daily_bars").fetchone()[0] or ""
        _DB_LATEST_CACHE[key] = (now, d)
        return d
    except sqlite3.Error:
        return ""


def _sm701_cache_epoch(src_max):
    """当日数据的缓存桶：src_max 为今天时按 30 分钟分桶（盘中数据会变），
    历史日期恒为 0（收盘定型，稳定缓存）。配合 key 用数据指纹替代
    库文件 mtime——预取/回填写库改变 mtime 但数据未推进，不应触发
    sm701 全市场重算（v6.6.5 修复：曾致 sm701 股票每次分析卡 100s+）。"""
    if src_max and src_max >= time.strftime("%Y-%m-%d"):
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
    sm_max = _db_latest_date(paths["db"])
    src_db, src_max = paths["db"], sm_max
    main_max = _db_latest_date()
    if main_max > sm_max:
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
        progress(f"test(sm701)：复算 {wanted[0]} ~ {wanted[-1]} 概率"
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
    main_max = _db_latest_date()
    key = (tuple(codes[:3]), len(codes), cal[0], cal[-1],
           os.path.getmtime(paths["db"]), _sm701_model_mtimes(paths),
           pred_mt, main_max, _sm701_cache_epoch(main_max))
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


def sm701_status():
    """sm701 外接算法数据/模型状态，供报告和排障展示。"""
    try:
        p = sm701_paths()
        with _sm701_conn(p["db"]) as conn:
            db_date = conn.execute("select max(date) from daily_bars").fetchone()[0]
        out = {"ok": True, "dir": p["dir"], "db_date": db_date or "",
               "live_db_date": max(db_date or "", _db_latest_date()),
               "model": p["model"], "pred": p["pred"]}
        prod = p.get("prod")
        if prod:
            out["prod_dir"] = prod["dir"]
            out["prod_models"] = {h: os.path.basename(v)
                                  for h, v in prod["models"].items()}
            out["prod_n_feat"] = len(prod["features"])
        return out
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# 外接算法信号阈值：模型概率滞回映射
# （v6.4.1：不消融，直接用 sm701 概率；≥0.55 买入、<0.50 卖出）
_SM701_SIG_BUY = 0.55
_SM701_SIG_SELL = 0.50


def _sm701_live_one(full, dates, progress=None):
    """多市场单股实时复算：读**副本库同源行情表**（hk_daily/us_daily/fut_daily
    ——特征与训练同源同口径），取 ≥180 根预热 + 目标日段，三周期模型等权。

    返回 {date: prob}。副本库缺最新日时用主库同前缀代码补（重叠段副本优先，
    接缝仅影响最后一日特征尾部）。缓存按（full, dates, 主库最新日/模型mtime）。"""
    import pandas as pd
    mk = _market_of(full)
    paths = sm701_paths(market=mk)
    sm_code = _sm701_code(full)
    dates_u = list(dict.fromkeys(d for d in dates if d))
    if not dates_u:
        return {}
    src_max = _db_latest_date()
    key = (full, tuple(dates_u), src_max, _sm701_cache_epoch(src_max),
           _sm701_model_mtimes(paths))
    cache = _SM701_CACHE.setdefault("live_one", {})
    hit = cache.get(key)
    if hit is not None:
        return dict(hit)
    table = _SM701_MK[mk]["table"]
    con = _sm701_conn(paths["db"])
    try:
        df = pd.read_sql_query(
            f"SELECT code,date,open,high,low,close,vol FROM {table} "
            f"WHERE code=? AND date>=? ORDER BY date",
            con, params=[sm_code, "2010-01-01"])
    finally:
        con.close()
    if df.empty:
        raise RuntimeError(f"副本库 {table} 无 {sm_code} 行情")
    # 主库补最新日（副本滞后时；重叠日以副本为准）
    try:
        with _db_conn() as conn:
            extra = pd.read_sql_query(
                "SELECT code,date,open,high,low,close,vol FROM daily_bars "
                "WHERE code=? ORDER BY date", conn, params=[full])
        if not extra.empty:
            have = set(df["date"])
            add = extra[~extra["date"].isin(have)]
            if not add.empty:
                df = pd.concat([df, add], ignore_index=True)
                df = df.sort_values("date").reset_index(drop=True)
    except Exception:
        log.debug("主库补最新分时数据失败(忽略)", exc_info=True)
    for c in ("open", "high", "low", "close", "vol"):
        df[c] = df[c].astype("float64")
    df["code"] = df["code"].astype("category")
    mod = _sm701_module(paths)
    feats = mod._compute_feats(df)
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


def sm701_prob_map(full, dates=None):
    """单股 sm701 概率序列 {date: prob}：历史落盘 + 新交易日实时复算。

    历史段流式读对应市场 valid_predictions[_市场].csv.gz（只留该股，按文件
    mtime 缓存）；dates 中晚于历史末日的交易日走实时复算——cn 全市场口径
    （_sm701_live_predict），hk/us/fut 单股口径（_sm701_live_one，
    v6.6.5 多市场）。crypto 无模型返回 {}。"""
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
    key = (pred_mt, _sm701_model_mtimes(paths),
           _db_latest_date(paths["db"]))
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
                              ("prob", "prob_lgbm", "prob_fuse")
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
        cache[full] = (key, dict(out))
    if dates:
        hist_max = max(out) if out else ""
        missing = ([d for d in dict.fromkeys(dates) if d > hist_max]
                   if hist_max else list(dict.fromkeys(dates)))
        if missing:
            try:
                if mk == "cn":
                    preds = _sm701_live_predict(
                        missing, list(dict.fromkeys(dates)))
                    out.update({d: pv for (code, d), pv in preds.items()
                                if code == full and pv is not None})
                else:
                    out.update(_sm701_live_one(full, missing))
            except Exception:
                log.exception("sm701 单股实时概率复算失败")
    return out


def _sig_sm701_from_probs(rows, probs):
    """sm701 概率序列 → 买卖点（probs 可为 {date: prob} 或按行对齐的序列）。

    与 `_sig_sm701` 同一滞回口径，供研究消融在父进程一次性预载概率后
    直接生成信号，避免每个 worker 重复扫描 70MB+ 预测文件。"""
    if not rows or probs is None:
        return []
    seq = isinstance(probs, dict)
    out = []
    state = 0
    for i, r in enumerate(rows):
        p = probs.get(r.get("date")) if seq else (
            probs[i] if i < len(probs) else None)
        if p is None:
            continue
        if state <= 0 and p >= _SM701_SIG_BUY:
            out.append((i, r["date"], "BUY",
                        f"sm701概率{p:.2f}≥{_SM701_SIG_BUY:.2f}"))
            state = 1
        elif state >= 1 and p < _SM701_SIG_SELL:
            out.append((i, r["date"], "SELL",
                        f"sm701概率{p:.2f}<{_SM701_SIG_SELL:.2f}"))
            state = 0
    return out


def _sig_sm701(rows, full, **_kw):
    """sm701 概率信号（外接算法直出，A股消融候选亦用此口径）：
    概率 ≥0.55 触发 BUY、<0.50 触发 SELL（滞回），同一方向不重复。"""
    if not rows or not full:
        return []
    try:
        probs = sm701_prob_map(full, [r.get("date") for r in rows])
    except Exception:
        log.exception("sm701 单股概率读取失败")
        return []
    return _sig_sm701_from_probs(rows, probs)


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

