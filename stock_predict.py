#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票形态相似度预测 · 命令行版（独立单文件，不依赖 stock_gui.py）

与 stock_gui.py 共用同一套分析算法（由 build_cli.py 自动生成）：
价格形态 + 量能状态 + 大盘 + 板块 + 同行业 + 同市值层 多级加权匹配，
多算法消融选策略（10 类信号，训练/验证切分防过拟合）。
内建 SQLite 缓存（stock_cache.db），同行业/同市值层样本池只回填一次。
K线源自动切换：腾讯(多域名容灾) -> 东财(4 host)，失效域自动熔断/自愈；支持代理。
每日拉取诊断：stock_fetch.log（缓存命中/拉取原因/命中源/耗时）。

用法：python stock_predict.py [--push] [--refresh-cache] [--refresh-etf] [--backfill]
                             [--clean] [--research] [--v4 [--v4-limit N]]
                             [--tiers [--tier 稳健|均衡|激进] [--ai-tier]]
                             [--tiers-backtest] [--universe all|main|etf|all_etf]
                             [股票代码]
  --push           分析完成后把报告推送到 Pi 量化系统收件箱（ai-quant）
  --refresh-cache  刷新全市场代码表/市值分层（约1分钟，7天有效）
  --refresh-etf    刷新东财 ETF/LOF 代码表并回填历史日K（约1500只，10~25分钟）
  --backfill       全市场深历史日K回填（目标=max(950, 设置内最大拉取样本量)，断点续传）
  --clean          数据清洗（结构异常/除权残留/退市/粘性，扫描+修复）
  --research       全A研究报告：各算法 IC/胜率/年化/回撤 跨股聚合
  --v4             v4.0 全A研究：Walk-Forward自适应ML + 三档风险回测 + 消融
  --tiers          v6.1.5 三档组合：输出最新目标持仓/闸门状态（可配 --tier）
  --ai-tier        荐股前由AI在三档内选一档（按设置里的风险偏好锚定）
  --universe       标的池：all(全A不含ETF，默认)/main(沪深主板)/etf(仅ETF)/all_etf(全A含ETF)
  --tiers-backtest v6.1.5 三档组合：全期回测摘要（相位平均，含全部费用）
  --picks-backtest v6.1.5 荐股收益回测（逐笔口径，按风险偏好；--tier 过滤）
  --picks-seg      荐股回测区间：full(默认)/val/bull/2024/2025...
"""


import atexit
import configparser
import heapq
import json
import logging
import math
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

try:
    import numpy as np            # 数值加速（缺失时自动退回纯Python）
except ImportError:
    np = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CACHE_OK = True      # 缓存层已内嵌，恒可用


# ================= 内嵌缓存层（原 stock_cache.py，单文件化）
# 库路径：环境变量 STOCK_DB 可指向另一份库（研究/回测隔离用，避免与正在
# 运行的 GUI 争用主库）；缺省仍是项目目录内 stock_cache.db
DB_PATH = (os.environ.get("STOCK_DB") or "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "stock_cache.db")
INI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "stock_gui.ini")

# ---- 日志（#10）：文件 INFO+（滚动5MBx2），控制台 WARNING+ ----
from logging.handlers import RotatingFileHandler  # noqa: E402

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "stock_gui.log")
# 数据层专项日志（诊断"1.7G缓存为何还联网拉取"）：只记缓存判定/拉取/数据源，
# 不落通用噪声，独立滚动2MBx2；不想看时直接删文件即可（会自动重建）。
FETCH_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "stock_fetch.log")
log = logging.getLogger("stock")
flog = logging.getLogger("stock.fetch")     # 缓存/拉取/源诊断


def setup_logging():
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(name)s %(message)s")
    try:
        fh = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024,
                                 backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO)
        log.addHandler(fh)
    except OSError:
        pass                      # 文件不可写时退化为仅控制台
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(logging.WARNING)
    log.addHandler(sh)
    # 拉取诊断日志：独立文件，不向 stock_gui.log 冒泡
    flog.setLevel(logging.INFO)
    flog.propagate = False
    if not flog.handlers:
        try:
            fh2 = RotatingFileHandler(FETCH_LOG_PATH,
                                      maxBytes=2 * 1024 * 1024,
                                      backupCount=2, encoding="utf-8")
            fh2.setFormatter(fmt)
            fh2.setLevel(logging.INFO)
            flog.addHandler(fh2)
        except OSError:
            flog.addHandler(logging.NullHandler())
    return log


setup_logging()

# 应用版本号（回测产物目录/关于/UA 共用；2026-09-26 升 6.1.5）
APP_VERSION = "6.1.5"


# ---- 缓存/拉取统计：定期汇总，回答"缓存够新为何还联网" ----
_FSTAT = {"calls": 0, "hit": 0, "stale": 0, "anomaly": 0, "empty": 0,
          "neg": 0, "pull_ok": 0, "pull_fail": 0, "pull_rows": 0}
_FSTAT_LOCK = threading.Lock()
_FSTAT_EVERY = 200              # 每 N 次 get_daily 打一行汇总


def _fstat_inc(key, n=1):
    with _FSTAT_LOCK:
        _FSTAT[key] = _FSTAT.get(key, 0) + n


def _fstat_log(force=False):
    """输出缓存命中/联网拉取累计统计（force=True 时忽略采样间隔）。"""
    with _FSTAT_LOCK:
        s = dict(_FSTAT)
    if s["calls"] == 0:
        return
    if not force and s["calls"] % _FSTAT_EVERY:
        return
    flog.info("统计: 调用=%d 直读缓存=%d 过期拉取=%d 异常拉取=%d 无缓存=%d "
              "负缓存=%d | 联网成功=%d 失败=%d 入库根数=%d",
              s["calls"], s["hit"], s["stale"], s["anomaly"],
              s["empty"], s["neg"], s["pull_ok"], s["pull_fail"],
              s["pull_rows"])


try:
    atexit.register(_fstat_log, True)   # 退出时落最后一行累计统计
except Exception:
    pass

KLINE_URL = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/"
             "fqkline/get")

# DeepSeek API Key 读取优先级：环境变量 > ini 文件
# 强烈建议通过环境变量 DEEPSEEK_API_KEY 设置，不要在磁盘留存明文 Key。
ENV_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()

# 急救箱(stock_firstaid.py)写入的备用源覆盖默认值：
# ini [data] kline_url = 完整接口URL（须以http(s)开头，否则忽略）
try:
    import configparser as _cp_firstaid
    _fa_cp = _cp_firstaid.ConfigParser()
    _fa_cp.read(INI_PATH, encoding="utf-8")
    _fa_url = _fa_cp.get("data", "kline_url", fallback="").strip()
    if _fa_url.startswith(("http://", "https://")):
        KLINE_URL = _fa_url
except Exception:
    pass
UT = "fa5fd1943c7b386f172d6893dbfba10b"
TIERS = ("大盘", "中盘", "小盘")
STOCKS_TTL = 7 * 86400          # 全市场代码表缓存7天
INIT_LOCK = threading.Lock()
REFRESH_LOCK = threading.Lock()

# 默认池大小（可被 analyze 的参数覆盖）
L2_DEFAULT_N = 50               # 同行业同伴数
L3_DEFAULT_N = 100              # 同市值层抽样数

# 全局共享可变状态锁：所有缓存字典的读-改-写必须持有本锁
_STATE_LOCK = threading.RLock()

# 共享线程池：预取/分析/后台任务复用，避免每次新建线程池开销
_SHARED_EX = ThreadPoolExecutor(max_workers=8, thread_name_prefix="data")
_BG_EX = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bg")


class CFG:
    """集中调参（原散落各处的魔数收敛于此；模块级别名保持兼容）。"""
    W_WINDOW = 20                       # 形态匹配窗口长度（小样本扫描 W20 略优；
                                        # 全市场样本外 L1 增量≈0，见 README 研究
                                        # 结论——勿据样本内 IC 调参）
    TOPK = 10                           # Top-K 相似样本数（显示用）
    CANDIDATE_TOPK = 50                 # 候选样本数（扩大后再筛选）
    LV_W = {"L1": 0.6, "L2": 0.3, "L3": 0.1}    # 三级样本池权重
    SIGNAL_SCORE_BUY = 2                # 多头信号触发分
    SIGNAL_SCORE_SELL = -2              # 空头信号触发分
    SIGNAL_COOLDOWN = 5                 # 相邻信号最小间隔(交易日)
    WEAK_IDX_TH = -1.5                  # 大盘弱势阈值(%)
    WEAK_SEC_TH = -2.0                  # 板块弱势阈值(%)
    BAND_FIT_MIN = 60.0                 # 波段适合度门槛
    PRED_MAX_DAYS = 10                  # 多日预测天数
    # 单只股票K线联网拉取根数（默认1000；只限制联网拉取，不限制读库/分析，
    # 库内已有历史永远全量参与计算；深历史回填 --backfill 另有 950+ 目标）
    MAX_FETCH_BARS = 1000
    # 后台主动预取未分析个股K线（样本池优先→全库滚动；ini [predict] auto_prefetch=0 关）
    AUTO_PREFETCH = True
    
    # 样本质量筛选与加权参数
    SIMILARITY_WEIGHTING = False        # 指数相似度加权（消融回测证实拖后腿：
                                        # 关闭后 命中50.4%→52.4%, IC -0.043→+0.020）
    QUALITY_FILTER = True               # 是否启用样本质量筛选
    # 注：原 MAX_DAILY_CHANGE=10 已删除（死常数、且不区分板块涨跌停；
    # 清洗层/回测层另有按板块的涨跌停判定，见 _limit_pct/_v4_limit_pct）
    MIN_SAMPLES_REQUIRED = 3            # 最少样本数要求 - 少于则降低置信度
    SIMILARITY_CUTOFF = 2.5             # 相似度截断阈值 - 超过则降低权重
    TIME_DECAY_ENABLED = True           # 是否启用时间衰减
    TIME_DECAY_DAYS = 90                # 时间衰减天数 - 超过此天数的样本权重衰减
    TIME_DECAY_RATE = 0.3               # 时间衰减率 - 超过天数的样本权重乘数
    
    # 置信度系统参数
    CONFIDENCE_ENABLED = True           # 是否启用置信度系统
    LOW_CONFIDENCE_SCORE = 0.3          # 低置信度阈值
    MEDIUM_CONFIDENCE_SCORE = 0.6       # 中置信度阈值

    # 多维形态匹配扩展（K线结构/波动率/RSI/量变/周线环境）
    STRUCT_W = 0.50                     # K线结构距离权重
    VOLA_W = 0.80                       # 波动率距离权重
    RSI_W = 0.50                        # RSI距离权重
    VOLCHG_W = 0.40                     # 量变(近5日/前5日)距离权重
    WEEKLY_W = 0.60                     # 周线环境距离权重
    WEEKLY_N = 4                        # 周线环境回看周数

    # 动态三级权重
    DYNAMIC_LV_W = True                 # 是否按各层最优相似度动态调整 L1/L2/L3 权重
    DYN_LV_STRENGTH = 0.5               # 动态混合强度(0=固定先验,1=完全按样本质量)
    # 层级消融回测(n=225)：L1+L2 命中56.9%/IC+0.057 最优；
    # L3(同市值层)拖后腿(并入后IC降至-0.001)，默认关闭
    ENABLE_L3 = False
    # 区间校准系数（n=4500实测：样本分位区间系统性过窄41%/70%，
    # 偏离P50放大1.3倍后 P25-P75→51.8%(名义50)、P10-P90→80.0%(名义80)）
    INTERVAL_K = 1.3
    # T+5 区间校准系数（n=4500标定 1.4最优：51.6%/79.2%）
    INTERVAL_K5 = 1.4

    # ---- 风险偏好（三级·网格寻优后参数）----
    # 保守=信号严(评分3+冷却8)+止损紧(ATR1.5/回落4%即走)
    # 激进=捕捉机会(评分1+冷却3)+止损松(ATR2.5/回落10%)
    # 数据源：n=6000回测网格，详见 报告_买卖点收益回测.md
    RISK_MODE = "稳健"
    RISK_PARAMS = {
        "保守": {"atr_mult": 1.5, "trail_trigger": 1.01,
                 "trail_ratio": 0.96, "buy_th": 3, "cooldown": 8},
        "稳健": {"atr_mult": 1.5, "trail_trigger": 1.02,
                 "trail_ratio": 0.94, "buy_th": 2, "cooldown": 5},
        "激进": {"atr_mult": 2.5, "trail_trigger": 1.05,
                 "trail_ratio": 0.90, "buy_th": 1, "cooldown": 3},
    }

    def risk_params():
        return CFG.RISK_PARAMS.get(CFG.RISK_MODE, CFG.RISK_PARAMS["稳健"])
    
    # 各技术维度在信号打分中的权重（1.0=标准；<1 降权、>1 升权）
    IND_W = {
        "MACD": 1.1,        # 趋势主指标，加权
        "KDJ": 0.9,         # 摆动指标，略降权（横盘易钝化）
        "RSI": 0.9,         # 同上
        "量价": 1.0,
        "MA20": 1.0,
        "MA趋势": 1.2,      # MA20/60趋势状态（v3.3全A实证 IC 0.228，最强规则信号）
        "形态": 1.2,        # L1形态上行概率（IC 0.265，全A实证最强）
        "爆发力": 1.0,      # 20日动量（10%~35%强势区加分，>35%过热减分：bias20 极端延伸 IC 为负）
        "量能": 0.9,        # 量能扩张（5日均量/20日均量，突破期特征）
        "板块": 1.0,        # 板块轮动（行业5日收益强势/前20%领先，v4.0.2 全A实证 Rot 变体显著增益）
        "筹码": 0.8,
        "布林带": 0.8,      # 均值回归维度，震荡市才准，降权
        "ADX": 0.8,         # 趋势强度过滤器维度
    }


def _load_predict_cfg():
    """从 stock_gui.ini [predict] 读取用户调过的预测参数（带范围钳制）。
    必须在模块级别名 W_WINDOW/TOPK 赋值之前执行。"""
    try:
        cp = configparser.ConfigParser()
        cp.read(INI_PATH, encoding="utf-8")
        if not cp.has_section("predict"):
            cp.add_section("predict")   # 缺节时 get 才不会抛 NoSectionError

        def gi(key, dflt, lo, hi):
            try:
                return max(lo, min(hi, int(cp.get("predict", key,
                                                  fallback=dflt))))
            except (ValueError, TypeError):
                return dflt

        def gf(key, dflt, lo, hi):
            try:
                return max(lo, min(hi, float(cp.get("predict", key,
                                                    fallback=dflt))))
            except (ValueError, TypeError):
                return dflt

        CFG.W_WINDOW = gi("w_window", CFG.W_WINDOW, 5, 30)
        CFG.TOPK = gi("topk", CFG.TOPK, 3, 30)
        CFG.CANDIDATE_TOPK = gi("candidate_topk", CFG.CANDIDATE_TOPK, 10, 100)
        # 漏斗一致性：候选样本数不得小于最终样本数（否则筛选后取不满/参数打架）
        CFG.CANDIDATE_TOPK = max(CFG.CANDIDATE_TOPK, CFG.TOPK)
        CFG.TIME_DECAY_DAYS = gi("time_decay_days", CFG.TIME_DECAY_DAYS,
                                 0, 1095)
        CFG.TIME_DECAY_RATE = gf("time_decay_rate", CFG.TIME_DECAY_RATE,
                                 0.0, 1.0)
        CFG.DYN_LV_STRENGTH = gf("dyn_lv_strength", CFG.DYN_LV_STRENGTH,
                                 0.0, 1.0)
        CFG.WEEKLY_N = gi("weekly_n", CFG.WEEKLY_N, 2, 8)
        CFG.STRUCT_W = gf("struct_w", CFG.STRUCT_W, 0.0, 3.0)
        CFG.VOLA_W = gf("vola_w", CFG.VOLA_W, 0.0, 3.0)
        CFG.RSI_W = gf("rsi_w", CFG.RSI_W, 0.0, 3.0)
        CFG.VOLCHG_W = gf("volchg_w", CFG.VOLCHG_W, 0.0, 3.0)
        CFG.WEEKLY_W = gf("weekly_w", CFG.WEEKLY_W, 0.0, 3.0)
        rm = cp.get("predict", "risk_mode", fallback=CFG.RISK_MODE)
        if rm in CFG.RISK_PARAMS:
            CFG.RISK_MODE = rm
        CFG.ENABLE_L3 = bool(gi("enable_l3", 0 if not CFG.ENABLE_L3 else 1,
                                0, 1))
        CFG.AUTO_PREFETCH = bool(gi("auto_prefetch",
                                    1 if CFG.AUTO_PREFETCH else 0, 0, 1))
        CFG.MAX_FETCH_BARS = gi("max_fetch_bars", CFG.MAX_FETCH_BARS,
                                100, 3000)
    except Exception:
        log.exception("读取预测参数失败(使用默认)")


_load_predict_cfg()


W_WINDOW: int = CFG.W_WINDOW
TOPK: int = CFG.TOPK


# ================= 基础 =================

def _cx():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db_conn(commit: bool = False):
    """SQLite 连接上下文管理器：保证提交/回滚并关闭，杜绝连接泄露。"""
    conn = _cx()
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            log.exception("db rollback failed")
        raise
    finally:
        conn.close()


def init_db() -> None:
    with INIT_LOCK:
        with db_conn(commit=True) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS daily_bars(
                    code TEXT NOT NULL, date TEXT NOT NULL,
                    open REAL, high REAL, low REAL, close REAL, vol REAL,
                    PRIMARY KEY(code, date));
                CREATE INDEX IF NOT EXISTS idx_bars_code
                    ON daily_bars(code, date);
                CREATE TABLE IF NOT EXISTS stocks(
                    code TEXT PRIMARY KEY, name TEXT, industry TEXT,
                    mktcap REAL, tier TEXT, updated TEXT);
                CREATE INDEX IF NOT EXISTS idx_stocks_industry
                    ON stocks(industry);
                CREATE INDEX IF NOT EXISTS idx_stocks_tier
                    ON stocks(tier);
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS failed(
                    code TEXT PRIMARY KEY, ts REAL, reason TEXT);
            """)
            # 迁移：旧库 failed 表若无 reason 列则补列
            try:
                cols = [r[1] for r in conn.execute(
                    "PRAGMA table_info(failed)").fetchall()]
                if cols and "reason" not in cols:
                    conn.execute("ALTER TABLE failed ADD COLUMN reason TEXT")
            except Exception:
                log.exception("failed 表迁移失败(忽略)")


init_db()


def _get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                 (key, str(value)))


# ================= 代理 =================

_PROXY_OPENER = None            # 仅K线等 stock_cache 内部请求走代理


def set_proxy(url):
    """设置K线数据源代理（只影响 stock_cache 的请求；
    行情快照/板块等国内接口保持直连）。url 形如 http://127.0.0.1:7890。"""
    global _PROXY_OPENER
    url = (url or "").strip()
    try:
        if not url:
            _PROXY_OPENER = None
            return ""
        if not url.startswith("http"):
            url = "http://" + url
        handler = urllib.request.ProxyHandler({"http": url, "https": url})
        _PROXY_OPENER = urllib.request.build_opener(handler)
        return url
    except Exception:
        _PROXY_OPENER = None
        return ""


def _load_proxy_ini():
    try:
        import configparser
        cp = configparser.ConfigParser()
        cp.read(INI_PATH, encoding="utf-8")
        return cp.get("proxy", "url", fallback="")
    except Exception:
        return ""


set_proxy(_load_proxy_ini())    # 导入即生效（GUI/CLI通用）


# ================= AI 模型配置（OpenAI 兼容：DeepSeek/智谱/opencode 等） =================

AI_MODEL_DEFAULT = "deepseek-v4-pro"
AI_BASE_DEFAULT = "https://api.deepseek.com"

# 未配置 ini Key 时回退主目录 opencode 授权（opencode-go，免配置开箱即用）
OPENCODE_AUTH_PATH = os.path.expanduser(
    "~/.local/share/opencode/auth.json")
OPENCODE_GO_BASE = "https://opencode.ai/zen/go/v1"
OPENCODE_GO_MODEL = "kimi-k3"


def _ai_ini_get(section, key, default=""):
    try:
        cp = configparser.ConfigParser()
        cp.read(INI_PATH, encoding="utf-8")
        return (cp.get(section, key, fallback=default) or "").strip()
    except Exception:
        return default


def _opencode_go_key() -> str:
    """主目录 opencode 授权文件里的 opencode-go Key（读取失败返回空串）。"""
    try:
        with open(OPENCODE_AUTH_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return ((d.get("opencode-go") or {}).get("key") or "").strip()
    except Exception:
        return ""


def _use_opencode_go() -> bool:
    """ini/环境变量均未提供 Key 时，回退使用主目录 opencode-go 授权。"""
    return (not ENV_API_KEY
            and not _ai_ini_get("deepseek", "api_key", "")
            and bool(_opencode_go_key()))


def _load_ai_model() -> str:
    """ini [deepseek] model；缺省时若走 opencode-go 回退则用其默认模型。"""
    m = _ai_ini_get("deepseek", "model", "")
    if m:
        return m
    return OPENCODE_GO_MODEL if _use_opencode_go() else AI_MODEL_DEFAULT


def _load_ai_base() -> str:
    """ini [deepseek] base_url；缺省时若走 opencode-go 回退则用 zen/go 接口。"""
    b = _ai_ini_get("deepseek", "base_url", "")
    if b:
        return b
    return OPENCODE_GO_BASE if _use_opencode_go() else AI_BASE_DEFAULT


def _normalize_ai_base(base: str) -> str:
    """接口地址规范化：去尾斜杠；允许填到 /v1 或完整 /chat/completions。"""
    b = (base or "").strip().rstrip("/")
    return b or AI_BASE_DEFAULT


AI_MODEL = _load_ai_model()
AI_BASE_URL = _normalize_ai_base(_load_ai_base())


def _save_ai_conf(model=None, base=None) -> None:
    """持久化 AI 模型/接口地址到 ini（None 表示不改）。"""
    global AI_MODEL, AI_BASE_URL
    try:
        cp = configparser.ConfigParser()
        cp.read(INI_PATH, encoding="utf-8")
        if not cp.has_section("deepseek"):
            cp.add_section("deepseek")
        if model is not None:
            AI_MODEL = (model or "").strip() or AI_MODEL_DEFAULT
            cp.set("deepseek", "model", AI_MODEL)
        if base is not None:
            AI_BASE_URL = _normalize_ai_base(base)
            cp.set("deepseek", "base_url", AI_BASE_URL)
        with open(INI_PATH, "w", encoding="utf-8") as f:
            cp.write(f)
    except Exception:
        log.exception("保存AI设置失败")


def set_ai_model(model: str) -> None:
    """运行时切换AI模型并持久化到 ini。"""
    _save_ai_conf(model=model)


def set_ai_base(base: str) -> None:
    """运行时切换OpenAI兼容接口地址并持久化到 ini。"""
    _save_ai_conf(base=base)


def get_ai_key() -> str:
    """当前可用 Key：环境变量 > ini > 主目录 opencode-go 授权。"""
    return (ENV_API_KEY or _ai_ini_get("deepseek", "api_key", "")
            or _opencode_go_key()).strip()


# ================= 荐股权限（板块/行业，供设置与荐股过滤共用） =================

PICK_PERMS = {"industries": set(), "boards": set()}


def _load_pick_perms():
    """从 ini [picks] 读取荐股权限；空集=不限制。"""
    inds = {x.strip() for x in
            _ai_ini_get("picks", "industries", "").split(",") if x.strip()}
    boards = {x.strip() for x in
              _ai_ini_get("picks", "boards", "").split(",") if x.strip()}
    PICK_PERMS["industries"] = inds
    PICK_PERMS["boards"] = boards
    return PICK_PERMS


_load_pick_perms()


def picks_conf() -> dict:
    """荐股设置：AI自动选档 / 风险偏好 / 股票池口径。"""
    return {
        "ai_auto_tier": _ai_ini_get("picks", "ai_auto_tier", "0") == "1",
        "risk_pref": _ai_ini_get("picks", "risk_pref", "稳健") or "稳健",
        "universe": (_ai_ini_get("picks", "universe", "all") or "all")
        if _ai_ini_get("picks", "universe", "all")
        in ("all", "main", "etf", "all_etf")
        else "all",
    }


def pick_allowed(code: str, industry: str = "") -> bool:
    """荐股权限判断：板块集合/行业集合为空表示不限制该项。"""
    boards = PICK_PERMS.get("boards") or set()
    if boards:
        if _is_etf(code):
            b = "ETF"
        elif code.startswith(("sh60", "sz00")):
            b = "主板"
        elif code.startswith("sz30"):
            b = "创业板"
        elif code.startswith("sh68"):
            b = "科创板"
        else:
            b = "其他"
        if b not in boards:
            return False
    inds = PICK_PERMS.get("industries") or set()
    if inds and (industry or "").strip() not in inds:
        return False
    return True


# ================= 数据源熔断器（针对503限流） =================

_SRC_CB = {}                    # 源名 -> [连续失败数, 熔断截止时间戳]
_CB_LOCK = threading.Lock()
_CB_THRESHOLD = 2               # 连续失败N次触发熔断
_CB_BASE_COOLDOWN = 60.0        # 首次熔断冷却60秒
_CB_MAX_COOLDOWN = 600.0        # 冷却上限10分钟


def _cb_ok(name):
    """源当前是否可用（未熔断）。"""
    st = _SRC_CB.get(name)
    return not (st and st[1] > 0 and time.time() < st[1])


def _cb_record(name, ok, err=None):
    """上报数据源一次请求结果。

    - 成功 → 计数清零，立即结束熔断（半开探测成功）；
    - 失败且属限流类(503等) → 连续失败数+1，达到阈值按
      cooldown = min(BASE * 2^(n-THRESHOLD), MAX) 指数延长熔断时间；
    - 普通网络错误只累计失败数，不单独延长冷却。"""
    ratelimited = (not ok) and _is_ratelimit_err(err) if err is not None \
        else False
    with _CB_LOCK:
        st = _SRC_CB.setdefault(name, [0, 0.0])
        if ok:
            st[0], st[1] = 0, 0.0
            return
        st[0] += 1
        if ratelimited and st[0] >= _CB_THRESHOLD:
            cd = min(_CB_BASE_COOLDOWN *
                     (2 ** (st[0] - _CB_THRESHOLD)), _CB_MAX_COOLDOWN)
            st[1] = max(st[1], time.time() + cd)


def _is_ratelimit_err(e):
    """识别服务端限流/封禁类错误：HTTP 429/501/502/503/504 或连接被重置。

    东财反爬/整域故障表现为 RemoteDisconnected / Connection reset
    （非 HTTP 状态码），同样应触发熔断降级，避免每个源反复撞墙；
    腾讯对反爬域名（如 web.ifzq 的 hfq 请求）返回 501，若不纳入熔断，
    配置里的死源会每只股票都被重试一次。"""
    code = getattr(e, "code", None)
    if code is not None:
        return code in (429, 501, 502, 503, 504)
    s = str(e)
    if any(c in s for c in ("RemoteDisconnected", "Remote end closed",
                            "Connection reset", "Connection aborted",
                            "连接被重置")):
        return True
    return any(c in s for c in ("501", "502", "503", "504", "429",
                                "Service Unavailable"))


def _backoff_delay(attempt, base=0.5, cap=6.0):
    """指数退避 + 抖动：base * 2^attempt，上限cap，±25%随机抖动防雪崩。"""
    d = min(base * (2 ** attempt), cap)
    import random
    return d * (0.75 + random.random() * 0.5)


# ---- 代理路由策略：国内行情直连优先，代理故障自动旁路 ----
_PROXY_DEAD_UNTIL = [0.0]       # 代理连接失败后的旁路截止时间
_PROXY_LOCK = threading.Lock()

# 国内行情域名：走本地代理会绕境外节点，易被服务端重置/限流
_DOMESTIC_SUFFIX = (
    "eastmoney.com", "gtimg.cn", "qq.com", "sinajs.cn", "sina.com.cn",
    "sina.com", "163.com", "126.net", "sse.com.cn", "szse.cn",
    "cninfo.com.cn", "csindex.com.cn",
)


def _is_domestic_url(url):
    """国内行情域名判断（含子域）。"""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == s or host.endswith("." + s)
               for s in _DOMESTIC_SUFFIX)


def _proxy_dead():
    with _PROXY_LOCK:
        return time.time() < _PROXY_DEAD_UNTIL[0]


def _mark_proxy_dead(seconds=120):
    """代理被拒（软件未开/端口关闭）后暂时旁路，避免每次双倍超时。"""
    with _PROXY_LOCK:
        _PROXY_DEAD_UNTIL[0] = time.time() + seconds


def _open_url(req, url, timeout):
    """按源类型选通道：国内直连优先，国外代理优先；代理被拒自动旁路。

    返回响应字节；两个通道都失败时抛最后一个异常。"""
    can_proxy = _PROXY_OPENER is not None and not _proxy_dead()
    if not can_proxy:
        order = [None]
    elif _is_domestic_url(url):
        order = [None, _PROXY_OPENER]      # 国内：直连失败再试代理
    else:
        order = [_PROXY_OPENER, None]      # 国外（AI接口等）：代理优先
    last = None
    for opener in order:
        try:
            if opener is None:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read()
            with opener.open(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            if last is None:
                last = e      # 保留首选通道的错误（更具代表性）
            if opener is not None and "refused" in str(e).lower():
                _mark_proxy_dead()
                log.debug("代理不可用，改为直连 %s: %s", url[:60], e)
            continue
    raise last if last is not None else RuntimeError("无可用网络通道")


def _http_get(url, retries=3, timeout=15, decode="utf-8", headers=None,
              src_name=None):
    """HTTP GET（带限流感知重试）。

    - 普通错误：指数退避+抖动后原URL重试；
    - 503/429等限流：只做最多1次退避重试就抛出，
      让上层多源切换/熔断机制接管，避免反复撞同一限流IP。
    - src_name 非空时向数据源熔断器上报成败。"""
    last = None
    hdr = {"User-Agent": "Mozilla/5.0"}
    if headers:
        hdr.update(headers)
    ok_flag = False
    try:
        for a in range(retries):
            with _THROTTLE_LOCK:
                wait = _MIN_INTERVAL - (time.time() - _LAST_REQ[0])
                if wait > 0:
                    time.sleep(wait)
                _LAST_REQ[0] = time.time()
            try:
                req = urllib.request.Request(url, headers=hdr)
                txt = _open_url(req, url, timeout).decode(
                    decode, errors="ignore")
                ok_flag = True
                return txt
            except Exception as e:
                last = e
                # 限流类错误：退避后仅再试一次即放弃（快速切源）
                eff_retries = min(retries, 2) if _is_ratelimit_err(e) \
                    else retries
                if a + 1 >= eff_retries:
                    break
                time.sleep(_backoff_delay(a))
        raise RuntimeError(f"网络请求失败: {last}")
    finally:
        if src_name:
            _cb_record(src_name, ok_flag, last)


# ================= 交易日辅助 =================

def _dstr(d):
    return d.strftime("%Y-%m-%d")


def _prev_weekday(d):
    import datetime
    d -= datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d


def _is_index_code(code: str) -> bool:
    """是否指数代码（sh000* / sz399*）：指数日K用作全库交易日历锚。"""
    return code.startswith(("sh000", "sz399"))


_INDEX_TD_CACHE = {"ts": 0.0, "date": ""}
_INDEX_PULL_TS = [0.0]          # 最近一次指数拉取尝试（防收盘后重复空拉）


def _index_last_td(max_age=60.0):
    """库内指数日K的最新日期（=真实上一交易日，节假日安全），60s 缓存。
    指数只有 3 只且由 stale_codes/analyze 持续回补，适合做全库日历锚。"""
    now = time.time()
    if now - _INDEX_TD_CACHE["ts"] < max_age:
        return _INDEX_TD_CACHE["date"]
    d = ""
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM daily_bars WHERE code IN "
                "('sh000001','sz399001','sz399006')").fetchone()
        d = (row[0] or "") if row else ""
    except Exception:
        log.debug("指数日历锚读取失败", exc_info=True)
    _INDEX_TD_CACHE["date"], _INDEX_TD_CACHE["ts"] = d, now
    return d


def _index_expected_td():
    """指数应有最新日K：收盘后（当日确为交易日）为今日，否则维持库内锚。

    指数是交易日历锚，不能像个股那样用锚否定自身更新；这里用行情快照
    （`_allow_today_bar`）判定"收盘后"才要求今日数据，盘中/休市一律回到锚。
    修复 2026-09-26：此前按自然日历把休市日（中秋 09-25）当应有交易日，
    周末每次用到指数K线（分析/市场简报/消融 regime）都联网重拉一遍。"""
    import datetime
    if _allow_today_bar() and time.time() - _INDEX_PULL_TS[0] >= 300:
        return _dstr(datetime.date.today())
    return _index_last_td()


def last_completed_td():
    """库中最后一天日K应为的日期（节假日安全）。

    自然日历（收盘后≥15:05取今日，否则上一工作日）只作上界；当上一"应该
    交易日"实际休市（中秋/国庆调休）时，以库内指数日K最新日期为准。
    修复 2026-09-26：休市日 prev_weekday 指向未开市的日历工作日（如中秋
    09-25），全库 7000+ 对象被判"过期"而反复联网空拉（1.7G 缓存仍拉取）。"""
    import datetime
    if _allow_today_bar():
        return _dstr(datetime.date.today())
    naive = _dstr(_prev_weekday(datetime.date.today()))
    anchor = _index_last_td()
    if anchor and anchor < naive:
        return anchor
    return naive


# 上证指数最近一次行情快照日期（YYYYMMDD）：判定今日是否交易日
# （节假日/休市时快照日期会停在上一交易日，避免把今天当交易日空拉K线）
_LAST_INDEX_SNAP_DATE = [""]


def _note_index_snap(t):
    """记录上证指数快照日期（只由指数行情调用，个股停牌不影响休市判定）。"""
    d = (t or "")[:8].replace("-", "")
    if len(d) == 8 and d.isdigit() and d > _LAST_INDEX_SNAP_DATE[0]:
        _LAST_INDEX_SNAP_DATE[0] = d


def _today_is_session():
    """今日是否交易日：有今日指数快照→是；快照落后且已过开盘→否；未知→None。"""
    d = _LAST_INDEX_SNAP_DATE[0]
    today = time.strftime("%Y%m%d")
    if not d:
        return None
    if d >= today:
        return True
    if time.strftime("%H%M") >= "0915":
        return False
    return None


def _allow_today_bar():
    """是否允许今日日K入库：工作日且已过 15:05（数据已收盘定型），
    且指数快照未表明今日休市（节假日/周末调休）。"""
    import datetime
    if not (datetime.date.today().weekday() < 5
            and time.strftime("%H:%M") >= "15:05"):
        return False
    return _today_is_session() is not False


# ================= 日K增量缓存 =================

def _db_rows(conn, code):
    rows = conn.execute(
        "SELECT date,open,high,low,close,vol FROM daily_bars "
        "WHERE code=? ORDER BY date", (code,)).fetchall()
    return [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "vol": r[5] or 0.0} for r in rows]


def db_hist_count(code: str) -> int:
    """快速返回该股缓存的日K总根数（仅查库，不联网）。"""
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM daily_bars WHERE code=?",
                (code,)).fetchone()
            return row[0] if row else 0
    except Exception:
        log.exception("db_hist_count failed: %s", code)
        return 0


def _db_rows_batch(conn, codes):
    """批量读取多只股票缓存，返回 {code: [row_dict,...]}。单次连接。"""
    placeholders = ",".join("?" for _ in codes)
    cur = conn.execute(
        f"SELECT code,date,open,high,low,close,vol FROM daily_bars "
        f"WHERE code IN ({placeholders}) ORDER BY code,date", codes)
    out = {}
    for r in cur:
        code = r[0]
        if code not in out:
            out[code] = []
        out[code].append({"date": r[1], "open": r[2], "high": r[3],
                          "low": r[4], "close": r[5], "vol": r[6] or 0.0})
    return out


def _bar_ok(r):
    """K线数据结构合法性校验：剔除脏数据（缺失/非正价/高低颠倒）。
    影线比例不再作为剔除依据——低价股分值效应下正常K线会被大量误杀
    （实测老数据误杀率30%+），价格异常由 _bars_anomalous 涨跌幅校验兜底。"""
    o, h, l, c = r["open"], r["high"], r["low"], r["close"]
    if None in (o, h, l, c):
        return False
    if min(o, h, l, c) <= 0:
        return False
    if h < l:
        return False
    return True


def sanitize_daily_db() -> None:
    """历史脏数据清理（仅手动调用；缓存默认不自动清理，保持大数据量提升准头）。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code,date,open,high,low,close FROM daily_bars").fetchall()
        bad = [(c, d) for c, d, o, h, l, cl in rows
               if not _bar_ok({"open": o, "high": h, "low": l, "close": cl})]
        if bad:
            conn.executemany(
                "DELETE FROM daily_bars WHERE code=? AND date=?", bad)
            conn.commit()


def _limit_pct(code, name, date):
    """该股当日允许的涨跌幅限制(%)，无限制返回None。
    - 主板普通股10%；主板ST在2026-07-06新规实施前为5%，之后统一10%
    - 创业板2020-08-24注册制改革后20%（含ST）、科创板20%（含ST）
    - 北交所30%
    - 1996-12-16涨跌停板制度实施前不限"""
    if date < "1996-12-16":
        return None
    if code.startswith("bj"):
        return 30.0
    board = code[2:4] if len(code) >= 4 else ""
    if board == "68":              # 科创板
        return 20.0
    if board == "30":              # 创业板
        return 20.0 if date >= "2020-08-24" else 10.0
    if (name and "ST" in name.upper()
            and date < "2026-07-06"):   # 主板ST旧规5%
        return 5.0
    return 10.0


def _is_etf(code):
    """是否ETF/LOF代码：沪 51/56/58，深 15/16/18 开头。"""
    pre = code[2:4] if len(code) >= 4 else ""
    return pre in ("51", "56", "58", "15", "16", "18")


def _bars_anomalous(rows, code, name=""):
    """相邻日涨跌幅超出涨跌停允许范围即视为数据异常。

    - 个股：容差 涨跌停+3pp。
    - ETF/LOF：除权除息、份额折算/拆分会产生单根K线的大跳变
      （分红 ±10%+、折算可能 ±66%/±200%），但折算通常只影响单日，
      跳变后价格恢复连续。因此对 ETF 采用「孤立跳变放行」策略：
      只有**连续**出现超阈值大跳变（≥2次相邻）才判为数据错误；
      单次孤立大跳变视为合法除权/折算。
    - 历史不足30根的新股跳过检查。
    - 2026-09-25 误报豁免（曾把深历史整只误替换为短数据）：退市整理期整体跳过；
      序列前10根（注册制新股前5日不设限）；相邻两根日历间隔>30天（长期停牌复牌/
      退市整理首日不设限）；主板ST旧规5%按现名回溯会误伤非ST历史段，放宽到10%。
    """
    if len(rows) < 30:
        return False
    if code.startswith(("sh000", "sz399")):   # 指数不受涨跌停约束
        return False
    if "退" in (name or ""):       # 退市整理：首日不设限且数据不再用于决策
        return False
    import datetime
    is_etf = _is_etf(code)
    threshold_extra = 12.0 if is_etf else 0.0
    # 记录每根是否超阈值
    flags = []
    for idx, (prev, r) in enumerate(zip(rows, rows[1:])):
        pc = prev.get("close")
        c = r.get("close")
        if not pc or pc <= 0 or not c or c <= 0:
            flags.append(False)
            continue
        if idx < 10:               # 新股上市初段（前5个交易日不设涨跌幅）
            flags.append(False)
            continue
        try:                       # 长期停牌复牌/退市整理首日
            d0 = datetime.date.fromisoformat(prev["date"])
            d1 = datetime.date.fromisoformat(r["date"])
            if (d1 - d0).days > 30:
                flags.append(False)
                continue
        except (ValueError, KeyError, TypeError):
            pass
        lim = _limit_pct(code, name, r["date"])
        if lim is None:
            flags.append(False)
            continue
        if lim < 10.0:             # 主板ST旧规5%：现名回溯历史会误伤，放宽到10%
            lim = 10.0
        chg = abs(c / pc - 1) * 100
        flags.append(chg > lim + 3.0 + threshold_extra)
    if not any(flags):
        return False
    # 非ETF：任一超阈值即异常
    if not is_etf:
        return True
    # ETF：连续超阈值（相邻两根都异常）才判异常；孤立单次跳变放行
    for i in range(1, len(flags)):
        if flags[i] and flags[i - 1]:
            return True
    return False


# ================= 多源日K获取 =================

def _code_to_163(full):
    """腾讯代码 → 网易163代码：sh600519 → 0600519, sz002241 → 1002241"""
    if full.startswith("sh"):
        return "0" + full[2:]
    if full.startswith("sz"):
        return "1" + full[2:]
    return None


def _code_to_em(full):
    """腾讯代码 → 东财代码：sh600519 → 1.600519, sz002241 → 0.002241"""
    if full.startswith("sh"):
        return "1." + full[2:]
    if full.startswith("sz"):
        return "0." + full[2:]
    return None


def _fetch_tencent(full, count=600, host=None, fq="hfq"):
    """腾讯K线。host 可换备用域名（主域被限流时走代理域/HTTP域）。

    fq='hfq'（默认，后复权，永远为正、乘法口径）；fq='qfq' 为腾讯
    除权公式前复权（现金分红做减法，长期高分红股会趋近 0/为负——
    已证实为此前全库假跳变根因，勿再入库）；fq='' 为不复权。
    返回 rows；原始实时价从响应 qt 字段解析（_tx_last_raw）。
    """
    host = host or KLINE_URL
    param = (f"?param={full},day,,,{count},{fq}" if fq
             else f"?param={full},day,,,{count},")
    txt = _http_get(host + param, decode="utf-8", retries=1, timeout=8)
    kd = json.loads(txt)
    d = (kd.get("data") or {}).get(full) or {}
    key = {"hfq": "hfqday", "qfq": "qfqday"}.get(fq, "day")
    bars = d.get(key) or d.get("day") or []
    out = []
    for b in bars:
        try:
            if float(b[2]) <= 0:
                continue
            out.append({"date": b[0], "open": float(b[1]),
                        "close": float(b[2]), "high": float(b[3]),
                        "low": float(b[4]), "vol": float(b[5])})
        except (ValueError, IndexError):
            continue
    try:
        _tx_raw_cache[full] = _tx_quote_raw(d.get("qt"))
    except Exception:
        pass
    return out


_tx_raw_cache = {}
_LAST_RAW = {}


def _tx_quote_raw(qt):
    """从腾讯若快照 qt 里取最新原始价（不复权）。"""
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


def _raw_last_price(full):
    """最新不复权收盘价（腾讯快照优先，其次东财 fqt=0，最后缓存）。
    用于把后复权价缩放到"乘法前复权"显示口径。失败返回 None。"""
    p = _tx_raw_cache.get(full)
    if p:
        return p
    try:
        rows = _fetch_tencent(full, count=5, fq="")
        if rows:
            return rows[-1]["close"]
    except Exception:
        pass
    try:
        rows = _fetch_eastmoney(full, count=5, fqt=0)
        if rows:
            return rows[-1]["close"]
    except Exception:
        pass
    v = _LAST_RAW.get(full)
    return v


def _set_adjust(full, k):
    """记录显示缩放系数 K=真实现价/后复权末价，供 get_daily 转乘法前复权。"""
    if not k or k <= 0:
        return
    try:
        with db_conn(commit=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS adjust("
                         "code TEXT PRIMARY KEY, k REAL, ts REAL)")
            conn.execute("INSERT OR REPLACE INTO adjust VALUES(?,?,?)",
                         (full, float(k), time.time()))
    except Exception:
        log.exception("adjust 写入失败 %s", full)


def _get_adjust(full):
    try:
        with db_conn() as conn:
            r = conn.execute("SELECT k FROM adjust WHERE code=?",
                             (full,)).fetchone()
        return float(r[0]) if r and r[0] else None
    except Exception:
        return None


def _sync_adjust(full, rows):
    """按「同一交易日」配对刷新显示缩放系数 k=raw_close/hfq_close。

    修复 2026-09-16：旧实现用实时价配库内最后 hfq bar，若日K落后数日
    （盘中/未回填），会把整段历史价格按「最新价/落后日价」缩放错
    （如京东方A显示昨收=今日价）。现在优先用远端不复权日K与 hfq 日K
    的同一日期配对；仅当 hfq 末 bar 就是今天时才允许用实时价兜底。"""
    if not rows:
        return
    try:
        raw_map = {}
        try:
            for r in (_fetch_tencent(full, count=6, fq="") or []):
                if r.get("close") and r.get("date"):
                    raw_map[r["date"]] = r["close"]
        except Exception:
            pass
        k = None
        for r in reversed(rows[-8:]):
            rc = raw_map.get(r["date"])
            if rc and r.get("close") and r["close"] > 0:
                k = rc / r["close"]
                break
        if k is None:
            raw = _raw_last_price(full)
            last = rows[-1]["close"]
            if (raw and last and last > 0
                    and rows[-1]["date"] == time.strftime("%Y-%m-%d")):
                k = raw / last
        if k and k > 0:
            _set_adjust(full, k)
    except Exception:
        log.debug("sync_adjust 失败 %s", full, exc_info=True)


def _fetch_163(full, count=600):
    """网易163财经（免费，稳定性好，返回CSV）"""
    code163 = _code_to_163(full)
    if not code163:
        return []
    import datetime
    end = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=count * 2)
             ).strftime("%Y%m%d")
    url = (f"http://quotes.money.163.com/service/chddata.html"
           f"?code={code163}&start={start}&end={end}"
           f"&fields=TCLOSE;HIGH;LOW;TOPEN;VOTURNOVER")
    txt = _http_get(url, retries=2, timeout=15, decode="gbk")
    out = []
    for line in txt.strip().split("\n"):
        if not line.strip() or line.startswith("日期"):
            continue
        parts = line.strip().split(",")
        if len(parts) < 7:
            continue
        try:
            date = parts[0].strip().strip("'")
            close = float(parts[3]) if parts[3].strip() else 0
            high = float(parts[4]) if parts[4].strip() else 0
            low = float(parts[5]) if parts[5].strip() else 0
            opn = float(parts[6]) if parts[6].strip() else 0
            vol = float(parts[11]) if len(parts) > 11 and parts[11].strip() else 0
            if close <= 0:
                continue
            out.append({"date": date, "open": opn, "close": close,
                        "high": high, "low": low, "vol": vol})
        except (ValueError, IndexError):
            continue
    out.reverse()  # 网易返回倒序，翻转
    return out[-count:]


def _fetch_eastmoney(full, count=600, fqt=2):
    """东方财富K线（免费，JSON格式）。多 host 轮询防限流。

    fqt: 0=不复权, 1=东财前复权(除权公式/减法，高分红股会为负，勿入库),
    2=后复权(默认)。"""
    secid = _code_to_em(full)
    if not secid:
        return []
    hosts = ("push2his.eastmoney.com",
             "92.push2his.eastmoney.com",
             "93.push2his.eastmoney.com",
             "97.push2his.eastmoney.com")
    last_err = None
    for host in hosts:
        url = (f"https://{host}/api/qt/stock/kline/get"
               f"?secid={secid}&fields1=f1,f2,f3"
               f"&fields2=f51,f52,f53,f54,f55,f56"
               f"&klt=101&fqt={fqt}&beg=0&end=20500101&lmt={count}")
        try:
            txt = _http_get(url, retries=2, timeout=20,
                            headers={"Referer": "https://quote.eastmoney.com/"})
            kd = json.loads(txt)
            klines = (kd.get("data") or {}).get("klines") or []
            out = []
            for line in klines:
                parts = line.split(",")
                if len(parts) < 6:
                    continue
                try:
                    close = float(parts[2])
                    if close <= 0:
                        continue
                    out.append({"date": parts[0], "open": float(parts[1]),
                                "close": close, "high": float(parts[3]),
                                "low": float(parts[4]), "vol": float(parts[5])})
                except (ValueError, IndexError):
                    continue
            if out:
                # 东财 lmt 实际返回全量，取最近 count 根
                return out[-count:]
            last_err = RuntimeError("东财返回空K线")
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"东财所有host均失败: {last_err}")


def _fetch_sina(full, count=600):
    """新浪财经K线（免费，稳定，返回JSON）"""
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/"
           f"json_v2.php/CN_MarketData.getKLineData"
           f"?symbol={full}&scale=240&ma=no&datalen={count}")
    txt = _http_get(url, retries=2, timeout=15, decode="utf-8",
                   headers={"Referer": "https://finance.sina.com.cn/"})
    # 新浪返回的不是标准JSON（key没引号），做简单修复
    import re
    txt = re.sub(r'(?<=[{,])(\w+):', r'"\1":', txt)
    bars = json.loads(txt)
    out = []
    for b in bars:
        try:
            close = float(b.get("close", 0))
            if close <= 0:
                continue
            out.append({"date": b["day"], "open": float(b["open"]),
                        "close": close, "high": float(b["high"]),
                        "low": float(b["low"]),
                        "vol": float(b.get("volume", 0))})
        except (ValueError, KeyError):
            continue
    return out


def _fetch_remote_rows(full, count=600, info=None):
    """多源自动切换 + 熔断调度：腾讯(配置域) → 腾讯代理 → 腾讯ifzq
    → 腾讯HTTP → 东财。

    2026-09 实测：web.ifzq.gtimg.cn 对 hfq 返回 501（腾讯反爬），
    稳定可用域为 proxy.finance.qq.com 与 ifzq.gtimg.cn；东财 push2his
    连接重置/502 属时段性故障，熔断后自动降级。
    运行逻辑：
    1. 按优先级遍历数据源，跳过处于熔断冷却期的源；
    2. 若所有源都在冷却（极端503风暴），退化为「半开探测」：
       选冷却结束最早的源强行试一次，成功即重置熔断；
    3. 单次调用内只对一个源做至多2次限流重试，
       失败立刻切下一源，避免整体请求被单源拖死。
    info：可选 dict，返回实际命中源（info["src"]）与尝试序列（info["tries"]）。"""
        # 注意：只使用后复权(hfq)源。163/新浪只提供不复权(或减法前复权)，
    # 与库内后复权口径混用会产生假跳变，不再作为持久化源。
    sources = [
        ("腾讯", lambda: _fetch_tencent(full, count)),
        ("腾讯代理", lambda: _fetch_tencent(
            full, count,
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/"
            "fqkline/get")),
        ("腾讯ifzq", lambda: _fetch_tencent(
            full, count,
            "https://ifzq.gtimg.cn/appstock/app/fqkline/get")),
        ("腾讯HTTP", lambda: _fetch_tencent(
            full, count,
            "http://ifzq.gtimg.cn/appstock/app/fqkline/get")),
        ("东财", lambda: _fetch_eastmoney(full, count)),
    ]
    last_err = None
    usable = [(n, f) for n, f in sources if _cb_ok(n)]
    if len(usable) < len(sources):
        flog.debug("%s 熔断跳过源: %s", full,
                   [n for n, _ in sources if not _cb_ok(n)])
    if not usable:
        # 半开探测：挑最早解禁的源
        probe = min(sources,
                    key=lambda nf: _SRC_CB.get(nf[0], [0, 0.0])[1])
        usable = [probe]
        flog.info("%s 全源熔断，半开探测 %s", full, probe[0])
    tried = []
    saw_soft = False            # 源正常应答但该股无数据（新股/退市）
    saw_hard = False            # 网络/限流类失败
    for name, fetcher in usable:
        tried.append(name)
        try:
            rows = fetcher()
            _cb_record(name, True)
            # 新股可能只有几根K线：>=5 即视为有效（分析层另有30根门槛）
            if rows and len(rows) >= 5:
                if info is not None:
                    info["src"], info["tries"] = name, tried
                if len(tried) > 1:
                    flog.info("%s 首源失败，%s 接管（尝试序列 %s）",
                              full, name, tried)
                return rows
            saw_soft = True
            last_err = RuntimeError(f"{name}返回空K线")
            flog.debug("%s 源%s返回空K线", full, name)
        except Exception as e:
            if "空K线" in str(e) or "空数据" in str(e):
                saw_soft = True
            else:
                saw_hard = True
            _cb_record(name, False, e)
            last_err = e
            flog.debug("%s 源%s失败: %s", full, name, e)
            continue
    # 只有"确实连不上源"才自愈/AI找源；全源正常应答但无此代码数据
    # （新 ETF 未上市、退市股）不应改写 K 线源配置（2026-09-26 修复）。
    if saw_hard and not saw_soft:
        _auto_heal_kline()      # 全灭时自动探测候选域并切换
        _maybe_ai_rescue()      # 仍无解：弹窗询问是否让AI找源(GUI)
    if info is not None:
        info["tries"] = tried
    raise RuntimeError(f"所有数据源均失败: {last_err}")


FAIL_TTL = 3600                 # 拉取失败记忆期1小时
_MIN_INTERVAL = 0.16            # 全局HTTP最小间隔，防腾讯限速
_THROTTLE_LOCK = threading.Lock()
_LAST_REQ = [0.0]


# ---- K线源自动容灾（急救箱逻辑内嵌，无人工参与）----
_KLINE_HEAL_TS = [0.0]          # 上次自愈探测时间（10分钟限频）
_KLINE_HEAL_LOCK = threading.Lock()
_KLINE_DEFAULTS = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "http://ifzq.gtimg.cn/appstock/app/fqkline/get",
)


def _probe_kline_url(base):
    """实测一个K线接口是否真的有数据（近5根**后复权**日K + JSON合法）。

    生产入库口径是 hfq：部分腾讯域对 qfq 正常但对 hfq 返回 501，
    若用 qfq 探测会把"持久化不可用"的域名写进配置（2026-09-26 实测
    web.ifzq 就是这种域）。连测2次都成功才判活，避免瞬时抖动误判。"""
    for _ in range(2):
        try:
            txt = _http_get(base + "?param=sz002241,day,,,5,hfq",
                            retries=1, timeout=6)
            d0 = (json.loads(txt).get("data") or {}).get("sz002241") or {}
            bars = d0.get("hfqday") or d0.get("day") or []
            if len(bars) >= 5:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _persist_kline_url(u):
    """把可用K线源写入内存与 ini（重启后仍生效）。"""
    globals()["KLINE_URL"] = u
    try:
        cp = configparser.ConfigParser()
        cp.read(INI_PATH, encoding="utf-8")
        if not cp.has_section("data"):
            cp.add_section("data")
        cp.set("data", "kline_url", u)
        cp.set("data", "kline_url_updated", time.strftime("%Y-%m-%d %H:%M"))
        with open(INI_PATH, "w", encoding="utf-8") as f:
            cp.write(f)
    except Exception:
        log.exception("kline_url 持久化失败(忽略)")


def _auto_heal_kline():
    """全部K线源失败时自动探测候选域并切换（内存+ini持久化）。
    10分钟限频，探测失败不影响原流程。"""
    with _KLINE_HEAL_LOCK:
        if time.time() - _KLINE_HEAL_TS[0] < 600:
            return False
        _KLINE_HEAL_TS[0] = time.time()
    cands = [u for u in _KLINE_DEFAULTS if u != KLINE_URL]
    for u in cands:
        if _probe_kline_url(u):
            _persist_kline_url(u)
            log.warning("K线源自动切换: %s", u)
            return True
    return False


# ---- AI 找源（需用户弹窗确认；AI只提议URL，程序实测验证后才启用）----
_AI_RESCUE_HOOK = None          # App.__init__ 注册的GUI确认回调
_AI_RESCUE_TS = [0.0]           # 30分钟内不重复触发
_AI_RESCUE_LOCK = threading.Lock()
_AI_URL_RE = re.compile(r"https?://[^\s\"'<>)\\]]+", re.I)


def ai_rescue_kline(api_key, model=None):
    """让 DeepSeek 提议候选K线接口URL，逐个实测验证，采用第一个通过者。
    返回 (ok, 提示消息)。绝不执行模型输出的代码，只做 GET 探测。"""
    import re as _re
    if not api_key:
        return False, "未配置DeepSeek Key，无法AI找源"
    prompt = (
        "我有一个A股工具，所有已知K线数据接口都失效了。"
        f"请给我最多5个【可以直接HTTP GET】获取A股 sz002241 日K线数据的"
        "候选接口完整URL（免费、无需key，返回JSON或CSV均可），"
        "一行一个URL，不要解释，不要markdown代码块，只输出URL列表。")
    try:
        txt = deepseek_chat(api_key, prompt, model=model, timeout=60,
                            session=_ai_session_id("kline-rescue"))
    except Exception as e:
        return False, f"AI调用失败: {e}"
    urls = []
    for u in _AI_URL_RE.findall(txt or ""):
        u = u.rstrip(".,;，。；")
        if u not in urls:
            urls.append(u)
    urls = urls[:5]
    if not urls:
        return False, "AI未给出可用URL"
    log.info("AI找源: 验证 %d 个候选", len(urls))
    for u in urls:
        if _probe_kline_url(u):
            _persist_kline_url(u)
            log.warning("K线源已采用AI提议: %s", u)
            return True, f"已采用AI提议源: {u}"
    return False, f"AI提议{len(urls)}个均未通过验证(已丢弃)"


def _maybe_ai_rescue():
    """全灭后触发AI找源确认弹窗（经App注册的钩子转到主线程）。
    30分钟限频；无钩子（CLI）或无Key时只在日志提示。"""
    global _AI_RESCUE_HOOK
    with _AI_RESCUE_LOCK:
        if time.time() - _AI_RESCUE_TS[0] < 1800:
            return False
        _AI_RESCUE_TS[0] = time.time()
    if _AI_RESCUE_HOOK is None:
        log.info("所有K线源失效；可运行 python stock_firstaid.py --ai 手动找源")
        return False
    try:
        _AI_RESCUE_HOOK()
    except Exception:
        log.exception("AI找源弹窗触发失败")
    return True


def _display_rows(full, rows, tail=None):
    """把库内后复权(hfq)价格按 adjust 系数转成"乘法前复权"供展示/分析。

    收益率在两种口径下完全一致；仅价格水平不同。无 adjust 记录时原样返回。"""
    k = _get_adjust(full)
    if k and k > 0 and rows:
        rows = [{"date": r["date"], "open": r["open"] * k,
                 "high": r["high"] * k, "low": r["low"] * k,
                 "close": r["close"] * k, "vol": r["vol"]} for r in rows]
    return rows[-tail:] if (tail and len(rows) > tail) else rows


def _record_fail(full, reason):
    """写入负缓存（prefetch 按 TTL 跳过；反复失败 2 倍退避，上限 24h）。

    用于两类"拉不到更新"：网络失败、以及源侧停牌/退市导致末日不前进
    （2026-09-26：后者此前每小时都重拉一次，长停牌股每次几百根）。"""
    new_ttl = FAIL_TTL
    try:
        with db_conn() as conn:
            row = conn.execute("SELECT ts FROM failed WHERE code=?",
                               (full,)).fetchone()
        if row:
            prev_ttl = max(FAIL_TTL - (time.time() - row[0]), FAIL_TTL)
            new_ttl = min(prev_ttl * 2, 86400)
    except Exception:
        log.exception("负缓存退避计算失败(忽略)")
    with db_conn(commit=True) as conn:
        # 存 ts 使 ts+FAIL_TTL = now+new_ttl，无需改表结构
        conn.execute(
            "INSERT OR REPLACE INTO failed(code,ts,reason) VALUES(?,?,?)",
            (full, time.time() - (FAIL_TTL - new_ttl), str(reason)[:120]))


def get_daily(full: str, min_bars: int = 100, tail=None):
    """带缓存的日K：本地够新且无异常直接返回，否则增量爬一次并入库。
    加载缓存后校验每日涨跌幅是否超出该股允许的涨跌停范围，
    数据异常则删除本地缓存全量重新下载。
    有缓存数据的股票永远返回数据（即使过期），不抛异常。
    只有从未成功获取过的代码才会触发网络请求和负缓存。
    tail: 非空时只返回最近 tail 根（用于启动快速预览，走缓存秒开）。
    库内一律存后复权(hfq)；返回前按 adjust 缩放为乘法前复权显示。
    2026-09-26：缓存判定/拉取/来源写入 stock_fetch.log（见 flog）。"""
    _fstat_inc("calls")
    today = time.strftime("%Y-%m-%d")
    # 指数用"收盘后才有今日"口径（自身是日历锚），个股用节假日安全口径
    fresh = _index_expected_td() if _is_index_code(full) \
        else last_completed_td()
    allow_today = _allow_today_bar()
    with db_conn() as conn:
        rows = _db_rows(conn, full)
        nrow = conn.execute("SELECT name FROM stocks WHERE code=?",
                            (full,)).fetchone()
    name = nrow[0] if nrow else ""
    bad_cache = bool(rows) and _bars_anomalous(rows, full, name)
    # 1) 有数据、够新且涨跌幅无异常 → 直接返回
    if rows and rows[-1]["date"] >= fresh and not bad_cache:
        _fstat_inc("hit")
        _fstat_log()
        return _display_rows(full, rows, tail)
    # 2) 有数据但过期或涨幅异常 → 拉远端（异常时清空全量替换）
    if rows:
        old_last = rows[-1]["date"]
        reason = ("异常" if bad_cache
                  else f"过期(本地末日{old_last}<应有{fresh})")
        _fstat_inc("anomaly" if bad_cache else "stale")
        t_pull = time.time()
        info = {}
        if _is_index_code(full):
            _INDEX_PULL_TS[0] = t_pull
        flog.info("拉取 %s 原因=%s 本地=%d根(末日%s)",
                  full, reason, len(rows), old_last)
        try:
            remote = [r for r in _fetch_remote_rows(
                full, count=CFG.MAX_FETCH_BARS, info=info)
                      if (r["date"] < today
                          or (allow_today and r["date"] == today))
                      and _bar_ok(r)]
            # 基期一致性校验：前复权序列每次分红整体重定基，增量合并会在
            # 缓存接缝处留下人造跳空。重叠日期收盘价偏差>0.5% → 全量替换。
            rebase = False
            if rows and remote:
                newmap = {r["date"]: r["close"] for r in remote}
                checked = 0
                for r in rows[-200:]:
                    c2 = newmap.get(r["date"])
                    if c2 and r["close"]:
                        checked += 1
                        if abs(c2 / r["close"] - 1) > 0.005:
                            rebase = True
                            break
                rebase = rebase and checked >= 5
            with db_conn(commit=True) as conn2:
                # 只有新数据足够长才允许全量替换，防止短源摧毁深历史。
                # bad_cache 存在误报（新股/停牌/ST历史）：必须覆盖90%才清空；
                # rebase（复权基期真变了）维持原「≥缓存一半」口径，保证口径统一。
                cover = (max(400, int(len(rows) * 0.9)) if bad_cache
                         else min(400, len(rows) // 2))
                if (bad_cache or rebase) and len(remote) >= cover:
                    conn2.execute("DELETE FROM daily_bars WHERE code=?",
                                  (full,))
                if remote:
                    conn2.executemany(
                        "INSERT OR REPLACE INTO daily_bars"
                        "(code,date,open,high,low,close,vol) "
                        "VALUES(?,?,?,?,?,?,?)",
                        [(full, r["date"], r["open"], r["high"], r["low"],
                          r["close"], r["vol"]) for r in remote])
            if rebase:
                log.info("%s 前复权基期漂移，已全量重定基", full)
            # 重新读取合并后的数据
            with db_conn() as conn3:
                rows = _db_rows(conn3, full)
            _sync_adjust(full, rows)
            _fstat_inc("pull_ok")
            _fstat_inc("pull_rows", len(remote))
            new_last = rows[-1]["date"] if rows else ""
            flog.info("入库 %s 源=%s 采用=%d根 合并后=%d根(末日%s) 耗时=%.0fms",
                      full, info.get("src") or "-", len(remote), len(rows),
                      new_last or "-", (time.time() - t_pull) * 1000)
            # 源侧也没更新（停牌/退市整理/新退市）：记负缓存，避免每小时重拉
            if (not _is_index_code(full) and new_last == old_last
                    and new_last < fresh):
                _record_fail(full, f"源无更新({old_last})")
                flog.info("无新数据 %s 末日=%s<应有%s（负缓存，预取将跳过）",
                          full, old_last, fresh)
        except Exception:
            _fstat_inc("pull_fail")
            log.warning("get_daily 增量拉取失败 %s，回退本地缓存",
                        full, exc_info=True)  # 网络失败就用旧缓存，不报错
        _fstat_log()
        return _display_rows(full, rows, tail)
    # 3) 无数据 → 检查负缓存
    _fstat_inc("empty")
    with db_conn() as conn:
        frow = conn.execute("SELECT ts, reason FROM failed WHERE code=?",
                            (full,)).fetchone()
        if frow and time.time() - frow[0] < FAIL_TTL:
            reason = (frow[1] or "网络失败") if len(frow) > 1 else "网络失败"
            _fstat_inc("neg")
            flog.info("跳过 %s 负缓存剩余%.0f分钟: %s", full,
                      (FAIL_TTL - (time.time() - frow[0])) / 60, reason)
            raise RuntimeError(f"{full} 近期拉取失败(负缓存中) [{reason}]")

    # 4) 从未获取过 → 网络请求
    t_pull = time.time()
    info = {}
    flog.info("拉取 %s 原因=无缓存", full)
    try:
        remote = _fetch_remote_rows(full, count=CFG.MAX_FETCH_BARS, info=info)
    except Exception as e:
        # 未上市/无数据识别：行情快照也拿不到有效价 → 静默记为未上市
        listed = True
        try:
            qq = fetch_quote(full)
            listed = bool(qq and qq.get("price") and qq["price"] > 0)
        except Exception:
            listed = False
        if not listed:
            log.info("%s 未上市或无行情数据，跳过", full)
            with db_conn(commit=True) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO failed(code,ts,reason) "
                    "VALUES(?,?,?)",
                    (full, time.time(), "未上市或无数据"))
            raise RuntimeError(f"{full} 未上市或无行情数据")
        _fstat_inc("pull_fail")
        log.warning("get_daily 首次拉取失败 %s: %s", full, e)
        flog.warning("拉取失败 %s 原因=无缓存 源尝试=%s: %s", full,
                     info.get("tries") or "-", e)
        _record_fail(full, e)
        raise
    with db_conn(commit=True) as conn:
        conn.execute("DELETE FROM failed WHERE code=?", (full,))
        conn.executemany(
            "INSERT OR REPLACE INTO daily_bars"
            "(code,date,open,high,low,close,vol) VALUES(?,?,?,?,?,?,?)",
            [(full, r["date"], r["open"], r["high"], r["low"],
              r["close"], r["vol"]) for r in remote
             if (r["date"] < today or (_allow_today_bar()
                                       and r["date"] == today))
             and _bar_ok(r)])
        rows = [r for r in _db_rows(conn, full)]
    _sync_adjust(full, rows)
    _fstat_inc("pull_ok")
    _fstat_inc("pull_rows", len(remote))
    flog.info("入库 %s 源=%s 远端=%d根 合并后=%d根(末日%s) 耗时=%.0fms",
              full, info.get("src") or "-", len(remote), len(rows),
              rows[-1]["date"] if rows else "-",
              (time.time() - t_pull) * 1000)
    _fstat_log()
    return _display_rows(full, rows, tail)


def prefetch(codes, workers=6, progress=None):
    """并发预取一批代码的日K入库（首次回填用）。"""
    # 先排除已知失败的代码（10分钟内不再重试），记录原因
    with db_conn() as conn:
        now = time.time()
        failed = {r[0] for r in
                  conn.execute("SELECT code FROM failed WHERE ts > ?",
                               (now - FAIL_TTL,)).fetchall()}
        fail_reasons = {}
        try:
            for r in conn.execute(
                    "SELECT code, reason FROM failed WHERE ts > ?",
                    (now - FAIL_TTL,)).fetchall():
                fail_reasons[r[0]] = r[1] or "网络失败"
        except Exception:
            log.exception("failed 表读取失败(忽略)")
    codes = [c for c in codes if c not in failed]
    if failed and progress:
        # 显示前3个失败原因，避免刷屏
        sample = [f"{c}({fail_reasons.get(c, '网络失败')})"
                  for c in list(failed)[:3]]
        progress(f"跳过{len(failed)}个近期失败代码: " + "; ".join(sample))
    done = [0]
    total = len(codes)
    if total == 0:
        flog.info("预取: 目标=0 全部跳过（负缓存%d）", len(failed))
        return
    flog.info("预取: 目标=%d 跳过负缓存=%d", total, len(failed))
    # 进度上报粒度：大批量约每5%报一次，小批量每只都报
    step = max(1, min(10, total // 20 or 1))

    def one(c):
        try:
            get_daily(c)
        except Exception:
            log.debug("prefetch 跳过 %s", c, exc_info=True)
        done[0] += 1
        if progress and (done[0] % step == 0 or done[0] == total):
            progress(f"缓存回填 {done[0]}/{total} "
                     f"({done[0] * 100 // total}%)")

    ex = _SHARED_EX                # 全局共享线程池，不再每次新建
    list(ex.map(one, codes))
    flog.info("预取完成: %d 只", total)


def stale_codes(limit=None, skip_bj=True, skip_delisted=True):
    """返回库内K线尚未更新到最新应有交易日（last_completed_td）的代码。

    只查 stocks/daily_bars（不联网），供后台主动预取挑选目标：
    收盘（15:05）后返回全市场，用于当日K线回补；盘中只返回缺昨日数据的。
    指数按"收盘后才有今日"口径判断，保证交易日历锚能推进又不空拉。
    skip_bj：排除北交所（与样本池/研究口径一致）；
    skip_delisted：已有数据但最后K线早于180天视为退市，不再重试。"""
    import datetime
    fresh = last_completed_td()
    fresh_idx = _index_expected_td()
    cutoff = _dstr(datetime.date.today() - datetime.timedelta(days=180))
    with db_conn() as conn:
        codes = [r[0] for r in conn.execute("SELECT code FROM stocks")]
        have = dict(conn.execute(
            "SELECT code, MAX(date) FROM daily_bars "
            "GROUP BY code").fetchall())
    out = []
    n_nodata = n_old = n_delisted = n_bj = 0
    for c in codes:
        if skip_bj and c.startswith("bj"):
            n_bj += 1
            continue
        d = have.get(c)
        fr = fresh_idx if _is_index_code(c) else fresh
        if d is None:                   # 无缓存：可能新股，值得拉
            n_nodata += 1
            out.append(c)
        elif d < fr:
            if skip_delisted and d < cutoff:
                n_delisted += 1
            else:
                n_old += 1
                out.append(c)
    flog.info("全库新鲜度扫描: 个股应有=%s 指数应有=%s 对象=%d 北交跳过=%d "
              "无缓存=%d 过期=%d 退市跳过=%d → 待回补=%d",
              fresh, fresh_idx, len(codes), n_bj, n_nodata, n_old,
              n_delisted, len(out))
    return out[:limit] if limit else out


# ================= 全市场深历史回填（集成版，原 backfill_full.py） =================
# 腾讯单次上限800根 → 两页翻取1600根(≥1000目标)；三域名轮换；
# 免费源有IP配额(东财批量~500只断连、腾讯每窗口~200请求501)：
# 501 全局暂停自愈 + 断点续传，数晚跑满全市场。CLI: --backfill

_BF_TX_HOSTS = [
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
]
_BF_TX_I = [0]
_BF_PAUSE = [0.0, 0]            # [暂停截止时间, 连续限流次数]


def _bf_tx_fetch(full, end, count):
    """腾讯K线单页（后复权 hfq）：三域名轮换，全部501才抛（触发全局暂停）。"""
    param = (f"?param={full},day,,{end},{count},hfq" if end
             else f"?param={full},day,,,{count},hfq")
    last = None
    for k in range(len(_BF_TX_HOSTS)):
        u = _BF_TX_HOSTS[(_BF_TX_I[0] + k) % len(_BF_TX_HOSTS)]
        try:
            txt = _http_get(u + param, decode="utf-8", retries=1, timeout=8)
            _BF_TX_I[0] = (_BF_TX_I[0] + k + 1) % len(_BF_TX_HOSTS)
            d = (json.loads(txt).get("data") or {}).get(full) or {}
            # 请求的是 hfq（后复权），腾讯返回 hfqday；指数无复权返回 day
            bars = d.get("hfqday") or d.get("day") or []
            out = []
            for b in bars:
                try:
                    if float(b[2]) <= 0:
                        continue
                    out.append({"date": b[0], "open": float(b[1]),
                                "close": float(b[2]), "high": float(b[3]),
                                "low": float(b[4]), "vol": float(b[5])})
                except (ValueError, IndexError):
                    continue
            return out
        except Exception as e:
            last = e
    raise last


def _bf_fetch_one(full, page=800, target=None):
    """单只：腾讯翻页为主源（后复权），东财全量兜底。返回 (rows, raw_last)。

    target: 目标根数（默认 max(CFG.MAX_FETCH_BARS, 2页)）；按需继续翻页直到
    达到目标或源侧没有更早数据（2000 根需 3 页）。"""
    import datetime
    target = target or max(CFG.MAX_FETCH_BARS, page * 2)
    rows1 = _bf_tx_fetch(full, "", page)
    if not rows1:
        raise RuntimeError("腾讯空数据")
    have = {r["date"] for r in rows1}
    pages = 1
    while len(rows1) < target and pages < 5 and len(rows1) >= page - 10:
        d0 = datetime.date.fromisoformat(rows1[0]["date"])
        end = (d0 - datetime.timedelta(days=1)).isoformat()
        try:
            older = _bf_tx_fetch(full, end, page)
        except Exception:
            break                               # 更早一页失败就保留已有
        new = [r for r in older if r["date"] not in have]
        if not new:
            break                               # 源侧已无更早数据
        rows1 = new + rows1
        have.update(r["date"] for r in new)
        pages += 1
    if len(rows1) >= 300:
        return rows1, _raw_last_price(full)
    try:
        rows = _fetch_eastmoney(full, count=1100)
        if len(rows) >= 300:
            return rows, _raw_last_price(full)
    except Exception:
        pass
    raise RuntimeError("有效数据不足300根")


def backfill_full_market(progress=None, force=False, limit=0,
                         workers=6, throttle=0.45, min_bars=None):
    """全市场日K批量回填（≥min_bars根，断点续传）。返回统计dict。

    min_bars 缺省 = max(950, CFG.MAX_FETCH_BARS)：设置页「最大拉取样本量」调到
    2000 时，回填会自动翻到 3 页（≈2400 根上限）直到 2000 根目标。"""
    min_bars = min_bars or max(950, CFG.MAX_FETCH_BARS)
    global _MIN_INTERVAL
    old_iv = _MIN_INTERVAL
    _MIN_INTERVAL = throttle
    try:
        today = time.strftime("%Y-%m-%d")
        import datetime
        fresh = (datetime.date.today()
                 - datetime.timedelta(days=6)).isoformat()
        with db_conn() as conn:
            codes = [r[0] for r in conn.execute(
                "SELECT code FROM stocks WHERE code NOT LIKE 'bj%' "
                "ORDER BY code").fetchall()]
            if not codes:
                refresh_all_codes(progress=progress)
                with db_conn() as conn:
                    codes = [r[0] for r in conn.execute(
                        "SELECT code FROM stocks WHERE code NOT LIKE 'bj%' "
                        "ORDER BY code").fetchall()]
        if limit:
            codes = codes[:limit]
        have = {}
        if not force:
            with db_conn() as conn:
                for c, n, d in conn.execute(
                        "SELECT code, COUNT(*), MAX(date) FROM daily_bars "
                        "GROUP BY code"):
                    have[c] = (n, d or "")
        todo = [c for c in codes
                if not (have.get(c, (0, ""))[0] >= min_bars
                        and have.get(c, (0, ""))[1] >= fresh)]
        stat = {"total": len(codes), "todo": len(todo), "ok": 0,
                "fail": 0, "codes": len(have)}
        if not todo:
            if progress:
                progress(f"回填：全市场已达标，无需继续")
            return stat
        if progress:
            progress(f"回填：待处理 {len(todo)}/{len(codes)} 只")
        t0 = time.time()
        done_n = [0]

        def work(c):
            if time.time() < _BF_PAUSE[0]:
                time.sleep(_BF_PAUSE[0] - time.time())
            try:
                fetched, raw_last = _bf_fetch_one(c, target=min_bars)
                rows = [r for r in fetched
                        if (r["date"] < today
                            or (_allow_today_bar() and r["date"] == today))
                        and _bar_ok(r)]
                if not rows:
                    raise RuntimeError("过滤后无有效数据")
                # 基期一致性：与库内重叠收盘偏差>0.5% 说明旧数据是别的
                # 复权口径（或旧口径污染），此时不能逐条 upsert，须整只替换
                with db_conn() as conn:
                    old = conn.execute(
                        "SELECT date, close FROM daily_bars WHERE code=? "
                        "ORDER BY date", (c,)).fetchall()
                newmap = {r["date"]: r["close"] for r in rows}
                mismatch, checked = False, 0
                for d, cl in old[-200:]:
                    c2 = newmap.get(d)
                    if c2 and cl:
                        checked += 1
                        if abs(c2 / cl - 1) > 0.005:
                            mismatch = True
                            break
                if checked < 5:
                    mismatch = False
                if mismatch:
                    full = []
                    try:
                        full = _fetch_eastmoney(c, count=8000)
                    except Exception:
                        full = []
                    if len(full) >= 200 and len(full) >= min(len(old), 400):
                        conn_rows = [r for r in full if r["date"] < today
                                     and _bar_ok(r)]
                        with db_conn(commit=True) as conn:
                            conn.execute(
                                "DELETE FROM daily_bars WHERE code=?", (c,))
                            conn.executemany(
                                "INSERT OR REPLACE INTO daily_bars"
                                "(code,date,open,high,low,close,vol) "
                                "VALUES(?,?,?,?,?,?,?)",
                                [(c, r["date"], r["open"], r["high"],
                                  r["low"], r["close"], r["vol"])
                                 for r in conn_rows])
                        stat["replaced"] = stat.get("replaced", 0) + 1
                    elif len(old) <= len(rows):
                        with db_conn(commit=True) as conn:
                            conn.execute(
                                "DELETE FROM daily_bars WHERE code=?", (c,))
                            conn.executemany(
                                "INSERT OR REPLACE INTO daily_bars"
                                "(code,date,open,high,low,close,vol) "
                                "VALUES(?,?,?,?,?,?,?)",
                                [(c, r["date"], r["open"], r["high"],
                                  r["low"], r["close"], r["vol"])
                                 for r in rows])
                        stat["replaced"] = stat.get("replaced", 0) + 1
                    else:
                        stat["need_migrate"] = stat.get("need_migrate", 0) + 1
                else:
                    with db_conn(commit=True) as conn:
                        conn.executemany(
                            "INSERT OR REPLACE INTO daily_bars"
                            "(code,date,open,high,low,close,vol) "
                            "VALUES(?,?,?,?,?,?,?)",
                            [(c, r["date"], r["open"], r["high"], r["low"],
                              r["close"], r["vol"]) for r in rows])
                if raw_last and rows[-1]["close"] > 0:
                    _set_adjust(c, raw_last / rows[-1]["close"])
                _BF_PAUSE[1] = 0
                stat["ok"] += 1
            except Exception as e:
                msg = str(e)
                if "501" in msg or "429" in msg or "503" in msg:
                    _BF_PAUSE[1] = min(_BF_PAUSE[1] + 1, 4)
                    wait = 120 if _BF_PAUSE[1] < 3 else 300
                    _BF_PAUSE[0] = max(_BF_PAUSE[0], time.time() + wait)
                stat["fail"] += 1
            done_n[0] += 1
            if progress and (done_n[0] % 20 == 0
                             or done_n[0] == len(todo)):
                el = time.time() - t0
                eta = el / done_n[0] * (len(todo) - done_n[0])
                progress(f"全市场回填 {done_n[0]}/{len(todo)} "
                         f"({done_n[0] * 100 // len(todo)}%) "
                         f"成功{stat['ok']} 失败{stat['fail']} "
                         f"ETA {eta / 60:.0f}分")

        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=workers) as ex2:
            list(ex2.map(work, todo))
        if progress:
            progress(f"回填完成：成功{stat['ok']} 失败{stat['fail']}"
                     f"（失败的下次运行自动续传）")
        return stat
    finally:
        _MIN_INTERVAL = old_iv


# ================= 数据清洗（集成版；独立版见 data_clean.py） =================

def _clean_bar_valid(r):
    """单根bar结构校验（只查硬错误，不查影线比例——低价股分值效应会误杀）。"""
    o, h, l, c = r[1], r[2], r[3], r[4]
    if None in (o, h, l, c) or min(x for x in (o, h, l, c)) <= 0:
        return False
    if h < l or h < max(o, c) or l > min(o, c):
        return False
    return True


def clean_daily_db(fix=True, progress=None):
    """扫描并（可选）修复全库日K：结构异常/涨跌幅越界(除权残留)/
    停牌缺口/退市/价格粘性。返回统计dict。与 data_clean.py 同规则。"""
    import datetime as _dt
    _today = _dt.date.today()
    stats = {"codes": 0, "bad_bars": 0, "refetch": 0, "suspend": 0,
             "delisted": 0, "stale": 0, "deleted": 0, "refetched": 0}
    issues = {}
    with db_conn(commit=bool(fix)) as conn:
        names = {r[0]: (r[1] or "") for r in
                 conn.execute("SELECT code, name FROM stocks").fetchall()}
        if fix:
            conn.execute("CREATE TABLE IF NOT EXISTS delisted("
                         "code TEXT PRIMARY KEY, last_date TEXT, ts REAL)")
        rows = conn.execute(
            "SELECT code,date,open,high,low,close,vol FROM daily_bars "
            "ORDER BY code,date").fetchall()
        by = {}
        for c, d, o, h, l, cl, v in rows:
            by.setdefault(c, []).append((d, o, h, l, cl, v or 0.0))
        stats["codes"] = len(by)
        for ci, (c, bars) in enumerate(by.items()):
            if progress and ci % 300 == 0:
                progress(f"清洗扫描 {ci}/{len(by)}")
            n = len(bars)
            bad = [b for b in bars if not _clean_bar_valid(b)]
            if bad:
                issues.setdefault(c, []).append("bad")
                stats["bad_bars"] += len(bad)
            # 价格失真：末价过低（除权公式前复权长期做减法导致）
            if bars and bars[-1][4] is not None and bars[-1][4] < 0.5:
                issues.setdefault(c, []).append("refetch")
                stats["refetch"] += 1
                stats["low_price"] = stats.get("low_price", 0) + 1
            flags = []
            name = names.get(c, "")
            for prev, cur in zip(bars, bars[1:]):
                pc, cl = prev[4], cur[4]
                lim = _limit_pct(c, name, cur[0])
                if not pc or not cl or lim is None:
                    flags.append(False)
                    continue
                flags.append(abs(cl / pc - 1) * 100 > lim + 3.0)
            viol = any(flags) if not _is_etf(c) else any(
                a and b for a, b in zip(flags, flags[1:]))
            if viol:
                issues.setdefault(c, []).append("refetch")
                stats["refetch"] += 1
            gaps = 0
            for a, b in zip(bars, bars[1:]):
                try:
                    da = _dt.datetime.strptime(a[0], "%Y-%m-%d").date()
                    db2 = _dt.datetime.strptime(b[0], "%Y-%m-%d").date()
                    if (db2 - da).days > 20:
                        gaps += 1
                except ValueError:
                    continue
            if gaps:
                stats["suspend"] += 1
            d1 = bars[-1][0]
            try:
                age = (_today - _dt.datetime.strptime(
                    d1, "%Y-%m-%d").date()).days
            except ValueError:
                age = 0
            if age > 180:
                issues.setdefault(c, []).append(f"delisted:{d1}")
                stats["delisted"] += 1
            run = 1
            for a, b in zip(bars, bars[1:]):
                run = run + 1 if a[4] == b[4] and a[4] else 1
                if run >= 20:
                    issues.setdefault(c, []).append("stale")
                    stats["stale"] += 1
                    break
        if fix:
            for c, kinds in issues.items():
                kinds_set = set(kinds)
                if "bad" in kinds_set:
                    bars = conn.execute(
                        "SELECT date,open,high,low,close FROM daily_bars "
                        "WHERE code=? ORDER BY date", (c,)).fetchall()
                    dels = [(c, b[0]) for b in bars
                            if not _clean_bar_valid(b)]
                    if dels:
                        conn.executemany(
                            "DELETE FROM daily_bars WHERE code=? AND date=?",
                            dels)
                        stats["deleted"] += len(dels)
                if ("refetch" in kinds_set or "stale" in kinds_set) \
                        and not c.startswith("bj"):
                    try:
                        fresh, raw_last = _bf_fetch_one(c)
                        fd = [r for r in fresh
                              if r["date"] < time.strftime("%Y-%m-%d")]
                        old_n = conn.execute(
                            "SELECT COUNT(*) FROM daily_bars WHERE code=?",
                            (c,)).fetchone()[0]
                        if len(fd) >= 200 and len(fd) >= min(old_n, 400):
                            conn.execute(
                                "DELETE FROM daily_bars WHERE code=?", (c,))
                            conn.executemany(
                                "INSERT OR REPLACE INTO daily_bars"
                                "(code,date,open,high,low,close,vol) "
                                "VALUES(?,?,?,?,?,?,?)",
                                [(c, r["date"], r["open"], r["high"],
                                  r["low"], r["close"], r["vol"])
                                 for r in fd])
                            if raw_last and fd[-1]["close"] > 0:
                                _set_adjust(c, raw_last / fd[-1]["close"])
                            stats["refetched"] += 1
                    except Exception:
                        pass            # 源不可用时保留原数据，下次再修
                dl = next((k.split(":", 1)[1] for k in kinds
                           if k.startswith("delisted:")), None)
                if dl:
                    conn.execute(
                        "INSERT OR REPLACE INTO delisted VALUES(?,?,?)",
                        (c, dl, time.time()))
    return stats


# ================= 全市场代码表 / 分层 =================

def stocks_age() -> float:
    with db_conn() as conn:
        ts = _get_meta(conn, "stocks_updated")
        if not ts:
            return 1e18
        try:
            return time.time() - float(ts)
        except ValueError:
            return 1e18


def refresh_all_codes(progress=None):
    """拉取全A代码表（代码/名称/总市值/东财行业），按市值三分位分层。"""
    with REFRESH_LOCK:
        if stocks_age() < STOCKS_TTL:
            if progress:
                progress("代码表仍新鲜，跳过")
            return False
        # 不含北交所(m:0+t:81)，腾讯K线不支持且用户不需要
        fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
        hosts = ("https://push2delay.eastmoney.com",
                 "https://push2.eastmoney.com",
                 "http://push2.eastmoney.com")
        items = []
        pn = 1
        while pn <= 90:
            u = (f"{hosts[(pn - 1) % len(hosts)]}/api/qt/clist/get"
                 f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invariant=0"
                 f"&fields=f12,f14,f20,f100&fs={fs}&ut={UT}")
            got = False
            for host in hosts:
                uu = u.replace(u.split("/api/")[0], host)
                try:
                    data = json.loads(_http_get(
                        uu, retries=3, timeout=20,
                        headers={"Referer":
                                 "https://quote.eastmoney.com/"})
                    ).get("data") or {}
                    got = True
                    break
                except Exception:
                    time.sleep(1.5)
            if not got:
                if len(items) >= 500 or pn > 1:
                    break           # 已拿到足够数据，容忍个别页失败
                raise RuntimeError("代码表首页拉取失败")
            diff = data.get("diff") or {}
            batch = list(diff.values()) if isinstance(diff, dict) else diff
            if not batch:
                break
            for it in batch:
                code, name = it.get("f12"), it.get("f14")
                cap = it.get("f20")
                ind = it.get("f100")
                if not code or len(code) != 6 or not isinstance(cap, (int, float)):
                    continue
                if code.startswith(("4", "8", "92")):
                    full = "bj" + code
                elif code[0] in "69" or code[:2] in ("51", "56", "58"):
                    full = "sh" + code
                else:
                    full = "sz" + code
                items.append((full, name or "", ind if isinstance(ind, str) else None,
                              float(cap)))
            if progress:
                progress(f"代码表 {len(items)} 只 (第{pn}页)")
            pn += 1
            time.sleep(0.6)
        if len(items) < 500:
            raise RuntimeError(f"代码表异常: 仅{len(items)}只")

        # 市值三分位分层
        caps = sorted(it[3] for it in items)
        q1, q2 = caps[len(caps) // 3], caps[2 * len(caps) // 3]

        def tier_of(cap):
            if cap >= q2:
                return TIERS[0]
            if cap >= q1:
                return TIERS[1]
            return TIERS[2]

        today = time.strftime("%Y-%m-%d")
        with db_conn(commit=True) as conn:
            # 只清 A 股行，保留 ETF/LOF 行（v6.1.2：ETF 宇宙独立维护）
            conn.execute(
                "DELETE FROM stocks WHERE substr(code,3,2) "
                "NOT IN ('51','56','58','15','16','18')")
            conn.executemany(
                "INSERT OR REPLACE INTO stocks"
                "(code,name,industry,mktcap,tier,updated) "
                "VALUES(?,?,?,?,?,?)",
                [(c, n, i, cap, tier_of(cap), today)
                 for c, n, i, cap in items])
            _set_meta(conn, "stocks_updated", repr(time.time()))
        if progress:
            progress(f"代码表完成: {len(items)}只, 分界 "
                     f"{q1/1e8:.0f}/{q2/1e8:.0f}亿")
        return True


def ensure_codes(progress=None) -> None:
    """代码表过期则自动刷新。"""
    if stocks_age() >= STOCKS_TTL:
        try:
            refresh_all_codes(progress)
        except Exception:
            log.exception("ensure_codes 刷新失败")
            if stocks_age() >= STOCKS_TTL * 4:
                raise           # 完全没有可用代码表时才向上抛


# ---- ETF 宇宙（v6.1.2）：东财 ETF 代码表 + 历史回填 ----

ETF_BOARD_FS = "b:MK0021,b:MK0022,b:MK0023,b:MK0024"   # 沪深 ETF/LOF 板块
_MMF_KW = ("货币", "快线", "快钱", "添益", "日利", "现金", "理财", "短融")
_MMF_PRE = ("5116", "5117", "5118", "5119", "159001", "159003", "159005")


def _is_money_etf(code, name=""):
    """货币/现金类（场内货基）：价格近乎不动、会污染低波因子，必须剔除。"""
    if any(k in (name or "") for k in _MMF_KW):
        return True
    return any(code.startswith(p) for p in _MMF_PRE)


def _refresh_etf_codes_sina(progress=None, exclude_mmf=True):
    """新浪 ETF/LOF 代码表兜底（东财整域故障时）：node=etf_hq_fund/lof_hq_fund。

    新浪 mktcap 单位为万元，×1e4 转成元，与东财 f20 口径一致。"""
    import re
    items = []
    for node in ("etf_hq_fund", "lof_hq_fund"):
        for page in range(1, 31):
            u = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
                 "json_v2.php/Market_Center.getHQNodeData"
                 f"?page={page}&num=100&sort=symbol&asc=1&node={node}")
            try:
                txt = _http_get(
                    u, retries=2, timeout=15, decode="gbk",
                    headers={"Referer": "https://finance.sina.com.cn/"},
                    src_name="新浪ETF表")
            except Exception as e:
                log.debug("新浪ETF表 %s 第%d页失败: %s", node, page, e)
                break
            try:
                batch = json.loads(re.sub(r'(?<=[{,])(\w+):', r'"\1":', txt)
                                   or "[]")
            except Exception:
                log.debug("新浪ETF表 %s 第%d页解析失败", node, page)
                break
            if not batch:
                break
            for it in batch:
                full = (it.get("symbol") or "").lower()
                name = it.get("name") or ""
                if len(full) != 8 or not _is_etf(full):
                    continue
                if exclude_mmf and _is_money_etf(full, name):
                    continue
                cap = it.get("mktcap")
                items.append((full, name,
                              float(cap) * 1e4
                              if isinstance(cap, (int, float)) else None))
            if progress and page % 3 == 0:
                progress(f"新浪ETF表 {len(items)} 只 ({node} 第{page}页)")
            if len(batch) < 100:
                break
            time.sleep(0.3)
    if not items:
        raise RuntimeError("新浪 ETF 代码表拉取失败")
    uniq = {}
    for full, name, cap in items:
        uniq[full] = (full, name, "ETF", cap, None, time.strftime("%Y-%m-%d"))
    with db_conn(commit=True) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO stocks(code,name,industry,mktcap,tier,updated) "
            "VALUES(?,?,?,?,?,?)", list(uniq.values()))
    if progress:
        progress(f"ETF 代码表完成(新浪): {len(uniq)} 只（已排除货币类）")
    return len(uniq)


def _refresh_etf_codes_em(progress=None, exclude_mmf=True):
    """东财 ETF/LOF 代码表（主源）。返回写入条数。"""
    hosts = ("https://push2delay.eastmoney.com", "https://push2.eastmoney.com",
             "http://push2.eastmoney.com")
    UT = "fa5fd1943c7b386f172d6893dbfba10b"
    items, pn, total = [], 1, None
    while pn <= 25:
        got, data = False, {}
        for host in hosts:
            u = (f"{host}/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1"
                 f"&fltt=2&invt=2&fields=f12,f13,f14,f20&fs={ETF_BOARD_FS}"
                 f"&ut={UT}")
            try:
                data = json.loads(_http_get(
                    u, retries=2, timeout=20,
                    headers={"Referer": "https://quote.eastmoney.com/"}
                )).get("data") or {}
                got = True
                break
            except Exception:
                time.sleep(1.0)
        if not got:
            break
        if total is None:
            total = data.get("total")
            log.info("ETF 代码表 total=%s", total)
        diff = data.get("diff") or {}
        batch = list(diff.values()) if isinstance(diff, dict) else diff
        if not batch:
            break
        for it in batch:
            code, name = it.get("f12"), it.get("f14") or ""
            if not code or len(code) != 6:
                continue
            full = ("sh" if code[0] == "5" else "sz") + code
            if not _is_etf(full):
                continue
            if exclude_mmf and _is_money_etf(full, name):
                continue
            cap = it.get("f20")
            items.append((full, name, float(cap) if isinstance(cap, (int, float))
                          else None))
        if progress and pn % 3 == 0:
            progress(f"ETF 代码表 {len(items)} 只 (第{pn}页)")
        pn += 1
        time.sleep(0.4)
    if not items:
        raise RuntimeError("ETF 代码表拉取失败")

    # 去重（同代码只留一条）
    uniq = {}
    for full, name, cap in items:
        uniq[full] = (full, name, "ETF", cap, None, time.strftime("%Y-%m-%d"))
    with db_conn(commit=True) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO stocks(code,name,industry,mktcap,tier,updated) "
            "VALUES(?,?,?,?,?,?)", list(uniq.values()))
    if progress:
        progress(f"ETF 代码表完成: {len(uniq)} 只（已排除货币类）")
    return len(uniq)


def refresh_etf_codes(progress=None, exclude_mmf=True):
    """拉取 ETF/LOF 代码表写入 stocks（industry='ETF'），保留原有 A 股行。

    主源东财，整域故障（连接重置/超时）时自动改走新浪 ETF/LOF 列表兜底。"""
    try:
        return _refresh_etf_codes_em(progress, exclude_mmf)
    except Exception as e:
        log.warning("东财 ETF 代码表失败，改走新浪: %s", e)
    if progress:
        progress("东财 ETF 代码表不可用，改用新浪 ...")
    return _refresh_etf_codes_sina(progress, exclude_mmf)


def backfill_etf_history(codes=None, progress=None, workers=6,
                         min_rows=200, only_stale=True):
    """回填 ETF 日K（hfg 口径，与主库一致）。返回统计 dict。

    codes 为空时取 stocks 表内 industry='ETF' 的全部代码。"""
    if codes is None:
        with db_conn() as conn:
            codes = [c for (c,) in conn.execute(
                "select code from stocks where industry='ETF' order by code")]
    codes = list(codes)
    if not codes:
        return {"total": 0, "ok": 0, "skip": 0, "fail": 0, "bar_ok": 0}
    have = {}
    with db_conn() as conn:
        for c, n, mx in conn.execute(
                "select code,count(*),max(date) from daily_bars "
                "group by code"):
            have[c] = (n, mx)
    fresh = last_completed_td()
    todo = []
    for c in codes:
        n, mx = have.get(c, (0, ""))
        if only_stale and n >= 1000 and mx >= fresh:
            continue
        todo.append(c)
    stat = {"total": len(codes), "todo": len(todo), "ok": 0, "fail": 0,
            "bar_ok": 0, "fail_list": []}
    if not todo:
        stat["bar_ok"] = sum(1 for c in codes if have.get(c, (0,))[0] >= min_rows)
        return stat
    lock = threading.Lock()
    done = [0]

    def work(code):
        try:
            rows = _fetch_remote_rows(code, count=CFG.MAX_FETCH_BARS)
            if not rows:
                raise RuntimeError("空数据")
            data = [r for r in rows if _bar_ok(r)]
            if len(data) < min_rows:
                raise RuntimeError(f"有效数据仅{len(data)}根")
            with db_conn(commit=True) as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO daily_bars"
                    "(code,date,open,high,low,close,vol) VALUES(?,?,?,?,?,?,?)",
                    [(code, r["date"], r["open"], r["high"], r["low"],
                      r["close"], r["vol"]) for r in data])
            with db_conn() as conn:
                allrows = _db_rows(conn, code)
            try:
                _sync_adjust(code, allrows)
            except Exception:
                log.debug("ETF adjust 同步失败 %s", code, exc_info=True)
            return code, len(data), None
        except Exception as e:
            return code, 0, str(e)[:80]

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(work, c) for c in todo]
        for fut in as_completed(futs):
            code, n, err = fut.result()
            with lock:
                done[0] += 1
                if err:
                    stat["fail"] += 1
                    stat["fail_list"].append((code, err))
                else:
                    stat["ok"] += 1
                    if n >= min_rows:
                        stat["bar_ok"] += 1
                if progress and done[0] % 50 == 0:
                    progress(f"ETF 回填 {done[0]}/{len(todo)}"
                             f"（成功{stat['ok']} 失败{stat['fail']}）")
    return stat


def get_stock_info(full: str):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT name,industry,mktcap,tier FROM stocks WHERE code=?",
            (full,)).fetchone()
        return {"name": row[0], "industry": row[1],
                "mktcap": row[2], "tier": row[3]} if row else None


def industry_peers(full: str, limit: int = L2_DEFAULT_N):
    """L2池：同行业、市值最接近目标股的 N 只（不含自身）。"""
    info = get_stock_info(full)
    if not info or not info.get("industry"):
        return [], None
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code,name,mktcap FROM stocks "
            "WHERE industry=? AND code!=? AND code NOT LIKE 'bj%'",
            (info["industry"], full)).fetchall()
    if not rows:
        return [], info["industry"]
    my_cap = info.get("mktcap") or 0.0
    lg = math.log(max(my_cap, 1e8))

    def near(r):
        return abs(math.log(max(r[2] or 1e8, 1e8)) - lg)

    rows.sort(key=near)
    return [r[0] for r in rows[:limit]], info["industry"]


_TIER_POOL_CACHE = {}       # (tier, 日期) -> L3样本代码列表（同日共享，避免重复拉取）
_TIER_POOL_TS = {}


def tier_sample(full: str, n: int = 0, exclude_industries=()):
    """L3池：同市值层样本，**缓存优先、不固定数量**。

    - 命中本地缓存的同层代码排前面（分析立刻可用）；
    - 未缓存的排后面，由后台渐进回填，拉到多少用多少；
    - 不再固定截取 N 只（n<=0 时返回全层）。
    缓存读写全程持有 _STATE_LOCK，多线程下不会互相污染。"""
    info = get_stock_info(full)
    if not info or not info.get("tier"):
        return [], None
    tier = info["tier"]
    today = time.strftime("%Y%m%d")
    key = (tier, today)
    now = time.time()
    pool = None
    with _STATE_LOCK:
        cached = _TIER_POOL_CACHE.get(key)
        if cached is not None and now - _TIER_POOL_TS.get(key, 0) <= 86400:
            pool = cached
        else:
            # 内存缓存跨天失效后重新计算（锁内查库+回写，保证原子性）
            with db_conn() as conn:
                q = ("SELECT code,industry FROM stocks "
                     "WHERE tier=? AND code NOT LIKE 'bj%'")
                rows = conn.execute(q, (tier,)).fetchall()
                have = {r[0] for r in conn.execute(
                    "SELECT DISTINCT code FROM daily_bars").fetchall()}
            # 缓存优先：已回填过日K的排前面，其余排后面
            rnd = random.Random(today + tier)
            head = [r for r in rows if r[0] in have]
            tail = [r for r in rows if r[0] not in have]
            rnd.shuffle(head)
            rnd.shuffle(tail)
            pool = head + tail
            _TIER_POOL_CACHE[key] = pool
            _TIER_POOL_TS[key] = now
    # 排除自身与 L2 已覆盖的行业（industry 名字，非代码）
    exclude = set(exclude_industries or ())
    if exclude:
        pool = [r for r in pool if (r[1] or "") not in exclude]
    out = [c for c, _ in pool if c != full]
    if n and n > 0:
        out = out[:n]
    return out, tier


# 题材行业（交易回测证实：这些行业L2无增益，只用L1）+ 传统行业ETF池
THEME_KW = ("软件", "计算机", "半导体", "元件", "电子", "通信", "光电",
            "IT", "互联网", "游戏", "传媒", "数字", "消费电子", "光学",
            "医药", "中药", "生物", "医疗", "制药", "疫苗")
ETF_POOL = ("sh510300", "sh510500", "sz159915", "sh588000", "sh510050",
            "sh512100", "sh510880", "sz159922", "sh512880", "sh512690")


def _is_theme_industry(industry):
    """科技/医药等题材行业：L2跨股池无增益，只参考L1自身历史。"""
    return any(k in (industry or "") for k in THEME_KW)


def pool_codes(full, l2_n=L2_DEFAULT_N, l3_n=0):
    """一次拿到两级样本池。l3_n<=0 表示 L3 不限量（缓存优先排序）。

    - 题材行业/ETF 目标：返回空池（只用 L1）
    - 传统行业：L2 = 精确同行业 + 宽基/行业ETF池（交易回测胜率+2.2pp）"""
    if _is_etf(full):
        return {"l2": [], "l3": [], "industry": None, "tier": None}
    peers, industry = industry_peers(full, l2_n)
    if _is_theme_industry(industry):
        return {"l2": [], "l3": [], "industry": industry, "tier": None}
    l3, tier = tier_sample(full, l3_n, exclude_industries=(industry,))
    seen = {full}
    l3 = [c for c in l3 if c not in seen and not seen.add(c)]
    # 传统行业：L2 并入 ETF 池（市场beta形态样本）
    if peers:
        eseen = set(peers) | {full}
        peers = peers + [e for e in ETF_POOL if e not in eseen]
    return {"l2": peers, "l3": l3,
            "industry": industry, "tier": tier}


# 注意：KLINE_URL / INI_PATH 等常量统一定义在文件头部内嵌缓存层，此处不再重复


QT_URL = "https://qt.gtimg.cn/q="
AUTHOR = "獨白"
AUTHOR_EMAIL = "kingrux106@gmail.com"
AUTHOR_QQ = "2180287399"
DISCLAIMER = ("免责声明：本程序所有输出仅为历史数据的技术统计与研究用途，"
              "不构成任何投资建议或收益承诺。股市有风险，据此操作盈亏自负。")
INDEX_CODES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("bj899050", "北证50"),
]

UP, DOWN, PRED_C = "#ff5252", "#26c281", "#4da3ff"
TPRED_C = "#ffffff"   # 暗色主题下白色虚线；亮色主题自动切换为黑色
# 指标线配色（随主题切换，见 THEMES；高对比主题用更亮的线）
MA_COLORS = {5: "#ffb86b", 10: "#7cc4ff", 20: "#f0a6ff", 30: "#7bf08b", 60: "#e8c14a"}
C_ORANGE, C_BLUE = "#ffa94d", "#5dade2"      # RSI6/DIF/K 与 RSI12/DEA/D
C_PURPLE, C_GOLD = "#d0a9f5", "#e8c14a"      # KDJ·J/BOLL中轨 与 BOLL轨道/BOLL%
BG = "#14181e"
GRID_C = "#232b34"
GUIDE_C = "#39434e"
AXIS_TXT = "#8fa0ad"
TITLE_TXT = "#aebccb"
CROSS_C = "#9fb3c8"
DARK_BG = "#101418"
PANEL_BG = "#171c22"
FIELD_BG = "#1c232b"
FG_MAIN = "#d7dee6"
BTN_BG = "#222a33"
BTN_FG = "#d7dee6"
BTN_HOVER = "#2b3540"
BTN_BORDER = "#333e4a"
# v6.1.5 UI 优化：统一边框/悬停/选中/强调/日志底色（随主题切换）
BORDER = "#2a3340"
HOVER_BG = "#222a34"
SEL_BG = "#2b3a4d"
ACCENT = "#4da3ff"
LOG_BG = "#0d1116"

# ---- 可切换主题 ----
THEMES = {
    "dark": dict(
        UP="#ff5252", DOWN="#26c281", PRED_C="#4da3ff", TPRED_C="#ffffff",
        BG="#14181e", GRID_C="#232b34", GUIDE_C="#39434e",
        AXIS_TXT="#8fa0ad", TITLE_TXT="#aebccb", CROSS_C="#9fb3c8",
        DARK_BG="#101418", PANEL_BG="#171c22", FIELD_BG="#1c232b",
        FG_MAIN="#d7dee6",
        BTN_BG="#222a33", BTN_FG="#d7dee6", BTN_HOVER="#2b3540",
        BTN_BORDER="#333e4a",
        BORDER="#2a3340", HOVER_BG="#222a34", SEL_BG="#2b3a4d",
        ACCENT="#4da3ff", LOG_BG="#0d1116",
        MA_COLORS={5: "#ffb86b", 10: "#7cc4ff", 20: "#f0a6ff",
                   30: "#7bf08b", 60: "#e8c14a"},
        C_ORANGE="#ffa94d", C_BLUE="#5dade2",
        C_PURPLE="#d0a9f5", C_GOLD="#e8c14a",
    ),
    "light": dict(
        UP="#e03131", DOWN="#0ca678", PRED_C="#1971c2", TPRED_C="#111111",
        BG="#ffffff", GRID_C="#ececec", GUIDE_C="#eef1f4",
        AXIS_TXT="#6b7684", TITLE_TXT="#3b444e", CROSS_C="#999999",
        DARK_BG="#f2f4f7", PANEL_BG="#ffffff", FIELD_BG="#f8f9fb",
        FG_MAIN="#1f2933",
        BTN_BG="#ffffff", BTN_FG="#1f2933", BTN_HOVER="#eef2f7",
        BTN_BORDER="#c9d1d9",
        BORDER="#d9dee5", HOVER_BG="#eef2f7", SEL_BG="#d8e8ff",
        ACCENT="#1971c2", LOG_BG="#f7f8fa",
        MA_COLORS={5: "#e8590c", 10: "#1971c2", 20: "#ae3ec9",
                   30: "#2f9e44", 60: "#b08900"},
        C_ORANGE="#e8590c", C_BLUE="#1971c2",
        C_PURPLE="#9c36b5", C_GOLD="#b08900",
    ),
    # 高对比：纯黑底 + 纯白字 + 亮边框/亮黄光标，适合弱光或视力不佳场景
    "contrast": dict(
        UP="#ff2d2d", DOWN="#00e676", PRED_C="#40c4ff", TPRED_C="#ffffff",
        BG="#000000", GRID_C="#3a3a3a", GUIDE_C="#6b6b6b",
        AXIS_TXT="#ffffff", TITLE_TXT="#ffffff", CROSS_C="#ffff00",
        DARK_BG="#000000", PANEL_BG="#0a0a0a", FIELD_BG="#111111",
        FG_MAIN="#ffffff",
        BTN_BG="#000000", BTN_FG="#ffffff", BTN_HOVER="#2a2a2a",
        BTN_BORDER="#ffffff",
        BORDER="#ffffff", HOVER_BG="#2a2a2a", SEL_BG="#555500",
        ACCENT="#ffee00", LOG_BG="#000000",
        MA_COLORS={5: "#ffb000", 10: "#00d4ff", 20: "#ff7ae0",
                   30: "#39ff88", 60: "#ffee00"},
        C_ORANGE="#ffb000", C_BLUE="#00b7ff",
        C_PURPLE="#ff7ae0", C_GOLD="#ffee00",
    ),
}


def apply_theme(theme, updown):
    """按设置重写模块级颜色常量；绘图函数读取全局值。"""
    t = dict(THEMES.get(theme, THEMES["dark"]))
    if updown == "green_up":
        t["UP"], t["DOWN"] = t["DOWN"], t["UP"]
    for k, v in t.items():
        globals()[k] = v


# ================= 数据获取 =================

def http_get(url: str, retries: int = 3) -> str:
    """腾讯行情文本接口（GBK）。统一走带熔断上报的 _http_get。"""
    return _http_get(url, retries=retries, timeout=15, decode="gbk",
                     src_name="腾讯行情")


def normalize_code(code):
    code = code.strip().lower()
    # 触摸屏/输入法常见：全角字符折叠为半角
    code = "".join(
        chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch
        for ch in code)
    # 只保留ASCII字母数字：零宽/NBSP/控制字符/中文标点一次性清除
    code = "".join(ch for ch in code if ch.isascii() and ch.isalnum())
    # 兼容 002241sz / sz.002241 / sh-600519 / 600519.SH 等变体
    if len(code) == 8 and code[-2:] in ("sh", "sz", "bj"):
        code = code[-2:] + code[:6]    # 002241sz → sz002241
    # 触摸屏 O/I/L 与 0/1 误触：'O00725'→'000725'（仅当整体为纯数字时替换）
    if any(ch in code for ch in "oil"):
        code2 = (code.replace("o", "0").replace("i", "1")
                     .replace("l", "1"))
        if code2.isdigit() or code2[:2] in ("sh", "sz", "bj"):
            code = code2
    for p in ("sh", "sz", "bj"):
        if code.startswith(p):
            return p + code[2:]
    d = "".join(ch for ch in code if ch.isdigit())
    if len(d) != 6:
        # !r 显示原始输入（含隐藏字符），便于远程排查
        raise ValueError(
            f"代码格式不对: {code!r}\n"
            "支持：002241 / 600519 / sh600519 / sz002241 / 002241.sz")
    if d[0] in "69" or d[:2] in ("51", "56", "58"):      # 沪股/沪ETF
        return "sh" + d
    if d[0] in "03" or d[:2] in ("15", "16", "18"):      # 深股/深ETF/LOF
        return "sz" + d
    if d[0] in "48":
        return "bj" + d
    raise ValueError(
        f"不支持的代码: {code!r}\n"
        "支持：沪深A股/ETF（0/3/6/5开头）与北交所（4/8开头）")


def vol_ratio_at(vols, i):
    """截至第 i 日（含）的量能状态：近5日均量 / 前15日均量。"""
    if i < 19:
        return None
    r5 = sum(vols[i - 4:i + 1]) / 5
    p15 = sum(vols[i - 19:i - 4]) / 15
    return (r5 / p15) if p15 > 0 else None


def vol_regime(vr):
    if vr is None:
        return "?"
    if vr > 1.2:
        return "放量"
    if vr < 0.8:
        return "缩量"
    return "平量"


def fmt_vol_cn(v):
    if v >= 1e8:
        return f"{v/1e8:.2f}亿"
    if v >= 1e4:
        return f"{v/1e4:.1f}万"
    return f"{v:.0f}"


_SECTOR_CACHE = {}          # code -> (timestamp, 结果三元组)
_SECTOR_CACHE_TTL = 1800    # 30 分钟
_BK_LIST_CACHE = {}         # 板块名 -> BK代码（全局，行业名单变化慢）
_BK_LIST_TS = 0.0

_IDX_QUOTE_CACHE = {}       # 上证指数行情快照，跨多股共享（避免重复拉取）
_IDX_QUOTE_TTL = 60         # 60 秒

_TOP_SECTORS_CACHE = None
_TOP_SECTORS_TS = 0.0
_TOP_SECTORS_LAST = ([], [])    # 最近一次成功结果（网络全挂时回退用）
_TOP_SECTORS_WARN_TS = 0.0      # 失败警告降噪：10分钟内只警告一次


def _fetch_top_sectors_tencent():
    """腾讯行业板块榜（东财整域故障时兜底）：返回 [(name, pct), ...]。
    o=0 涨幅榜 / o=1 跌幅榜，各取前10。"""
    out = []
    for o in (0, 1):
        try:
            txt = _http_get(
                "https://ifzq.gtimg.cn/appstock/app/mktHs/rank"
                f"?l=10&p=1&t=01/averatio&o={o}",
                retries=1, timeout=8, src_name="腾讯板块")
            for it in (json.loads(txt).get("data") or []):
                name = it.get("bd_name")
                pct = it.get("bd_zdf")
                if name and pct is not None:
                    out.append((name, float(pct)))
        except Exception as e:
            log.debug("腾讯板块榜 o=%s 失败: %s", o, e)
    return out


_AKSHARE_MOD = [None, 0]        # [模块, 状态] 状态: 0=未探测 1=可用 -1=不可用


def _akshare():
    """懒加载 akshare（可选依赖：未安装/导入失败返回 None，只探测一次）。

    客户端默认不依赖 akshare；装了则自动作为末位兜底源启用。"""
    if _AKSHARE_MOD[1] != 0:
        return _AKSHARE_MOD[0]
    try:
        import akshare as ak
        _AKSHARE_MOD[0] = ak
        _AKSHARE_MOD[1] = 1
        log.info("akshare 兜底源可用: %s", getattr(ak, "__version__", "?"))
    except Exception as e:
        _AKSHARE_MOD[1] = -1
        log.info("未安装 akshare，跳过末位兜底（%s）", e)
    return _AKSHARE_MOD[0]


def _fetch_top_sectors_akshare():
    """akshare（Sina 行业线路）板块榜兜底：返回 [(name, pct), ...]。
    东财/腾讯全挂且本机已安装 akshare 时才会走到这里。"""
    ak = _akshare()
    if ak is None:
        return []
    try:
        df = ak.stock_sector_spot(indicator="行业")
        out = []
        for _, row in df.iterrows():
            name = str(row.get("板块") or "").strip()
            pct = row.get("涨跌幅")
            if name and pct is not None:
                out.append((name, float(pct)))
        return out
    except Exception as e:
        log.debug("akshare 行业榜失败: %s", e)
        return []


def fetch_top_sectors():
    """获取今日行业板块涨跌幅排行（Top3涨/Top3跌）。
    返回 [(name, pct), ...] 的两个列表，带10分钟缓存。
    缓存读写持有 _STATE_LOCK；请求统一走熔断 _http_get。
    网络全挂时回退最近一次成功结果（可能滞后，但好于空白）。"""
    global _TOP_SECTORS_CACHE, _TOP_SECTORS_TS, _TOP_SECTORS_WARN_TS
    global _TOP_SECTORS_LAST
    now = time.time()
    with _STATE_LOCK:
        if _TOP_SECTORS_CACHE and now - _TOP_SECTORS_TS < 600:
            return _TOP_SECTORS_CACHE
    UT = "fa5fd1943c7b386f172d6893dbfba10b"
    hdr = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    # HTTPS/HTTP × 主域/延迟域：连接被重置时逐个轮换；
    # 熔断期内直接跳过东财（改走腾讯/akshare兜底），避免每轮白等数秒
    hosts = () if not _cb_ok("东财板块") else (
        "https://push2delay.eastmoney.com",
        "https://push2.eastmoney.com",
        "http://push2delay.eastmoney.com",
        "http://push2.eastmoney.com")
    items = []
    vals = []
    fail_cnt = 0
    for pn in (1, 2, 3):
        u = (f"/api/qt/clist/get"
             f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invariant=0"
             f"&fields=f12,f14,f3&fs=m:90+t:2&ut={UT}")
        done = False
        for host in hosts:
            try:
                data = json.loads(_http_get(
                    host + u, retries=3, timeout=8, headers=dict(hdr),
                    src_name="东财板块"))
                diff = data.get("data", {}).get("diff") or {}
                vals = list(diff.values()) if isinstance(diff, dict) else diff
                for it in vals:
                    name = it.get("f14", "")
                    pct = it.get("f3")
                    if name and pct is not None:
                        items.append((name, float(pct)))
                done = True
                break
            except Exception as e:
                fail_cnt += 1
                if now - _TOP_SECTORS_WARN_TS > 600:
                    log.warning("板块排行第%d页 %s 失败: %s", pn, host, e)
                else:
                    log.debug("板块排行第%d页 %s 失败: %s", pn, host, e)
                continue
        if not done:
            break
        if len(vals) < 100:
            break
    if now - _TOP_SECTORS_WARN_TS > 600:
        _TOP_SECTORS_WARN_TS = now
    if not items:
        # 东财整域故障（连接被重置/502）时改走腾讯行业板块榜兜底
        items = _fetch_top_sectors_tencent()
    if not items:
        # 腾讯也挂：akshare（Sina 线路）末位兜底；未装 akshare 自动跳过
        items = _fetch_top_sectors_akshare()
    if items:
        items.sort(key=lambda x: x[1], reverse=True)
        result = (items[:3], items[-3:][::-1])
        with _STATE_LOCK:
            _TOP_SECTORS_CACHE = result
            _TOP_SECTORS_TS = now
            _TOP_SECTORS_LAST = result
        return result
    # 全部失败：回退最近一次成功结果，避免界面板块栏空白
    log.info("板块排行本轮全部失败(%d次请求)，回退上次结果", fail_cnt)
    return _TOP_SECTORS_LAST


def fetch_sector_context(full):
    """个股所属行业板块指数上下文。

    返回 (板块名, {date: 当日涨跌%}, 板块今日涨跌%)；失败返回 (None, {}, None)。
    带 30 分钟缓存：板块日内变化不大，命中缓存零耗时。
    """
    now = time.time()
    with _STATE_LOCK:
        hit = _SECTOR_CACHE.get(full)
        if hit and now - hit[0] < _SECTOR_CACHE_TTL:
            return hit[1]
    if not _cb_ok("东财板块"):
        # 东财板块接口熔断期内：板块上下文不是必需项，直接跳过省时
        return None, {}, None
    UT = "fa5fd1943c7b386f172d6893dbfba10b"
    hdr = {"User-Agent": "Mozilla/5.0",
           "Referer": "https://quote.eastmoney.com/"}

    def get(u, timeout=4):
        # 东财接口对部分网络直连被重置：按 URL 类型做主机轮询容灾；
        # 有些主机会返回 200 的反爬 HTML 页，需校验内容是 JSON
        if "push2his" in u:
            hosts = ("push2delay.eastmoney.com", "push2his.eastmoney.com",
                     "92.push2his.eastmoney.com")
            base = "push2his.eastmoney.com"
        else:
            hosts = ("push2delay.eastmoney.com", "push2.eastmoney.com",
                     "push2his.eastmoney.com")
            base = "push2.eastmoney.com"
        last = None
        for host in hosts:
            uu = u.replace(base, host)
            try:
                txt = _http_get(uu, retries=1, timeout=timeout,
                                headers=dict(hdr), src_name="东财板块")
                txt = txt.lstrip("\ufeff")
                if txt.lstrip().startswith("{"):
                    return txt
                last = RuntimeError("反爬HTML页")
                log.debug("板块接口 %s 返回非JSON", host)
            except Exception as e:
                last = e
                log.debug("板块接口 %s 失败: %s", host, e)
        raise last

    try:
        global _BK_LIST_CACHE, _BK_LIST_TS
        code = full[2:]
        mkt = "1" if full.startswith("sh") else "0"
        # 1) 个股行业名
        u = (f"https://push2.eastmoney.com/api/qt/stock/get"
             f"?secid={mkt}.{code}&fields=f127&ut={UT}")
        ind_name = json.loads(get(u))["data"].get("f127")
        if not ind_name:
            return None, {}, None
        # 2) 行业板块列表（分页，全局缓存 30 分钟），按名称匹配 BK 代码
        bk_code = None
        with _STATE_LOCK:
            if _BK_LIST_CACHE and now - _BK_LIST_TS < _SECTOR_CACHE_TTL:
                bk_code = _BK_LIST_CACHE.get(ind_name)
            else:
                new_bk = {}
                for pn in (1, 2, 3):
                    u = (f"https://push2.eastmoney.com/api/qt/clist/get"
                         f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invariant=0"
                         f"&fields=f12,f14&fs=m:90+t:2&ut={UT}")
                    diff = json.loads(get(u)).get("data", {}).get("diff") or {}
                    items = list(diff.values()) if isinstance(diff, dict) else diff
                    for it in items:
                        new_bk[it.get("f14")] = it.get("f12")
                    if len(items) < 100:
                        break
                _BK_LIST_CACHE = new_bk
                _BK_LIST_TS = now
                bk_code = _BK_LIST_CACHE.get(ind_name)
        if not bk_code:
            return ind_name, {}, None
        # 3) 板块日K（收盘价）—— 必须带 ut，否则部分主机返回反爬页
        u = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
             f"?secid=90.{bk_code}&fields1=f1,f2,f3&fields2=f51,f53"
             f"&klt=101&fqt=0&beg=20240101&end=20500101&ut={UT}")
        kl = json.loads(get(u, timeout=8))["data"]["klines"]
        bars = [(s.split(",")[0], float(s.split(",")[1])) for s in kl]
        if not bars:
            # 部分板块指数日K在部分主机返回空：降级取今日板块涨跌幅
            # （历史留空，评分按缺数据处理）
            u3 = (f"https://push2.eastmoney.com/api/qt/clist/get"
                  f"?pn=1&pz=100&po=1&np=1&fltt=2&invariant=0"
                  f"&fields=f12,f14,f3&fs=m:90+t:2&ut={UT}")
            diff = (json.loads(get(u3, timeout=8)).get("data") or {}).get(
                "diff") or {}
            items = list(diff.values()) if isinstance(diff, dict) else diff
            today_chg = None
            for it in items:
                if it.get("f12") == bk_code and it.get("f3") is not None:
                    today_chg = float(it["f3"])
                    break
            log.info("板块 %s(%s) 日K为空，降级仅用今日涨跌 %s",
                     ind_name, bk_code, today_chg)
            with _STATE_LOCK:
                _SECTOR_CACHE[full] = (time.time(),
                                       (ind_name, {}, today_chg))
            return ind_name, {}, today_chg
        if len(bars) < 2:
            log.info("板块 %s(%s) 日K数据不足2天", ind_name, bk_code)
            return ind_name, {}, None
        chg_by_date = {
            b[0]: (b[1] / a[1]) * 100 - 100
            for a, b in zip(bars, bars[1:])
        }
        today_chg = chg_by_date.get(bars[-1][0])
        with _STATE_LOCK:
            _SECTOR_CACHE[full] = (time.time(),
                                   (ind_name, chg_by_date, today_chg))
        return ind_name, chg_by_date, today_chg
    except Exception as e:
        # 东财整域故障时属预期失败，降噪为单行（板块上下文缺失不影响主预测）
        log.warning("fetch_sector_context 失败 %s（忽略）: %s", full, e)
        return None, {}, None


def _fetch_quote_tencent(full):
    f = http_get(QT_URL + full).split("~")
    if len(f) < 35 or not f[3]:
        raise ValueError("腾讯未查询到该股票")
    return {"name": f[1], "price": float(f[3]), "prev_close": float(f[4]),
            "open": float(f[5]), "high": float(f[33]), "low": float(f[34]),
            "time": f[30]}


def _fetch_quote_sina(full):
    """新浪行情快照（GBK，需带 Referer）。字段与腾讯同序：
    name/open/prev_close/price/high/low/date/time。"""
    txt = _http_get("https://hq.sinajs.cn/list=" + full, retries=2,
                    timeout=10, decode="gbk",
                    headers={"Referer": "https://finance.sina.com.cn/"},
                    src_name="新浪行情")
    body = txt.split('"')[1] if '"' in txt else ""
    f = body.split(",")
    if len(f) < 32 or not f[3] or float(f[3] or 0) <= 0:
        raise ValueError("新浪未查询到该股票")
    return {"name": f[0], "price": float(f[3]), "prev_close": float(f[2]),
            "open": float(f[1]), "high": float(f[4]), "low": float(f[5]),
            "time": f[31]}


def _fetch_quote_em(full):
    """东财 ulist 行情快照（push2 整域故障时通常也挂，作末位兜底）。"""
    secid = _code_to_em(full)
    if not secid:
        raise ValueError("不支持的市场")
    u = ("https://push2.eastmoney.com/api/qt/ulist.np/get"
         f"?secids={secid}&fltt=2&invt=2"
         f"&fields=f2,f12,f14,f15,f16,f17,f18,f124&ut={UT}")
    d = json.loads(_http_get(
        u, retries=2, timeout=8,
        headers={"Referer": "https://quote.eastmoney.com/"},
        src_name="东财行情"))
    diff = (d.get("data") or {}).get("diff") or {}
    it = (list(diff.values())[0] if isinstance(diff, dict)
          else (diff[0] if diff else None))
    if not it or it.get("f2") in (None, "-"):
        raise ValueError("东财未查询到该股票")
    try:
        # f124 为最后行情时间戳（秒），保留完整日期供休市/盘前判定
        qtime = time.strftime("%Y%m%d%H%M%S",
                              time.localtime(float(it.get("f124"))))
    except (TypeError, ValueError, OSError):
        qtime = time.strftime("%Y%m%d%H%M%S")
    return {"name": it.get("f14") or full, "price": float(it["f2"]),
            "prev_close": float(it.get("f18") or 0),
            "open": float(it.get("f17") or 0),
            "high": float(it.get("f15") or 0),
            "low": float(it.get("f16") or 0),
            "time": qtime}


def fetch_quote(full):
    """行情快照多源容灾：腾讯 → 新浪 → 东财，任一成功即返回。"""
    last = None
    for name, fetcher in (("腾讯行情", _fetch_quote_tencent),
                          ("新浪行情", _fetch_quote_sina),
                          ("东财行情", _fetch_quote_em)):
        try:
            q = fetcher(full)
            if full == "sh000001":
                _note_index_snap(q.get("time"))    # 休市判定锚
            return q
        except Exception as e:
            last = e
            log.debug("%s失败 %s: %s", name, full, e)
    raise RuntimeError(f"所有行情源均失败: {last}")


def _batch_tencent(codes):
    raw = http_get(QT_URL + ",".join(codes))
    out = {}
    for seg in raw.split(";"):
        seg = seg.strip()
        if "=" not in seg or "~" not in seg:
            continue
        code = seg.split("=")[0].strip().replace("v_", "", 1).lower()
        f = seg.split("~")
        if len(f) < 34 or not f[3]:
            continue
        try:
            out[code] = {"name": f[1], "price": float(f[3]),
                         "chg": float(f[32]), "time": f[30]}
        except ValueError:
            continue
    return out


def _batch_sina(codes):
    txt = _http_get("https://hq.sinajs.cn/list=" + ",".join(codes),
                    retries=2, timeout=10, decode="gbk",
                    headers={"Referer": "https://finance.sina.com.cn/"},
                    src_name="新浪行情")
    out = {}
    for line in txt.split(";"):
        if '"' not in line:
            continue
        code = (line.split("=")[0].strip()
                .replace("var hq_str_", "").lower())
        f = line.split('"')[1].split(",")
        if not code or len(f) < 6 or not f[3]:
            continue
        try:
            price = float(f[3])
            prev = float(f[2] or 0)
        except ValueError:
            continue
        if price <= 0:
            continue
        tstr = ""
        if len(f) > 31 and f[30]:
            tstr = f[30].replace("-", "") + f[31].replace(":", "")
        out[code] = {"name": f[0], "price": price,
                     "chg": (price / prev * 100 - 100) if prev else 0.0,
                     "time": tstr}
    return out


def fetch_batch_quotes(codes):
    """批量行情快照（自选池名称/指数栏用）：腾讯 → 新浪。"""
    codes = [c for c in codes if c]
    if not codes:
        return {}
    last = None
    for fetcher in (_batch_tencent, _batch_sina):
        try:
            d = fetcher(codes)
            if d:
                if "sh000001" in d:
                    _note_index_snap(d["sh000001"].get("time"))
                return d
        except Exception as e:
            last = e
    log.debug("批量行情全失败: %s", last)
    return {}


def fetch_quote_cached(full: str, ttl=_IDX_QUOTE_TTL):
    """带短时缓存的行情快照：多只股票共享同一份大盘/指数数据。"""
    if full == "sh000001":
        now = time.time()
        with _STATE_LOCK:
            hit = _IDX_QUOTE_CACHE.get(full)
            if hit and now - hit[0] < ttl:
                return hit[1]
            q = fetch_quote(full)
            _IDX_QUOTE_CACHE[full] = (now, q)
            return q
    return fetch_quote(full)


def fetch_daily(full):
    kd = json.loads(http_get(KLINE_URL + f"?param={full},day,,,500,qfq"))
    d = kd.get("data", {}).get(full)
    if not d:
        raise ValueError("K线数据获取失败")
    bars = d.get("qfqday") or d.get("day")
    rows = [{"date": b[0], "open": float(b[1]), "close": float(b[2]),
             "high": float(b[3]), "low": float(b[4]), "vol": float(b[5])}
            for b in bars if float(b[2]) > 0]
    if len(rows) < 100:
        raise ValueError("上市时间太短，样本不足")
    return rows


# ================= 指标计算 =================

def sma_period(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(vals, n):
    out, k, e = [], 2 / (n + 1), None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def calc_macd(closes):
    dif = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    dea_raw = ema(dif, 9)
    dea = [None] * 8 + dea_raw[8:]
    hist = [None if dd is None else 2 * (a - dd) for a, dd in zip(dif, dea)]
    return dif, dea, hist


def calc_kdj(rows, n=9):
    ks, ds = [], []
    k = d = 50.0
    for i in range(len(rows)):
        seg = rows[max(0, i - n + 1):i + 1]
        lo = min(r["low"] for r in seg)
        hi = max(r["high"] for r in seg)
        rsv = (rows[i]["close"] - lo) / (hi - lo) * 100 if hi > lo else 50.0
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        ks.append(k)
        ds.append(d)
    return ks, ds, [3 * a - 2 * b for a, b in zip(ks, ds)]


def calc_rsi(closes, n):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def calc_chips(rows, cur_price=None, nbin=360):
    """筹码分布：逐日按换手衰减历史筹码，当日成交量在[低,高]区间均匀摊分。
    无流通股本数据，换手率用 量/中位量*2% 启发式近似（限幅）。
    nbin=360（v6.1.5 热修⑦）：全历史价格区间下 120 桶在可见窗口只剩约 40 桶，
    右侧筹码柱太稀疏；360 桶后可见窗口约 120+ 桶、柱间距 ~3px（信号引擎
    chip_snapshots/_chip_feats_py 各自用 80/200 桶，不受影响）。"""
    bars = [r for r in rows
            if r.get("vol") and r.get("low") and r["low"] > 0
            and r["high"] >= r["low"]]
    if len(bars) < 30:
        return None
    lo_p = min(r["low"] for r in bars)
    hi_p = max(r["high"] for r in bars)
    if hi_p <= lo_p:
        return None
    step = (hi_p - lo_p) / nbin
    chips = [0.0] * (nbin + 1)
    med_vol = sorted(r["vol"] for r in bars)[len(bars) // 2] or 1.0
    for r in bars:
        t = min(0.20, max(0.002, 0.02 * (r["vol"] / med_vol)))
        chips = [c * (1.0 - t) for c in chips]
        b_lo = max(0, int((r["low"] - lo_p) / step))
        b_hi = min(nbin, int((r["high"] - lo_p) / step))
        if b_hi <= b_lo:
            chips[b_hi] += r["vol"]
        else:
            share = r["vol"] / (b_hi - b_lo + 1)
            for k in range(b_lo, b_hi + 1):
                chips[k] += share
    tot = sum(chips)
    if tot <= 0:
        return None
    mids = [lo_p + step * (k + 0.5) for k in range(nbin + 1)]
    if cur_price is None or cur_price <= 0:
        cur_price = bars[-1]["close"]
    avg_cost = sum(m * w for m, w in zip(mids, chips)) / tot
    profit = sum(w for m, w in zip(mids, chips) if m <= cur_price) / tot
    p5 = p95 = None
    cum = 0.0
    for m, w in zip(mids, chips):
        cum += w
        if p5 is None and cum >= tot * 0.05:
            p5 = m
        if cum >= tot * 0.95:
            p95 = m
            break

    # 支撑/压力：现价下方/上方“最密集的筹码带”。先做 3bin 平滑，
    # 避免单 bin 噪声；不用“局部极大值”是因为现价切在主峰侧面时，
    # 主力筹码带会落在下降坡上而非局部峰，反而被远处的小凸起压过
    # （实测 002241：全历史口径 压力 27.33 只有 0.7% 筹码，而
    # 24~25 元的厚筹码带被判为“非峰”）。
    def _strongest(below):
        sm = [(chips[max(0, k - 1)] + chips[k]
               + chips[min(nbin, k + 1)]) / 3.0 for k in range(nbin + 1)]
        best_w, best_m = -1.0, None
        for k, m in enumerate(mids):
            if (m < cur_price) == below and sm[k] > best_w:
                best_w, best_m = sm[k], m
        return best_m

    return {"bins": list(zip(mids, chips)),
            "avg_cost": round(avg_cost, 3), "profit": profit,
            "p5": p5, "p95": p95,
            "peak": max(zip(chips, mids))[1],
            "sup": _strongest(True), "res": _strongest(False),
            "cur": cur_price}


def calc_adx(rows, n=14):
    """DMI/ADX：+DI、-DI、ADX(n=14)。
    返回 (pdi, mdi, adx) 三条序列，预热期(2n左右)为 None。"""
    m = len(rows)
    pdi = [None] * m
    mdi = [None] * m
    adx = [None] * m
    if m < 2 * n + 1:
        return pdi, mdi, adx
    tr_s = pdm_s = ndm_s = 0.0
    dxs = []
    for i in range(1, m):
        h, l = rows[i]["high"], rows[i]["low"]
        hp, lp = rows[i - 1]["high"], rows[i - 1]["low"]
        pc = rows[i - 1]["close"]
        up = h - hp
        dn = lp - l
        pdm = up if (up > dn and up > 0) else 0.0
        ndm = dn if (dn > up and dn > 0) else 0.0
        tr = max(h - l, abs(h - pc), abs(l - pc))
        if i <= n:
            tr_s += tr
            pdm_s += pdm
            ndm_s += ndm
        else:
            # Wilder 平滑
            tr_s = tr_s - tr_s / n + tr
            pdm_s = pdm_s - pdm_s / n + pdm
            ndm_s = ndm_s - ndm_s / n + ndm
        if i >= n and tr_s > 0:
            pdi[i] = 100.0 * pdm_s / tr_s
            mdi[i] = 100.0 * ndm_s / tr_s
            s = pdi[i] + mdi[i]
            dxs.append(100.0 * abs(pdi[i] - mdi[i]) / s if s > 0 else 0.0)
            if len(dxs) >= n:
                if adx[i - 1] is None:
                    adx[i] = sum(dxs[-n:]) / n
                else:
                    adx[i] = (adx[i - 1] * (n - 1) + dxs[-1]) / n
    return pdi, mdi, adx


def calc_boll(closes, n=20, k=2.0):
    """布林带：中轨=n日SMA，上下轨=中轨±k倍标准差。
    返回 (mid, up, low) 三条序列，预热期为 None。"""
    mid = sma_period(closes, n)
    up = [None] * len(closes)
    low = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        seg = closes[i - n + 1:i + 1]
        m = mid[i]
        sd = (sum((x - m) ** 2 for x in seg) / n) ** 0.5
        up[i] = m + k * sd
        low[i] = m - k * sd
    return mid, up, low


def _band_fit_score(rows, mas, vr_arr):
    """波段适合度评分（0-100）：用实时技术特征判断该股是否适合做短线波段。

    适合波段的特征：波动率适中、趋势明确、量能活跃、非单边阴跌。
    返回分越高越适合波段（≥60 判为适合）。
    """
    try:
        if len(rows) < 60:
            return 50.0
        closes = [r["close"] for r in rows]
        c = closes[-1]
        # 1) 波动率（近20日日收益标准差年化）：适中偏高加分
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        r20 = rets[-20:]
        import statistics
        vol = statistics.stdev(r20) if len(r20) > 1 else 0
        vol_score = 0.0
        # 日均波动 1%~3% 视为波段友好区间
        if 0.008 <= vol <= 0.04:
            vol_score = 25.0
        elif vol < 0.008:
            vol_score = 5.0          # 太死水，无波段空间
        else:
            vol_score = 15.0          # 波动过大，风险高
        # 2) 趋势强度：MA20 斜率 + 价格相对 MA20 位置
        ma20 = mas[20][-1] if mas[20][-1] else c
        ma20p = mas[20][-6] if len(mas[20]) > 6 and mas[20][-6] else ma20
        slope = (ma20 - ma20p) / ma20p if ma20p else 0
        dist = (c - ma20) / ma20 if ma20 else 0
        trend = 0.0
        if slope > 0.002 and 0 < dist < 0.06:      # 温和上行且未过度偏离
            trend = 30.0
        elif slope > 0 and dist > -0.03:
            trend = 20.0
        elif slope < -0.002:
            trend = 5.0                             # 单边下行，不适合波段
        else:
            trend = 15.0
        # 3) 量能活跃度：近期量比
        vr = vr_arr[-1] if vr_arr else None
        vol_active = 0.0
        if vr is None:
            vol_active = 15.0
        elif 0.8 <= vr <= 2.5:
            vol_active = 25.0                       # 量能活跃且未过度
        elif vr > 2.5:
            vol_active = 12.0                       # 放量过猛，注意见顶
        else:
            vol_active = 8.0                        # 缩量，波段乏力
        # 4) 非单边阴跌：近60日整体趋势
        ma60 = mas[60][-1] if mas[60][-1] else c
        long_trend = 0.0
        if c >= ma60:
            long_trend = 20.0
        else:
            long_trend = 5.0
        return round(vol_score + trend + vol_active + long_trend, 1)
    except Exception:
        return 50.0


def _trend_track_signals(disp_rows, mas, idx_chg_by_date, idx_chg_today):
    """长周期趋势跟踪信号（用于不适合波段的标的）。

    以 MA20 与 MA60 的金叉/死叉为主信号，叠加 MA20 斜率与大盘环境过滤；
    信号少而稳，持有周期长，避免阴跌中被频繁套牢。
    返回与现有多维信号相同格式的列表 [(index, date, "BUY"/"SELL", 理由)]。
    """
    signals = []
    if len(disp_rows) < 60:
        return signals
    closes = [r["close"] for r in disp_rows]
    ma20 = mas[20]
    ma60 = mas[60]
    prev_state = None          # 0=空仓/无趋势, 1=多头持有
    # 从近 120 根开始扫描
    start = max(1, len(disp_rows) - 120)
    for i in range(start, len(disp_rows)):
        m20 = ma20[i]
        m60 = ma60[i]
        if m20 is None or m60 is None or m20 <= 0 or m60 <= 0:
            continue
        c = closes[i]
        # MA20 斜率
        m20p = ma20[i - 1] if i >= 1 and ma20[i - 1] else m20
        slope20 = (m20 - m20p) / m20p if m20p else 0
        # MA60 前值
        m60p = ma60[i - 1] if i >= 1 and ma60[i - 1] else m60
        # 大盘环境过滤（只用当日大盘涨跌，避免引用“今日”数据造成前视）
        ic = idx_chg_by_date.get(disp_rows[i]["date"])
        idx_ok = True
        if ic is not None:
            idx_ok = ic > -1.2     # 当日大盘未大幅走弱
        # 金叉：MA20 上穿 MA60 且斜率向上
        if (m20 > m60 and m20p <= m60p
                and slope20 > 0 and idx_ok):
            if prev_state != 1:
                signals.append((i, disp_rows[i]["date"], "BUY",
                                "趋势金叉 MA20上穿MA60 且斜率向上"))
                prev_state = 1
        # 死叉：MA20 下穿 MA60，或趋势破坏
        elif m20 < m60 and m20p >= m60p:
            if prev_state == 1:
                signals.append((i, disp_rows[i]["date"], "SELL",
                                "趋势死叉 MA20下穿MA60"))
                prev_state = 0
    return signals


def _dedup_signals(signals):
    """压缩连续同向信号（同一方向只保留首条）。

    回测中持仓期的重复 BUY / 空仓期的重复 SELL 本就会被忽略，
    压缩后图上标注与「最近买卖信号」不再出现成串重复 B/S。"""
    out = []
    prev = None
    for s in signals:
        if s[2] != prev:
            out.append(s)
            prev = s[2]
    return out


def _exec_mode():
    """成交价口径（环境变量 EXEC_PX，默认 close）：
    close = 信号次日收盘成交（早盘信号，默认）；open = 信号次日开盘成交。"""
    return os.environ.get("EXEC_PX", "close").lower()


def _bt_simulate(rows, signals, rp):
    """单段事件回测：BUY开仓/SELL平仓 + ATR动态止损/移动止盈。

    早盘信号：信号在 T 日收盘生成，T+1 日收盘成交；止损单用 T-1 日 ATR
    设定，T 日盘中止损触发才是可执行的挂单（防前视）。
    返回指标 dict（含净值曲线 curve 与逐笔收益 trades_list）。"""
    n = len(rows)
    sig_map = {s[0] + 1: s[2] for s in signals if s[0] + 1 < n}
    # 计算ATR(14)用于止损
    atrs = [0.0] * n
    for i in range(14, n):
        atrs[i] = sum(max(rows[j]["high"] - rows[j]["low"],
                          abs(rows[j]["high"] - rows[j-1]["close"]),
                          abs(rows[j]["low"] - rows[j-1]["close"]))
                      for j in range(i - 13, i + 1)) / 14
    exec_open = _exec_mode() == "open"
    eq = 1.0
    entry = None
    highest = None  # 持仓期间最高价
    trades = []
    curve = []

    for i, r in enumerate(rows):
        c = r["close"]
        h = r["high"]
        l = r["low"]
        typ = sig_map.get(i)

        if entry is not None:
            prev_high = highest
            highest = max(highest, h) if highest else h
            # 止损单在前一日收盘后用 T-1 的 ATR 设定，T 日盘中触发合法
            atr_prev = atrs[i - 1] if i > 0 else 0.0
            atr_stop = entry - rp["atr_mult"] * atr_prev \
                if atr_prev > 0 else entry * 0.95
            trail_stop = prev_high * rp["trail_ratio"] \
                if prev_high > entry * rp["trail_trigger"] else atr_stop

            # 止损触发（日内最低触及止损价）
            if l <= trail_stop:
                exit_price = r["open"] if r["open"] <= trail_stop \
                    else trail_stop
                trades.append(exit_price / entry - 1)
                eq *= exit_price / entry
                entry = None
                highest = None
                curve.append(eq)
                continue

        if typ == "BUY" and entry is None and c:
            px_fill = ((r.get("open") or c) if exec_open else c)
            entry = px_fill
            highest = px_fill     # 成交时点之前的盘中高点不计入
        elif typ == "SELL" and entry:
            px_fill = ((r.get("open") or c) if exec_open else c)
            trades.append(px_fill / entry - 1)
            eq *= px_fill / entry
            entry = None
            highest = None
        curve.append(eq * (c / entry) if entry else eq)

    # 未平仓按最后收盘价计算
    floating = rows[-1]["close"] / entry - 1 if entry else None
    wins = len([t for t in trades if t > 0])
    losses = len([t for t in trades if t <= 0])

    import datetime
    d0 = datetime.date.fromisoformat(rows[signals[0][0]]["date"])
    d1 = datetime.date.fromisoformat(rows[-1]["date"])
    years = max((d1 - d0).days / 365.25, 1e-9)
    total = curve[-1] if curve else 1.0
    ann = total ** (1 / years) - 1 if total > 0 else -1.0

    peak = 0.0
    mdd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)

    avg_win = sum(t for t in trades if t > 0) / wins if wins else 0
    avg_loss = sum(t for t in trades if t <= 0) / losses if losses else 0
    return {
        "trades": len(trades) + (1 if floating is not None else 0),
        "closed": len(trades),
        "wins": wins,
        "losses": losses,
        "winrate": wins / len(trades) if trades else None,
        "total": total - 1,
        "ann": ann,
        "mdd": mdd,
        "floating": floating,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_loss": avg_win / abs(avg_loss) if avg_loss != 0 else float('inf'),
        "curve": curve,
        "trades_list": trades,
    }


def _rank_avg(vals):
    """平均秩（并列取平均），用于 Spearman IC。"""
    n = len(vals)
    order = sorted(range(n), key=lambda k: vals[k])
    r = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def _signal_ic(rows, signals, horizon=5):
    """信号方向(+1买/-1卖) 与未来 horizon 日收益的 Spearman IC。

    返回 (ic, n)：ic 为 None 表示样本不足/无区分度。"""
    pairs = []
    n_all = len(rows)
    for s in signals:
        i = s[0]
        if i + horizon >= n_all:
            continue
        c0, c1 = rows[i].get("close"), rows[i + horizon].get("close")
        if not c0 or not c1 or c0 <= 0:
            continue
        pairs.append((1.0 if s[2] == "BUY" else -1.0, c1 / c0 - 1.0))
    n = len(pairs)
    if n < 5:
        return None, n
    rx = _rank_avg([p[0] for p in pairs])
    ry = _rank_avg([p[1] for p in pairs])
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx <= 1e-12 or dy <= 1e-12:
        return None, n
    return num / (dx * dy), n


def _signal_forward_stats(rows, signals, horizons=(1, 5)):
    """BUY/SELL 信号后 H 日平均收益与上涨占比（方向验证）。"""
    out = {}
    n_all = len(rows)
    for typ in ("BUY", "SELL"):
        sigs = [s[0] for s in signals if s[2] == typ]
        rec = {}
        for h in horizons:
            rs = []
            for i in sigs:
                if i + h < n_all:
                    c0, c1 = rows[i].get("close"), rows[i + h].get("close")
                    if c0 and c1 and c0 > 0:
                        rs.append(c1 / c0 - 1.0)
            rec[h] = (len(rs),
                      (sum(rs) / len(rs)) if rs else None,
                      (len([x for x in rs if x > 0]) / len(rs)) if rs else None)
        out[typ] = rec
    return out


def backtest_signals(rows, signals, rp=None):
    """按买卖点信号模拟交易（早盘信号：信号在 T 日收盘生成，T+1 日收盘成交）。
    BUY开仓/SELL平仓，带ATR动态止损+移动止盈；止损单用 T-1 日 ATR 设定，
    T 日盘中止损触发才是可执行的挂单，避免用当日收盘信息判当日盘中。
    返回全期指标 + 训练集(前75%)/验证集(后25%)分段指标 + 信号IC
    + 信号后1/5日收益，供 GUI 展开显示。"""
    try:
        if not signals or len(rows) < 30:
            return None
        rp = rp or CFG.risk_params()
        out = _bt_simulate(rows, signals, rp)
        n = len(rows)
        split = max(30, int(n * 0.75))
        out["split_i"] = split if 0 < split < n else None
        out["train"] = out["val"] = None
        if 0 < split < n:
            tr_sigs = [s for s in signals if s[0] < split]
            va_sigs = [(s[0] - split,) + tuple(s[1:])
                       for s in signals if split <= s[0] < n]
            if tr_sigs:
                out["train"] = _bt_simulate(rows[:split], tr_sigs, rp)
            if va_sigs:
                out["val"] = _bt_simulate(rows[split:], va_sigs, rp)
        out["ic1"] = _signal_ic(rows, signals, 1)
        out["ic5"] = _signal_ic(rows, signals, 5)
        out["fwd"] = _signal_forward_stats(rows, signals)
        return out
    except Exception:
        log.exception("backtest_signals 回测失败")
        return None


def strategy_signals_full(rows, strat, industry=""):
    """按所选策略在传入 rows 上重算信号（工具→信号胜率回测用）。

    主图买卖点只展示近 250 根（性能/可读性），若直接拿展示信号做 75/25
    训练/验证切分，指标型策略信号会全部落在尾部；这里按消融选型同口径
    重算 raw 信号（调用方传近1000根，与 run_ablation 同窗），不做展示端压缩。"""
    algo = (strat or {}).get("algo", "composite")
    rp = (strat or {}).get("params") or CFG.risk_params()
    try:
        if algo == "composite":
            pre = _composite_precompute(rows)
            return _composite_signals(rows, rp, pre=pre)
        if algo == "l2_ind":
            return _sig_l2_industry(rows, industry=industry)
        if algo == "sector_rot":
            return _sig_sector_rot(rows, industry=industry)
        gen = {"macd": _sig_macd, "kdj": _sig_kdj, "rsi": _sig_rsi,
               "boll": _sig_boll, "ma_trend": _sig_ma_trend,
               "l1_pattern": _sig_l1_pattern,
               "chip_peak": _sig_chip_peak}.get(algo)
        return gen(rows) if gen else []
    except Exception:
        log.exception("策略信号生成失败 %s", algo)
        return []


def _l1_up_prob_last(rows):
    """最新一日的 L1 形态上行概率（与 v4 因子 l1_up 完全同口径，防前视）。
    样本不足返回 None。"""
    n = len(rows)
    closes = [r["close"] for r in rows]
    L = logret(closes)
    W = W_WINDOW
    t = n - 1
    if len(L) < 2 * W + 3 or t - W < W:
        return None
    cur = znorm(L[t - W:t])
    d_arr = _px_distances(L[:t], cur, W)    # 仅用 t 以前窗口（防前视）
    ks = [k for k in range(len(d_arr)) if k + W <= t - W]
    if len(ks) < 6:
        return None
    ks.sort(key=lambda k: d_arr[k])
    ups = tot = 0.0
    for k in ks[:CFG.TOPK]:
        j = k + W
        if j + 1 < n:
            tot += 1.0
            ups += 1.0 if closes[j + 1] > closes[j] else 0.0
    if tot < 5:
        return None
    return ups / tot


def _picks_ind_ctx(days=14):
    """最新行业5日等权收益（板块轮动荐股用）。
    返回 (map={industry: ret5}, med=全行业中位数, lead=前20%行业集合)。"""
    with db_conn() as conn:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_bars ORDER BY date DESC LIMIT ?",
            (days + 6,))]
        if len(dates) < 7:
            return {}, 0.0, set()
        ind_of = {c: (i or "") for c, i in
                  conn.execute("SELECT code, industry FROM stocks")}
        acc = {}
        for c, d, cl in conn.execute(
                "SELECT code, date, close FROM daily_bars "
                "WHERE date >= ? AND date <= ? ORDER BY code, date",
                (min(dates), dates[0])):
            acc.setdefault(c, []).append((d, cl))
    latest = dates[0]
    rets = {}
    for c, seq in acc.items():
        dd = [x[0] for x in seq]
        if latest not in dd:
            continue
        k = dd.index(latest)
        if k < 5:
            continue
        p0, p5 = seq[k][1], seq[k - 5][1]
        x = ind_of.get(c, "")
        if p0 and p5 and p0 > 0 and p5 > 0 and x:
            rets.setdefault(x, []).append(p0 / p5 - 1.0)
    m = {x: sum(v) / len(v) for x, v in rets.items() if v}
    vals = sorted(m.values())
    med = vals[len(vals) // 2] if vals else 0.0
    lead = set()
    if m:
        srt = sorted(m.items(), key=lambda kv: -kv[1])
        lead = {x for x, _ in srt[:max(1, len(srt) // 5)]}
    return m, med, lead


def daily_pick_score(rows, ind_ctx=None):
    """对最新一根K线做多维打分（与买卖点信号同构，权重同 CFG.IND_W）。
    v4.0.1 荐股优化（依据 v3.3/v4 全A实证，定位"以小博大"）：
    - RSI 改动量口径：超卖反弹假设不成立（反向状态 IC -0.098，仅13.8%个股为正），
      超卖不再加分，强势状态加分；
    - 新增 MA20/60 趋势维度（IC 0.228）与 L1 形态上行概率维度（IC 0.265）；
    - 新增爆发力（20日动量，10%~35%强势区加分、>35%过热减分）与量能扩张维度；
    - ind_ctx 非 None 时新增板块轮动维度（行业5日收益强势/前20%领先）；
    - 返回第4元素 gates：{"ma_trend": ±2/0} 供 daily_picks 做空头趋势闸门。
    返回 (score, reasons, band_fit_score, gates)。"""
    n = len(rows)
    if n < 60:
        return None
    closes = [r["close"] for r in rows]
    dif, dea, _ = calc_macd(closes)
    k_, d_, _ = calc_kdj(rows)
    r6 = calc_rsi(closes, 6)
    b_mid, b_up, b_low = calc_boll(closes)
    pdi_a, mdi_a, adx_a = calc_adx(rows)
    mas = {20: sma_period(closes, 20), 60: sma_period(closes, 60)}
    vols_d = [r.get("vol") or 0.0 for r in rows]
    vr_arr = [vol_ratio_at(vols_d, k) for k in range(n)]
    i = n - 1
    if None in (dif[i], dea[i], dif[i - 1], dea[i - 1]):
        return None
    sc = 0
    reasons = []

    def _wadd(dim, pts, reason=None):
        nonlocal sc
        sc += int(round(pts * CFG.IND_W.get(dim, 1.0)))
        if reason and pts:
            reasons.append(reason)

    if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
        _wadd("MACD", 2, "MACD金叉")
    elif dif[i] > dea[i]:
        _wadd("MACD", 1, "DIF>DEA")
    elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
        _wadd("MACD", -2, "MACD死叉")
    else:
        _wadd("MACD", -1)
    if k_[i - 1] <= d_[i - 1] and k_[i] > d_[i] and k_[i] < 45:
        _wadd("KDJ", 2, "KDJ低位金叉")
    elif k_[i] > d_[i]:
        _wadd("KDJ", 1)
    elif k_[i - 1] >= d_[i - 1] and k_[i] < d_[i] and k_[i] > 65:
        _wadd("KDJ", -2, "KDJ高位死叉")
    else:
        _wadd("KDJ", -1)
    if r6[i] is not None and r6[i - 1] is not None:
        # v4.0.1：动量口径（超卖=弱势延续不加分，强势=延续加分）
        if r6[i] < 30:
            _wadd("RSI", -1, "RSI超卖弱势(动量)")
        elif r6[i] > 70:
            _wadd("RSI", 1, "RSI强势(动量)")
    c, cp = rows[i]["close"], rows[i - 1]["close"]
    v5 = sum(vols_d[max(0, i - 5):i]) / max(1, min(5, i))
    vr_d = vols_d[i] / v5 if v5 > 0 else 0.0
    if vr_d > 1.5 and c > cp:
        _wadd("量价", 1, "放量上涨")
    elif vr_d > 1.5 and c < cp:
        _wadd("量价", -1, "放量下跌")
    ma20, ma20p = mas[20][i], mas[20][i - 1]
    if ma20 and ma20p:
        if c > ma20 and ma20 > ma20p:
            _wadd("MA20", 1)
        elif c < ma20 and ma20 < ma20p:
            _wadd("MA20", -1)
    ma60, ma60p = mas[60][i], mas[60][i - 1]
    ma_trend = 0
    if ma20 and ma20p and ma60 and ma60p:
        # v3.3 全A实证：MA20/60趋势状态 IC 0.228（87%个股为正）
        if c > ma20 > ma60 and ma20 > ma20p:
            ma_trend = 2
            _wadd("MA趋势", 2, "MA20/60多头趋势")
        elif c < ma20 < ma60 and ma20 < ma20p:
            ma_trend = -2
            _wadd("MA趋势", -2, "MA20/60空头趋势")
    # 爆发力：20日动量（以小博大核心；极端过热反向，bias20 极端延伸 IC 为负）
    if i >= 20 and closes[i - 20]:
        c20 = c / closes[i - 20] - 1.0
        if 0.10 <= c20 < 0.35:
            _wadd("爆发力", 2, "20日强势+%.0f%%" % (c20 * 100))
        elif 0.05 <= c20 < 0.10:
            _wadd("爆发力", 1, "20日强势+%.0f%%" % (c20 * 100))
        elif c20 >= 0.35:
            _wadd("爆发力", -2, "20日过热+%.0f%%" % (c20 * 100))
        elif c20 <= -0.15:
            _wadd("爆发力", -1, "20日弱势%.0f%%" % (c20 * 100))
    # 量能扩张：5日均量显著放大且收涨（突破期特征）
    if i >= 20:
        v20m = sum(vols_d[i - 19:i + 1]) / 20.0
        v5m = sum(vols_d[max(0, i - 4):i + 1]) / max(1, min(5, i + 1))
        if v20m > 0 and v5m / v20m > 1.5 and c > cp:
            _wadd("量能", 1, "量能扩张")
    # 板块轮动：行业5日收益强势 / 前20%领先
    if ind_ctx:
        r5 = ind_ctx.get("r5")
        if r5 is not None:
            if r5 > ind_ctx.get("med", 0.0):
                _wadd("板块", 1, "板块强势")
            if ind_ctx.get("lead"):
                _wadd("板块", 1, "板块前20%")
    try:
        # 只用尾部160根算筹码快照：全量算1600只需数分钟且饿死GIL卡界面
        snap = chip_snapshots(rows[-160:], tail=1).get(rows[i]["date"])
        if snap:
            sup_i, res_i = snap[0], snap[1]
            if sup_i and c <= sup_i * 1.01:
                _wadd("筹码", 1, "贴近支撑")
            elif res_i and c >= res_i * 0.99:
                _wadd("筹码", -1, "贴近压力")
    except Exception:
        pass
    if None not in (b_up[i], b_low[i]):
        if c < b_low[i]:
            _wadd("布林带", 1, "布林下轨超卖")
        elif c > b_up[i]:
            _wadd("布林带", -1, "布林上轨超买")
    a_i, p_i, m_i = adx_a[i], pdi_a[i], mdi_a[i]
    if None not in (a_i, p_i, m_i) and a_i >= 20:
        if p_i > m_i:
            _wadd("ADX", 1, "ADX趋势偏多" if a_i >= 25 else None)
        elif m_i > p_i:
            _wadd("ADX", -1)
    try:
        # L1 形态上行概率（v4 因子同口径，IC 0.265 全A最强）
        p_up = _l1_up_prob_last(rows)
        if p_up is not None:
            if p_up >= 0.6:
                _wadd("形态", 2, "L1形态上行%d%%" % round(p_up * 100))
            elif p_up <= 0.4:
                _wadd("形态", -2, "L1形态上行%d%%" % round(p_up * 100))
    except Exception:
        pass
    band = _band_fit_score(rows, mas, vr_arr)
    return sc, reasons, band, {"ma_trend": ma_trend}


def daily_picks(progress=None, top_n=20, min_bars=120):
    """每日荐股：纯本地缓存扫描，不联网。
    返回 [(code, name, close, chg_pct, score, reasons, band)] 按 score 降序。
    深历史库（全市场×1000+根）不能整库载入内存：先按根数初筛，
    再分块只取每只尾部400根。"""
    with db_conn() as conn:
        names = {r[0]: r[1] for r in conn.execute(
            "SELECT code, name FROM stocks").fetchall()}
        ind_of = {c: (i or "") for c, i in
                  conn.execute("SELECT code, industry FROM stocks")}
        codes = [r[0] for r in conn.execute(
            "SELECT code FROM daily_bars GROUP BY code "
            "HAVING COUNT(*) >= ? AND MAX(date) >= "
            "(SELECT date(MAX(date), '-10 day') FROM daily_bars)",
            (min_bars,)).fetchall()]
        try:
            delisted = {r[0] for r in conn.execute(
                "SELECT code FROM delisted").fetchall()}
        except sqlite3.OperationalError:
            delisted = set()
    ind5_map, ind5_med, ind5_lead = _picks_ind_ctx()
    CH = 500
    cands = []
    for i in range(0, len(codes), CH):
        chunk = codes[i:i + CH]
        with db_conn() as conn:
            ph = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT code,date,open,high,low,close,vol FROM ("
                f"  SELECT code,date,open,high,low,close,vol,"
                f"         ROW_NUMBER() OVER (PARTITION BY code "
                f"                            ORDER BY date DESC) rn"
                f"  FROM daily_bars WHERE code IN ({ph})"
                f") WHERE rn<=400 ORDER BY code, date", chunk).fetchall()
        by = {}
        for c, d, o, h, l, cl, v in rows:
            by.setdefault(c, []).append(
                {"date": d, "open": o, "high": h, "low": l,
                 "close": cl, "vol": v or 0.0})
        cands += [(c, r) for c, r in by.items()
                  if len(r) >= min_bars and not _is_etf(c)
                  and not c.startswith('bj')          # 北交所K线源不支持
                  and c not in delisted               # 退市登记
                  and r[-1]['close'] and r[-1]['close'] >= 2]   # 剔除仙股
        if progress:
            progress(f"荐股载入 {min(i + CH, len(codes))}/{len(codes)}")
    picks = []
    total = len(cands)
    t_start = time.time()
    for k, (code, rws) in enumerate(cands):
        if progress and k % 40 == 0:
            progress(f"荐股扫描 {k}/{total}")
            time.sleep(0.01)          # 让出GIL，防止界面卡死
        if time.time() - t_start > 120:
            break                     # 2分钟硬熔断：宁可少扫不卡界面
        try:
            _ic = ind_of.get(code, "")
            r = daily_pick_score(rws[-400:], ind_ctx={
                "r5": ind5_map.get(_ic), "med": ind5_med,
                "lead": _ic in ind5_lead})
        except Exception:
            continue
        if r is None:
            continue
        score, reasons, band, gates = r
        nm = names.get(code, "")
        if "ST" in nm.upper() or "退" in nm:
            continue             # ST/退市：流动性风险，以小博大不碰
        if not pick_allowed(code, ind_of.get(code, "")):
            continue             # 荐股权限（设置内配置的板块/行业）
        if gates.get("ma_trend", 0) <= -2:
            continue             # 空头趋势闸门（MA20/60趋势 IC 0.228，最强信号）
        if score < CFG.risk_params()["buy_th"]:
            continue
        chg = (rws[-1]["close"] / rws[-2]["close"] - 1) * 100 \
            if rws[-1]["close"] and rws[-2]["close"] else 0.0
        picks.append((code, names.get(code, code[-6:]),
                      rws[-1]["close"], chg, score,
                      " ".join(reasons) or "-", band))
    picks.sort(key=lambda x: -x[4])
    if progress:
        progress(f"荐股完成：{len(picks)} 只入围，取Top{top_n}")
    return picks[:top_n]


def chip_snapshots(rows, nbin=80, tail=120):
    """逐日演化筹码分布，返回 {日期: (支撑价, 压力价, 获利比例)}（仅尾部tail天）。"""
    bars = [r for r in rows
            if r.get("vol") and r.get("low") and r["low"] > 0
            and r["high"] >= r["low"]]
    if len(bars) < 30:
        return {}
    # 因果网格：bin 边界/换手基准只用评估窗口之前的热身历史，
    # 不用未来价格极值（否则历史某日的筹码分布含未来信息）
    rec_from = len(bars) - min(tail, len(bars))
    base = bars[:rec_from] if rec_from >= 30 else []
    if not base:
        return {}
    lo_p = min(r["low"] for r in base)
    hi_p = max(r["high"] for r in base)
    if hi_p <= lo_p:
        return {}
    step = (hi_p - lo_p) / nbin
    mids = [lo_p + step * (k + 0.5) for k in range(nbin + 1)]
    chips = [0.0] * (nbin + 1)
    med_vol = sorted(r["vol"] for r in base)[len(base) // 2] or 1.0
    out = {}
    for idx, r in enumerate(bars):
        t = min(0.20, max(0.002, 0.02 * (r["vol"] / med_vol)))
        chips = [c * (1.0 - t) for c in chips]
        b_lo = max(0, int((r["low"] - lo_p) / step))
        b_hi = min(nbin, int((r["high"] - lo_p) / step))
        if b_hi <= b_lo:
            chips[b_hi] += r["vol"]
        else:
            share = r["vol"] / (b_hi - b_lo + 1)
            for k in range(b_lo, b_hi + 1):
                chips[k] += share
        if idx < rec_from:
            continue
        tot = sum(chips)
        if tot <= 0:
            continue
        c = r["close"]
        profit = sum(w for m, w in zip(mids, chips) if m <= c) / tot

        def _peaks():
            return [(mids[k], chips[k])
                    for k in range(1, nbin)
                    if chips[k] > chips[k - 1] and chips[k] >= chips[k + 1]
                    and chips[k] > 0]

        def _strongest(below):
            pk = _peaks()
            cand = [(m, w) for m, w in pk
                    if (m < c) == below]
            if not cand:
                bw, bm = -1.0, None
                for w, m in zip(chips, mids):
                    if (m < c) == below and w > bw:
                        bw, bm = w, m
                return bm
            return max(cand, key=lambda x: x[1])[0]

        out[r["date"]] = (_strongest(True), _strongest(False), profit)
    return out


def pct(vals, p):
    s = sorted(vals)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    f = k - lo
    return s[lo] * (1 - f) + s[hi] * f


def znorm(win):
    m = sum(win) / len(win)
    sd = (sum((x - m) ** 2 for x in win) / len(win)) ** 0.5 or 1e-12
    return [(x - m) / sd for x in win]


def logret(seq, is_etf=False):
    """计算对数收益率序列。
    对于ETF，除权除息会导致前复权价格跳变，对异常跳变进行平滑处理。"""
    rets = [math.log(seq[i + 1] / seq[i]) for i in range(len(seq) - 1)]
    if is_etf and len(rets) > 0:
        # 计算收益率的中位数和标准差
        import statistics
        median = statistics.median(rets)
        # 计算绝对偏差的中位数（MAD），比标准差更稳健
        mad = statistics.median([abs(r - median) for r in rets])
        # 对超过5倍MAD的异常收益率进行平滑（截断到±5倍MAD）
        threshold = 5 * mad if mad > 0 else 0.1
        rets = [max(min(r, median + threshold), median - threshold) for r in rets]
    return rets


# v6.1.5 热修⑦：原为 `W_WINDOW, TOPK = 10, 10`，会在导入时覆盖
# _load_predict_cfg() 读入的 ini 值 → 设置界面显示 W=20 实际跑 W=10，
# 且「保存并应用」只在当次进程生效、重启又回 10。现改为与 CFG 同步。
W_WINDOW, TOPK = CFG.W_WINDOW, CFG.TOPK

# ---- 三级样本池：L1自身 / L2同行业 / L3同市值层，融合权重 ----
LV_W = dict(CFG.LV_W)
LV_LABEL = {"L1": "自身历史", "L2": "同行业", "L3": "同市值层"}


def wpct(pairs, p):
    """加权分位数：pairs=[(值,权重)]，p∈{10..90}。"""
    pairs = sorted(pairs)
    tot = sum(w for _, w in pairs) or 1.0
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= p / 100 * tot:
            return v
    return pairs[-1][0]


# ---- 多维匹配扩展特征 ----

def rsi_at(closes, i, n=14):
    """截至第 i 日（含）的简单RSI。样本不足返回 None。"""
    if i < n or n <= 0:
        return None
    gains = losses = 0.0
    for k in range(i - n + 1, i + 1):
        ch = closes[k] - closes[k - 1]
        if ch > 0:
            gains += ch
        else:
            losses -= ch
    if gains + losses <= 0:
        return None
    if losses <= 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def vola_at(rets, i, n=10):
    """截至第 i 日（含）近 n 日对数收益标准差（日波动率）。"""
    if i < n - 1 or n <= 1:
        return None
    seg = rets[i - n + 1:i + 1]
    m = sum(seg) / n
    return (sum((x - m) ** 2 for x in seg) / n) ** 0.5


def candle_feats(rows, i):
    """第 i 根K线结构特征：(实体方向占比, 上影占比, 下影占比, 收盘位置)。
    全部归一到 -1..1 / 0..1，量纲无关。"""
    r = rows[i]
    o, h, l, c = r.get("open"), r.get("high"), r.get("low"), r.get("close")
    if None in (o, h, l, c) or o <= 0 or c <= 0:
        return None
    rng = h - l
    if rng <= 1e-9:
        return (0.0, 0.0, 0.0, 0.5)
    body = (c - o) / rng
    up_sh = (h - max(o, c)) / rng
    dn_sh = (min(o, c) - l) / rng
    pos = (c - l) / rng
    return (body, up_sh, dn_sh, pos)


def volchg_at(vols, i, n=5):
    """量变特征：log(近n日均量 / 前n日均量)。"""
    if i < 2 * n - 1:
        return None
    cur = sum(vols[i - n + 1:i + 1]) / n
    prev = sum(vols[i - 2 * n + 1:i - n + 1]) / n
    if prev <= 0 or cur <= 0:
        return None
    return math.log(cur / prev)


def weekly_ctx(rows, i, n=4):
    """周线环境：截至第 i 日最近 n 周（每周5根K线近似）的周收益列表，
    最近一周在前。样本不足返回 None。"""
    if i + 1 < n * 5:
        return None
    out = []
    for k in range(n):
        e = i + 1 - 5 * k
        s = e - 5
        if s < 0:
            return None
        c0 = rows[s].get("close")
        c1 = rows[e - 1].get("close")
        if not c0 or c0 <= 0 or not c1:
            return None
        out.append(c1 / c0 - 1)
    return out


def _cur_context(rows, rets, vols, closes):
    """计算「今日」的多维匹配特征向量（供 L1/L2/L3 共用）。"""
    i = len(rows) - 1
    return {
        "struct": candle_feats(rows, i),
        "vola": vola_at(rets, i),
        "rsi": rsi_at(closes, i),
        "volchg": volchg_at(vols, i),
        "weekly": weekly_ctx(rows, i, CFG.WEEKLY_N),
    }


def _dist_extra(d_cur, d_i):
    """样本与今日的扩展特征距离（结构+波动率+RSI+量变+周线）。
    任一特征缺失时该项取中性惩罚值，保证不同样本分数可比。
    d_cur 为 None（如渐进加载早期未计算今日特征）时全部按缺失处理。"""
    d = 0.0
    d_cur = d_cur or {}
    # K线结构（4维欧氏距离/4）
    if d_cur.get("struct") is not None and d_i.get("struct") is not None:
        a, b = d_cur["struct"], d_i["struct"]
        d += CFG.STRUCT_W * (sum((x - y) ** 2 for x, y in zip(a, b)) / len(a)) ** 0.5
    else:
        d += CFG.STRUCT_W * 0.5
    # 波动率（对数比，封顶1.5）
    if d_cur.get("vola") is not None and d_i.get("vola") is not None \
            and d_cur["vola"] > 0 and d_i["vola"] > 0:
        d += CFG.VOLA_W * min(abs(math.log(d_cur["vola"] / d_i["vola"])), 1.5)
    else:
        d += CFG.VOLA_W * 0.5
    # RSI（0-100 差 / 100）
    if d_cur.get("rsi") is not None and d_i.get("rsi") is not None:
        d += CFG.RSI_W * abs(d_cur["rsi"] - d_i["rsi"]) / 100.0
    else:
        d += CFG.RSI_W * 0.5
    # 量变（对数比，封顶1.5）
    if d_cur.get("volchg") is not None and d_i.get("volchg") is not None:
        d += CFG.VOLCHG_W * min(abs(d_cur["volchg"] - d_i["volchg"]), 1.5)
    else:
        d += CFG.VOLCHG_W * 0.5
    # 周线环境（周收益欧氏距离，×5 放大到日级别量纲附近）
    if d_cur.get("weekly") is not None and d_i.get("weekly") is not None:
        a, b = d_cur["weekly"], d_i["weekly"]
        d += CFG.WEEKLY_W * min((sum((x - y) ** 2 for x, y in zip(a, b))
                                 / len(a)) ** 0.5 * 5, 1.5)
    else:
        d += CFG.WEEKLY_W * 0.5
    return d


def _dynamic_lv_weights(levels):
    """按层级的有效样本质量动态分配权重，使用Top-K中位距离而非单个best。"""
    keys = ("L1", "L2", "L3")
    pri_tot = sum(max(0.0, LV_W.get(k, 0.0)) for k in keys) or 1.0
    pri = {k: max(0.0, LV_W.get(k, 0.0)) / pri_tot for k in keys}
    if not CFG.DYNAMIC_LV_W:
        return pri
    raw = {}
    for k, smp in levels:
        scores = sorted(float(s.get("similarity_score", 9.0)) for s in smp
                        if math.isfinite(float(s.get("similarity_score", 9.0))))
        if not scores:
            raw[k] = 0.0
            continue
        med = scores[len(scores) // 2]
        quality = math.exp(-min(med, 6.0) / 1.25)
        n_factor = min(1.0, math.sqrt(len(scores) / 6.0))
        raw[k] = quality * (0.65 + 0.35 * n_factor)
    tot = sum(raw.values())
    if tot <= 0:
        return pri
    dyn = {k: raw.get(k, 0.0) / tot for k in keys}
    st = min(max(CFG.DYN_LV_STRENGTH, 0.0), 1.0)
    return {k: (1.0 - st) * pri[k] + st * dyn.get(k, 0.0) for k in keys}


def _pool_match(pool_rows, cur, vr_now, idx_chg_by_date, idx_chg_today,
                topk=None, candidate_topk=None, cur_ctx=None):
    """多股票池历史窗口匹配；只用样本T及以前的信息，目标收益绝不参与筛选。
    topk 缺省在调用时读全局 TOPK（定义期默认值会在「保存并应用」后过期）。"""
    W = W_WINDOW
    topk = topk or TOPK
    candidate_topk = candidate_topk or CFG.CANDIDATE_TOPK
    info, sims = {}, []
    for code, rows in pool_rows:
        closes = [r["close"] for r in rows]
        rets = logret(closes, is_etf=_is_etf(code))
        if len(rets) < W + 3:
            continue
        vols = [r.get("vol") or 0.0 for r in rows]
        vr_arr = [vol_ratio_at(vols, k) for k in range(len(vols))]
        info[code] = (rows, vr_arr)
        smp_ctx = {
            "struct": [candle_feats(rows, k) for k in range(len(rows))],
            "vola": [vola_at(rets, k) for k in range(len(rets))],
            "rsi": [rsi_at(closes, k) for k in range(len(closes))],
            "volchg": [volchg_at(vols, k) for k in range(len(vols))],
            "weekly": [weekly_ctx(rows, k, CFG.WEEKLY_N) for k in range(len(rows))],
        }
        last_i = len(rets) - W
        # d_px 向量化（_px_distances 内部 numpy 可用时快约50倍）；
        # s_v/s_i 廉价可全量算；d_x 较贵 → 先按 d_px+s_v+s_i 预筛
        # top(4K+80) 再补算 d_x（d_x 上界约3.6，截断误差可忽略）
        d_px_arr = _px_distances(rets, cur, W)
        pre = []
        for k, d_px in enumerate(d_px_arr):
            i = k + W
            if i > last_i:
                break
            vr_i = vr_arr[i]
            s_v = 0.6 * min(abs(math.log(max(vr_now,1e-6)/max(vr_i,1e-6))),2.5) if vr_now is not None and vr_i is not None else 0.30
            ic = idx_chg_by_date.get(rows[i]["date"])
            s_i = min(1.5,0.3*abs(ic-idx_chg_today)) if ic is not None and idx_chg_today is not None else 0.40
            pre.append((d_px + s_v + s_i, i, d_px, s_v, s_i))
        pre.sort(key=lambda x: x[0])
        for base_s, i, d_px, s_v, s_i in pre[:candidate_topk * 4 + 80]:
            d_x = _dist_extra(cur_ctx,{k:smp_ctx[k][i] for k in smp_ctx})
            sims.append((base_s+d_x,i,code,d_px,s_v,s_i))
    if not info:
        return []
    candidate = heapq.nsmallest(candidate_topk,sims,key=lambda x:x[0])
    if not candidate:
        return []
    best = candidate[0][0]
    out=[]
    for score,i,code,d_px,s_v,s_i in candidate:
        rows,vr_arr=info[code]; r=rows[i]; ic=idx_chg_by_date.get(r["date"])
        # 消融回测(n=1480)：指数加权命中50.4%/IC-0.043 → 等权52.4%/IC+0.020，
        # 默认关闭；如需启用把 CFG.SIMILARITY_WEIGHTING 改回 True
        weight = math.exp(-min(max(0.0, score - best), 6.0) / 0.9) \
            if CFG.SIMILARITY_WEIGHTING else 1.0
        if CFG.QUALITY_FILTER and d_px>CFG.SIMILARITY_CUTOFF:
            weight*=0.35
        # 时间衰减（消融回测：保留衰减 IC+0.020 vs 无衰减 -0.024）
        if CFG.TIME_DECAY_ENABLED:
            try:
                age=(time.mktime(time.strptime(time.strftime("%Y-%m-%d"),"%Y-%m-%d"))
                     -time.mktime(time.strptime(r["date"],"%Y-%m-%d")))/86400.0
                if age>CFG.TIME_DECAY_DAYS:
                    weight*=max(CFG.TIME_DECAY_RATE,
                                1.0-(age-CFG.TIME_DECAY_DAYS)/365.0*0.5)
            except (ValueError, TypeError):
                pass
        s={"t_date":r["date"],"vr":vr_arr[i],
           "idx_chg":ic-idx_chg_today if ic is not None and idx_chg_today is not None else None,
           "gap":rows[i+1]["open"]/r["close"]-1 if i+1<len(rows) else None,
           "code":code,"_match_i":i,"similarity_score":score,
           "distance_px":d_px,"distance_vol":s_v,"distance_idx":s_i,"weight":weight}
        for d in range(1,11):
            if i+d<len(rows):
                nd=rows[i+d]; prev_c=rows[i+d-1]["close"]; op=nd.get("open") or prev_c
                s[f"n{d}_date"]=nd["date"]; s[f"n{d}_cl"]=nd["close"]/prev_c-1
                s[f"n{d}_hi"]=nd["high"]/prev_c-1; s[f"n{d}_lo"]=nd["low"]/prev_c-1
                s[f"n{d}_oc"]=nd["close"]/op-1 if op>0 else None
                s[f"n{d}_oh"]=nd["high"]/op-1 if op>0 else None
                s[f"n{d}_ol"]=nd["low"]/op-1 if op>0 else None
            else:
                for suf in ("date","cl","hi","lo","oc","oh","ol"): s[f"n{d}_{suf}"]=None
        out.append(s)
    # v6.1.5 热修⑦：同股样本窗口间隔取 W（互不重叠；原 W//2 重叠一半）
    selected=[]; per_code={}; min_gap=max(3,W)
    for s in sorted(out,key=lambda x:x["similarity_score"]):
        c=s["code"]; i=s["_match_i"]
        if any(abs(i-j)<min_gap for j in per_code.get(c,[])): continue
        per_code.setdefault(c,[]).append(i); selected.append(s)
        if len(selected)>=topk: break
    return selected


def market_phase_text(time_str):
    """行情快照时间(YYYYMMDDHHMMSS...) -> 'HH:MM 市场阶段'。

    快照日期非今日时（休市/停牌/数据未更新）标注日期，避免把上一交易日的
    16:14 显示成"今天已收盘"（时间不匹配的观感问题）。"""
    s = time_str or ""
    try:
        hhmm = int(s[8:12])
    except ValueError:
        return "时间未知"
    hm = f"{hhmm // 100}:{hhmm % 100:02d}"
    snap_d = s[:8].replace("-", "")
    if (len(snap_d) == 8 and snap_d.isdigit()
            and snap_d != time.strftime("%Y%m%d")):
        return f"{snap_d[4:6]}-{snap_d[6:8]} {hm} 收盘（快照非今日）"
    if hhmm < 915:
        return hm + " 盘前"
    if hhmm < 925:
        return hm + " 集合竞价"
    if hhmm < 1130 or 1300 <= hhmm < 1500:
        return hm + " 盘中交易"
    if hhmm < 1300:
        return hm + " 午间休市"
    return hm + " 已收盘"


def _load_pools(pool_info, cur, vr_now, idx_chg_by_date, idx_chg_today,
                progress=None, cur_ctx=None):
    """取 L2/L3 池K线（走缓存增量）并跑匹配，返回 {"L2":[...], "L3":[...]}。"""
    out = {}
    t0 = time.time()
    lv_keys = ("L2", "L3") if CFG.ENABLE_L3 else ("L2",)
    for key in lv_keys:
        if time.time() - t0 > 60:
            break
        codes = pool_info.get(key.lower()) or []
        if not codes:
            continue
        try:
            if progress:
                progress(f"回填{LV_LABEL[key]}池 {len(codes)}只...")
            prefetch(codes, workers=10, progress=progress)
            # 批量读缓存（单次连接）
            with db_conn() as conn:
                cached = _db_rows_batch(conn, codes)
            pool_rows = [(c, r) for c, r in cached.items() if len(r) >= 130]
            smp = _pool_match(pool_rows, cur, vr_now, idx_chg_by_date,
                              idx_chg_today, cur_ctx=cur_ctx)
            if smp:
                out[key] = smp
        except Exception:
            log.warning("加载%s池失败(跳过)", LV_LABEL[key], exc_info=True)
            continue
    return out


def load_pools_progressive(full, ctx, progress=None, batch=12):
    """逐步加载 L2/L3 样本池并实时产出融合预测。

    ctx 为 analyze 返回的 _ctx（含 o_today/pre_open/live/src/cur/vr_now/
    idx_chg_by_date/idx_chg_today/gap_today/prev_close 等）。
    每加载完一批 L2 或 L3，就用已累计的样本重算一次预测并 yield
    (level_map, t_pred, pred, clamped, tpred_bar, pool_note)；
    调用方在 GUI 主线程据此刷新预测K线，实现“边跑边更新”。
    """
    o_today = ctx["o_today"]
    pre_open = ctx["pre_open"]
    live = ctx["live"]
    src = ctx["src"]
    # 取样本池代码（复用缓存/失败记忆）
    pool_info = None
    try:
        pool_info = pool_codes(full)
    except Exception:
        log.warning("load_pools_progressive: pool_codes 失败 %s",
                    full, exc_info=True)
        pool_info = None
    if not pool_info:
        return
    level_map = {}
    order = ([("L2", pool_info.get("l2") or [])]
             + ([("L3", pool_info.get("l3") or [])] if CFG.ENABLE_L3 else []))
    for key, codes in order:
        if not codes:
            continue
        t0 = time.time()
        acc_rows = []       # 本级的累计池K线，逐批变大，匹配随之变准
        matched_n = 0       # 上次跑匹配时的池大小（自适应降频）
        n_batches = (len(codes) + batch - 1) // batch
        for bi, i in enumerate(range(0, len(codes), batch), 1):
            chunk = codes[i:i + batch]
            if progress:
                progress(f"后台加载{LV_LABEL[key]}样本 "
                         f"{min(i + batch, len(codes))}/{len(codes)}只 "
                         f"({min((i + batch) * 100 // len(codes), 100)}%) "
                         f"第{bi}/{n_batches}批...")
            try:
                # 进度由上面的批次消息统一汇报，避免内层"缓存回填"刷屏
                prefetch(chunk, workers=8)
            except Exception:
                log.warning("样本池回填失败 %s(跳过)", chunk, exc_info=True)
            # 批量读缓存
            try:
                with db_conn() as conn:
                    cached = _db_rows_batch(conn, chunk)
            except Exception:
                cached = {}
            acc_rows += [(c, r) for c, r in cached.items() if len(r) >= 130]
            if progress:
                progress(f"后台加载{LV_LABEL[key]}样本 "
                         f"{len(acc_rows)}/{len(codes)}只有效 "
                         f"(第{bi}/{n_batches}批，"
                         f"{bi * 100 // n_batches}%)")
            # 池大了以后每批全量匹配太慢：新增≥24只有效K线才重跑一次
            if len(acc_rows) < matched_n + 24:
                continue
            matched_n = len(acc_rows)
            # 用已累计的全部本池K线跑匹配，样本随加载增多而变准
            try:
                smp = _pool_match(acc_rows, ctx["cur"], ctx["vr_now"],
                                  ctx["idx_chg_by_date"], ctx["idx_chg_today"],
                                  cur_ctx=ctx.get("cur_ctx"))
                if smp:
                    level_map[key] = smp
            except Exception:
                log.warning("池匹配失败 %s %s(跳过)", key, full,
                            exc_info=True)
            # 每次有新增样本就产出一次更新
            if level_map.get(key):
                levels = [("L1", src)] + [
                    (k, v) for k, v in sorted(level_map.items()) if v]
                t_pred, pred, clamped, tpred_bar = _fusion_prediction(
                    o_today, pre_open, live, levels)
                # 多日预测
                multi_pred = _multi_day_prediction(o_today, levels, max_days=CFG.PRED_MAX_DAYS)
                parts = [f"{LV_LABEL['L1']}{len(src)}"]
                parts += [f"{LV_LABEL[k]}{len(v)}"
                          for k, v in sorted(level_map.items()) if v]
                pool_note = "样本池: " + "+".join(parts)
                yield level_map, t_pred, pred, clamped, tpred_bar, pool_note, multi_pred
            if time.time() - t0 > 60:   # 单级超时保护（L3不限量后池更大）
                if progress:
                    progress(f"{LV_LABEL[key]}池加载超时(60s)，"
                             f"已用{len(acc_rows)}只K线继续")
                break
        if progress:
            progress(f"{LV_LABEL[key]}池完成: {len(level_map.get(key, []))}个匹配样本")


# ================= 策略消融引擎（多算法回测+防过拟合选型） =================
# 每次分析对该股近1000交易日做一次多算法消融回测：
#   候选 = MACD / KDJ / RSI / 布林带 / MA20-60趋势 / L1形态 / L2同行业+行业ETF /
#          筹码峰 / 板块轮动 / 多维评分×3风险档（全部 × 3 档风险参数）
# 防过拟合：前~75%训练集选策略，后~25%验证集只报告不参与选择（前视零容忍：
# 信号只用 T 日及以前数据，信号日收盘成交）。v6.1.5 热修②：选型再加"近端子窗
# 一致性"——最近 ~250 根也须排前列，否则回退该档「多维评分」（防风格切换失配；
# 近窗为时点可观测数据，n=1000 时即验证段，只做 top 门槛否决、不参与 rank 打分）。
# 结果缓存 meta 表，5日过期。

STRAT_TTL = 5 * 86400


def _strat_key(full):
    return "strategy:" + full


def load_strategy(full):
    """读策略缓存（meta表），5日过期返回 None。"""
    try:
        with db_conn() as conn:
            v = _get_meta(conn, _strat_key(full))
        if not v:
            return None
        d = json.loads(v)
        if time.time() - float(d.get("ts", 0)) > STRAT_TTL:
            return None
        return d
    except Exception:
        log.exception("load_strategy 失败(忽略)")
        return None


def save_strategy(full, strat):
    try:
        strat = dict(strat)
        strat["ts"] = time.time()
        with db_conn(commit=True) as conn:
            _set_meta(conn, _strat_key(full),
                      json.dumps(strat, ensure_ascii=False))
    except Exception:
        log.exception("save_strategy 失败(忽略)")


# ---- 各算法信号发生器（只用 T 日及以前数据，杜绝前视）----

def _sig_macd(rows):
    closes = [r["close"] for r in rows]
    dif, dea, _ = calc_macd(closes)
    out = []
    for i in range(9, len(rows)):
        if None in (dif[i], dea[i], dif[i - 1], dea[i - 1]):
            continue
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            out.append((i, rows[i]["date"], "BUY", "MACD金叉"))
        elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            out.append((i, rows[i]["date"], "SELL", "MACD死叉"))
    return out


def _sig_kdj(rows):
    k, d, _ = calc_kdj(rows)
    out = []
    for i in range(3, len(rows)):
        if None in (k[i], d[i], k[i - 1], d[i - 1]):
            continue
        if k[i - 1] <= d[i - 1] and k[i] > d[i] and k[i] < 45:
            out.append((i, rows[i]["date"], "BUY", "KDJ低位金叉"))
        elif k[i - 1] >= d[i - 1] and k[i] < d[i] and k[i] > 65:
            out.append((i, rows[i]["date"], "SELL", "KDJ高位死叉"))
    return out


def _sig_rsi(rows):
    closes = [r["close"] for r in rows]
    r6 = calc_rsi(closes, 6)
    out = []
    for i in range(1, len(rows)):
        if r6[i] is None or r6[i - 1] is None:
            continue
        if r6[i - 1] < 20 <= r6[i]:
            out.append((i, rows[i]["date"], "BUY", "RSI超卖回升"))
        elif r6[i - 1] > 80 >= r6[i]:
            out.append((i, rows[i]["date"], "SELL", "RSI超买回落"))
    return out


def _sig_boll(rows):
    closes = [r["close"] for r in rows]
    _, up, low = calc_boll(closes)
    out = []
    for i in range(1, len(rows)):
        if None in (up[i], low[i], up[i - 1], low[i - 1]):
            continue
        pc = rows[i - 1]["close"]
        c = rows[i]["close"]
        if pc <= low[i - 1] and c > low[i]:
            out.append((i, rows[i]["date"], "BUY", "布林下轨回升"))
        elif pc >= up[i - 1] and c < up[i]:
            out.append((i, rows[i]["date"], "SELL", "布林上轨回落"))
    return out


def _sig_ma_trend(rows):
    closes = [r["close"] for r in rows]
    ma20 = sma_period(closes, 20)
    ma60 = sma_period(closes, 60)
    out = []
    state = 0
    for i in range(60, len(rows)):
        if None in (ma20[i], ma60[i], ma20[i - 1], ma60[i - 1]):
            continue
        if ma20[i - 1] <= ma60[i - 1] and ma20[i] > ma60[i] and state != 1:
            out.append((i, rows[i]["date"], "BUY", "MA20上穿MA60"))
            state = 1
        elif ma20[i - 1] >= ma60[i - 1] and ma20[i] < ma60[i] and state != -1:
            out.append((i, rows[i]["date"], "SELL", "MA20下穿MA60"))
            state = -1
    return out


def _composite_precompute(rows, chip_tail=400, use_chips=True):
    """预计算多维评分所需指标，供同一只股票多档风险复用。"""
    closes = [r["close"] for r in rows]
    dif, dea, _ = calc_macd(closes)
    k_, d_, _ = calc_kdj(rows)
    r6 = calc_rsi(closes, 6)
    _, b_up, b_low = calc_boll(closes)
    pdi_a, mdi_a, adx_a = calc_adx(rows)
    ma20 = sma_period(closes, 20)
    vols_d = [r.get("vol") or 0.0 for r in rows]
    try:
        chip_snaps = chip_snapshots(rows, tail=chip_tail) if use_chips \
            else {}
    except Exception:
        chip_snaps = {}
    return (dif, dea, k_, d_, r6, b_up, b_low, pdi_a, mdi_a, adx_a,
            ma20, vols_d, chip_snaps)


def _composite_signals(rows, rp, idx_chg_by_date=None, chip_tail=400,
                       use_chips=True, pre=None):
    """多维评分信号（消融用，与GUI打分同构；筹码维度限尾段提速）。
    rp: 风险参数（buy_th/cooldown）。pre 为 _composite_precompute 结果，
    同一只股票多档复用可避免重复计算指标。返回 [(i,date,"BUY"/"SELL",reason)]。"""
    n = len(rows)
    if n < 60:
        return []
    if pre is None:
        pre = _composite_precompute(rows, chip_tail, use_chips)
    (dif, dea, k_, d_, r6, b_up, b_low, pdi_a, mdi_a, adx_a,
     ma20, vols_d, chip_snaps) = pre
    weak = idx_chg_by_date or {}
    buy_th = rp["buy_th"]
    out = []
    prev_dir = 0
    cooldown = 0
    start = max(1, n - 1000)
    for i in range(start, n):
        if cooldown > 0:
            cooldown -= 1
            continue
        if None in (dif[i], dea[i], dif[i - 1], dea[i - 1]):
            continue
        sc = 0

        def _wadd(dim, pts):
            return int(round(pts * CFG.IND_W.get(dim, 1.0)))

        sc += _wadd("MACD", 2 if (dif[i - 1] <= dea[i - 1] and dif[i] > dea[i])
                    else -2 if (dif[i - 1] >= dea[i - 1] and dif[i] < dea[i])
                    else 1 if dif[i] > dea[i] else -1)
        sc += _wadd("KDJ", 2 if (k_[i - 1] <= d_[i - 1] and k_[i] > d_[i]
                                 and k_[i] < 45)
                    else -2 if (k_[i - 1] >= d_[i - 1] and k_[i] < d_[i]
                                and k_[i] > 65)
                    else 1 if k_[i] > d_[i] else -1)
        if r6[i] is not None and r6[i - 1] is not None:
            sc += _wadd("RSI", 2 if (r6[i - 1] < 20 and r6[i] >= 20)
                        else -2 if (r6[i - 1] > 80 and r6[i] <= 80)
                        else 1 if r6[i] < 30 else -1 if r6[i] > 70 else 0)
        c, cp = rows[i]["close"], rows[i - 1]["close"]
        v5 = sum(vols_d[max(0, i - 5):i]) / max(1, min(5, i))
        vr_d = vols_d[i] / v5 if v5 > 0 else 0.0
        sc += _wadd("量价", 1 if (vr_d > 1.5 and c > cp)
                    else -1 if (vr_d > 1.5 and c < cp) else 0)
        m20, m20p = ma20[i], ma20[i - 1]
        if m20 and m20p:
            sc += _wadd("MA20", 1 if (c > m20 and m20 > m20p)
                        else -1 if (c < m20 and m20 < m20p) else 0)
        snap = chip_snaps.get(rows[i]["date"])
        if snap:
            sup_i, res_i = snap[0], snap[1]
            if sup_i and c <= sup_i * 1.01:
                sc += _wadd("筹码", 1)
            elif res_i and c >= res_i * 0.99:
                sc += _wadd("筹码", -1)
        if None not in (b_up[i], b_low[i]):
            sc += _wadd("布林带", 1 if c < b_low[i]
                        else -1 if c > b_up[i] else 0)
        a_i, p_i, m_i = adx_a[i], pdi_a[i], mdi_a[i]
        if None not in (a_i, p_i, m_i) and a_i >= 20:
            sc += _wadd("ADX", 1 if p_i > m_i else -1)
        day_weak = (weak.get(rows[i]["date"]) is not None
                    and weak[rows[i]["date"]] < CFG.WEAK_IDX_TH)
        th = buy_th + 1 if day_weak else buy_th
        if sc >= th and prev_dir <= 0:
            out.append((i, rows[i]["date"], "BUY", f"多维偏多({sc})"))
            prev_dir = 1
            cooldown = rp["cooldown"]
        elif sc <= CFG.SIGNAL_SCORE_SELL and prev_dir >= 0:
            out.append((i, rows[i]["date"], "SELL", f"多维偏空({sc})"))
            prev_dir = -1
            cooldown = rp["cooldown"]
    return out


ALGO_LABEL = {
    "l1_pattern": "L1形态上行概率",
    "l2_ind": "L2同行业+行业ETF",
    "macd": "MACD金叉/死叉",
    "kdj": "KDJ金叉/死叉",
    "rsi": "RSI超买超卖",
    "boll": "布林带回归",
    "ma_trend": "MA20/60趋势",
    "chip_peak": "筹码峰支撑",
    "sector_rot": "板块轮动",
    "composite": "多维评分",
}


def _px_distances(rets, cur, W):
    """滑窗z-normalize后与cur的欧氏距离：返回数组（窗口k=rets[k:k+W]）。
    numpy可用时向量化（快约50倍），否则退回纯Python循环。"""
    n = len(rets)
    if n < W:
        return []
    if np is not None:
        a = np.asarray(rets, dtype=np.float64)
        wins = np.lib.stride_tricks.sliding_window_view(a, W)
        mu = wins.mean(axis=1, keepdims=True)
        sd = wins.std(axis=1, keepdims=True)
        zn = (wins - mu) / np.maximum(sd, 1e-12)
        d = np.sqrt(((zn - np.asarray(cur)) ** 2).sum(axis=1))
        return d.tolist()
    out = []
    for k in range(n - W + 1):
        w = znorm(rets[k:k + W])
        out.append(math.sqrt(sum((a - b) ** 2 for a, b in zip(cur, w))))
    return out


def _sig_l1_pattern(rows, W=None, step=5, up_th=0.6, dn_th=0.4):
    """L1形态信号：每隔step日做一次自身历史形态匹配（只用该日以前数据），
    Top-K样本次日上行概率≥60%→BUY，≤40%→SELL。"""
    W = W or W_WINDOW
    closes = [r["close"] for r in rows]
    rets = logret(closes)
    if len(rets) < 2 * W + 3:
        return []
    out = []
    for i in range(W, len(rets) - W + 1, step):
        cur = znorm(rets[i - W:i])
        d_arr = _px_distances(rets[:i], cur, W)   # 仅用 i 以前窗口（防前视）
        cand = [(d_arr[k], k) for k in range(len(d_arr))
                if k + W <= i - W]                # 样本窗口完整结束于 i 之前
        if len(cand) < 6:
            continue
        cand.sort()
        tops = cand[:CFG.TOPK]
        ups = tot = 0
        for _, k in tops:
            j = k + W                              # 窗口次日起算结果
            if j < len(rows) - 1:
                tot += 1
                if rows[j + 1]["close"] > rows[j]["close"]:
                    ups += 1
        if tot < 5:
            continue
        p = ups / tot
        if p >= up_th:
            out.append((i, rows[i]["date"], "BUY",
                        f"L1形态上行{p*100:.0f}%"))
        elif p <= dn_th:
            out.append((i, rows[i]["date"], "SELL",
                        f"L1形态上行{p*100:.0f}%"))
    return out


# ---- v6.1 消融新增信号源：筹码峰 / 板块轮动（本地计算，AI 不参与）----

# 注意：与上面的 _SECTOR_CACHE（个股板块上下文缓存）区分，勿重名
_SECTOR_MOM_CACHE = {"ts": 0.0, "data": None}


_SECTOR_L2_CACHE = {"ts": 0.0, "data": None}

# 行业名与 ETF 简称匹配时要去掉的发行商后缀/噪声词
_ETF_VENDOR = ("ETF", "基金", "国泰", "华夏", "华宝", "易方达", "南方", "广发",
               "嘉实", "富国", "汇添富", "银华", "工银", "博时", "天弘", "华安",
               "招商", "鹏华", "建信", "景顺", "中欧", "万家", "摩根", "国联安",
               "中证", "上证", "深证", "指数", "LOF", "联接")


def _match_industry_etf(ind_names, etf_names):
    """行业 → 行业ETF 代码的启发式匹配（v6.1.3）。

    规则：ETF 名称去掉发行商/指数噪声词后，与行业名（去掉 Ⅱ/Ⅲ 后缀）互相包含，
    取名称最长的匹配（更具体）。返回 {industry: etf_code}。"""
    def norm(s):
        t = s or ""
        for w in _ETF_VENDOR:
            t = t.replace(w, "")
        return t.replace("Ⅱ", "").replace("Ⅲ", "").strip()
    out = {}
    for ind in ind_names:
        ni = norm(ind)
        if len(ni) < 2:
            continue
        best, best_len = None, -1
        for code, name in etf_names.items():
            ne = norm(name)
            if len(ne) < 2:
                continue
            if (ni in ne or ne in ni) and len(ne) > best_len:
                best, best_len = code, len(ne)
        if best:
            out[ind] = best
    return out


def sector_l2_series(max_age=1800):
    """L2 参照序列（同行业 + 行业ETF）（v6.1.3）。

    每个行业构造一条「行业指数」日收盘序列，优先用**同名行业ETF**（可直接交易、
    无成分股停牌噪声）；找不到匹配 ETF 时用**同行业个股等权收益累乘**合成。
    返回 (cal, {industry: close数组(归一化到首个有效值)}, {industry: etf_code或''})，
    仅用截至当日数据（因果）。"""
    now = time.time()
    if (_SECTOR_L2_CACHE["data"] is not None
            and now - _SECTOR_L2_CACHE["ts"] < max_age):
        return _SECTOR_L2_CACHE["data"]
    codes, cal, C, V = tier_load_panel()
    with db_conn() as conn:
        info = {c2: ((n or ""), (i or "")) for c2, n, i in
                conn.execute("select code,name,industry from stocks")}
    idx_of = {c: k for k, c in enumerate(codes)}
    groups, etf_names = {}, {}
    for k, code in enumerate(codes):
        nm, ind = info.get(code, ("", ""))
        if _is_etf(code):
            if nm:
                etf_names[code] = nm
            continue
        if ind:
            groups.setdefault(ind, []).append(k)
    ind_etfs = _match_industry_etf(list(groups), etf_names)
    series, used_etf = {}, {}
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for ind, ks in groups.items():
            if len(ks) < 3:
                continue
            etf = ind_etfs.get(ind)
            if etf is not None:
                ei = idx_of.get(etf)
                if ei is not None:
                    arr = C[ei, :].astype(float)
                    fin = np.isfinite(arr) & (arr > 0)
                    if int(fin.sum()) >= 60:
                        base = arr[fin][0]
                        series[ind] = arr / base
                        used_etf[ind] = etf
                        continue
            # 回退：同行业个股等权收益累乘合成
            sub = C[ks, :].astype(float)
            with np.errstate(invalid="ignore", divide="ignore"):
                r = np.where(sub[:, :-1] > 0, sub[:, 1:] / sub[:, :-1] - 1.0,
                             np.nan)
            r = np.hstack([np.full((len(ks), 1), np.nan), r])
            m = np.nanmean(r, axis=0)
            m = np.where(np.isfinite(m), m, 0.0)
            series[ind] = np.cumprod(1.0 + m)
            used_etf[ind] = ""
    _SECTOR_L2_CACHE["data"] = (cal, series, used_etf)
    _SECTOR_L2_CACHE["ts"] = now
    return _SECTOR_L2_CACHE["data"]


def _sig_l2_industry(rows, industry="", step=3, **_kw):
    """L2 信号（同行业 + 行业ETF，v6.1.3）：

    用行业的「行业指数」（优先同名行业ETF）做择时——
    · BUY：行业指数在 MA20 上方 **且** 行业 5 日动量 > 0（行业走强）；
    · SELL：行业指数在 MA20 下方 **且** 行业 5 日动量 < 0（行业转弱）。
    即「同行业/行业ETF 先转强，再买该行业个股」，与 `sector_rot`（行业之间比强弱）
    互补：L2 看**行业自身的时序趋势**，sector_rot 看**横截面排名**。"""
    if not industry:
        return []
    try:
        cal, series, _used = sector_l2_series()
    except Exception:
        return []
    arr = series.get(industry)
    if arr is None or len(arr) < 25:
        return []
    ma20 = np.full(len(arr), np.nan)
    ma20[19:] = np.convolve(arr, np.ones(20) / 20.0, "valid")
    r5 = np.full(len(arr), np.nan)
    r5[5:] = arr[5:] / arr[:-5] - 1.0
    didx = {d: i for i, d in enumerate(cal)}
    out = []
    for i in range(5, len(rows), step):
        j = didx.get(rows[i].get("date"))
        if j is None or j < 20:
            continue
        up = np.isfinite(ma20[j]) and arr[j] > ma20[j]
        dn = np.isfinite(ma20[j]) and arr[j] < ma20[j]
        if up and np.isfinite(r5[j]) and r5[j] > 0:
            out.append((i, rows[i]["date"], "BUY", "行业(ETF)走强"))
        elif dn and np.isfinite(r5[j]) and r5[j] < 0:
            out.append((i, rows[i]["date"], "SELL", "行业(ETF)转弱"))
    return out


def sector_mom_series(max_age=1800):
    """行业5日动量序列（面板 numpy 一次构建并缓存）：
    返回 (cal, {industry: r5数组}, 全行业中位r5数组)，仅用截至当日数据。"""
    now = time.time()
    if (_SECTOR_MOM_CACHE["data"] is not None
            and now - _SECTOR_MOM_CACHE["ts"] < max_age):
        return _SECTOR_MOM_CACHE["data"]
    codes, cal, C, V = tier_load_panel()
    with db_conn() as conn:
        ind_of = {c2: (i or "") for c2, i in
                  conn.execute("select code,industry from stocks")}
    r5 = np.full_like(C, np.nan)
    r5[:, 5:] = C[:, 5:] / C[:, :-5] - 1.0
    groups = {}
    for k, code in enumerate(codes):
        if _is_etf(code):
            continue          # 板块轮动用个股行业，ETF 不参与（避免伪"ETF行业"）
        ind = ind_of.get(code)
        if ind:
            groups.setdefault(ind, []).append(k)
    import warnings
    series = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for ind, ks in groups.items():
            if len(ks) < 3:
                continue
            series[ind] = np.nanmean(r5[ks, :], axis=0)
        med5 = (np.nanmedian(np.vstack(list(series.values())), axis=0)
                if series else np.full(len(cal), np.nan))
    _SECTOR_MOM_CACHE["data"] = (cal, series, med5)
    _SECTOR_MOM_CACHE["ts"] = now
    return _SECTOR_MOM_CACHE["data"]


def _is_finite(x):
    try:
        return math.isfinite(x)
    except (TypeError, ValueError):
        return False


def _chip_feats_py(rows, start_idx, params=None):
    """内置纯 Python 筹码因子引擎（口径同 factor_lab.chips，因果）。

    价格网格摊分成交量、按换手率衰减演化；局部峰中取筹码最重者作为
    支撑/压力（无峰退化最密集 bin）。返回 (feats, valid)，feats[i] =
    (支撑距离, 压力距离, 获利占比)，valid[i] 标记输出窗口内的有效日；
    不足 30 根或热身段不足返回全空。不依赖 factor_lab / numpy。"""
    p = {"nbin": 200, "decay_a": 0.02, "cap": 0.20, "floor": 0.002,
         "prom": 0.0}
    if params:
        p.update(params)
    nbin = int(p["nbin"])
    nan = float("nan")
    n_all = len(rows)
    feats = [(nan, nan, nan)] * n_all
    valid = [False] * n_all
    keep = [i for i, b in enumerate(rows)
            if b.get("vol") and b.get("low") and b["low"] > 0
            and b.get("high") and b["high"] >= b["low"]]
    if len(keep) < 30 or start_idx < 30:
        return feats, valid
    bars = [rows[i] for i in keep]
    base = [b for i, b in zip(keep, bars) if i < start_idx]
    if not base:
        return feats, valid
    lo = min(b["low"] for b in base)
    hi = max(b["high"] for b in base)
    if hi <= lo:
        return feats, valid
    step = (hi - lo) / nbin
    mids = [lo + step * (j + 0.5) for j in range(nbin + 1)]
    chips = [0.0] * (nbin + 1)
    med_vol = sorted(b["vol"] for b in base)[len(base) // 2] or 1.0
    a, cap, floor, prom = (p["decay_a"], p["cap"], p["floor"], p["prom"])
    for k, b in enumerate(bars):
        i = keep[k]
        t = min(cap, max(floor, a * (b["vol"] / med_vol)))
        d = 1.0 - t
        chips = [v * d for v in chips]
        b_lo = min(nbin, max(0, int((b["low"] - lo) / step)))
        b_hi = min(nbin, max(0, int((b["high"] - lo) / step)))
        if b_hi <= b_lo:
            chips[b_hi] += b["vol"]
        else:
            per = b["vol"] / (b_hi - b_lo + 1)
            for j in range(b_lo, b_hi + 1):
                chips[j] += per
        if i < start_idx:
            continue
        tot = 0.0
        for v in chips:
            tot += v
        if tot <= 0:
            continue
        c = b["close"]
        valid[i] = True
        thr = prom * max(chips)
        pk_mid, pk_mass = [], []
        prev, cur = chips[0], chips[1]
        for j in range(1, nbin):
            nxt = chips[j + 1]
            if cur > prev and cur >= nxt and cur > thr:
                pk_mid.append(mids[j])
                pk_mass.append(cur)
            prev, cur = cur, nxt
        sup = res = nan
        best = -1.0
        for m, v in zip(pk_mid, pk_mass):
            if m < c and v > best:
                best, sup = v, m
        if best < 0:
            for m, v in zip(mids, chips):
                if m < c and v > best:
                    best, sup = v, m
        best = -1.0
        for m, v in zip(pk_mid, pk_mass):
            if m >= c and v > best:
                best, res = v, m
        if best < 0:
            for m, v in zip(mids, chips):
                if m >= c and v > best:
                    best, res = v, m
        profit = 0.0
        for m, v in zip(mids, chips):
            if m <= c:
                profit += v
        profit /= tot
        feats[i] = (-(c - sup) / c if _is_finite(sup) else nan,
                    -(res - c) / c if _is_finite(res) else nan,
                    profit)
    return feats, valid


def _sig_chip_peak(rows, **_kw):
    """筹码峰信号：贴近峰支撑企稳（获利盘<35%）→BUY；
    获利盘过重(>90%)或跌破支撑(>0.5%)→SELL。内置筹码引擎（因果）。"""
    try:
        feats, valid = _chip_feats_py(rows, min(250, len(rows) // 4))
    except Exception:
        return []
    out = []
    for i, r in enumerate(rows):
        if not valid[i]:
            continue
        sup_dist, _res_dist, profit = feats[i]
        if not _is_finite(profit):
            continue
        if _is_finite(sup_dist) and profit < 0.35 and sup_dist > -0.04:
            out.append((i, r["date"], "BUY", "筹码峰支撑企稳"))
        elif profit > 0.90 or (_is_finite(sup_dist) and sup_dist > 0.005):
            out.append((i, r["date"], "SELL", "获利盘过重/跌破筹码峰"))
    return out


def _sig_sector_rot(rows, industry="", step=3, **_kw):
    """板块轮动信号：行业5日动量>全行业中位 且个股5日为正 → BUY；
    行业动量落后且个股5日转负 → SELL。"""
    if not industry:
        return []
    try:
        cal, series, med5 = sector_mom_series()
    except Exception:
        return []
    arr = series.get(industry)
    if arr is None:
        return []
    didx = {d: i for i, d in enumerate(cal)}
    closes = [r.get("close") or 0.0 for r in rows]
    out = []
    for i in range(5, len(rows), step):
        j = didx.get(rows[i].get("date"))
        if j is None or not np.isfinite(arr[j]) or not np.isfinite(med5[j]):
            continue
        if not closes[i - 5]:
            continue
        sr5 = closes[i] / closes[i - 5] - 1.0
        if arr[j] > med5[j] and sr5 > 0.01:
            out.append((i, rows[i]["date"], "BUY", "板块动量领先"))
        elif arr[j] < med5[j] and sr5 < 0:
            out.append((i, rows[i]["date"], "SELL", "板块动量落后"))
    return out


def _rank01(vals):
    """带 None 值的横截面 rank（0~1），None 记 0。"""
    m = sum(1 for v in vals if v is not None)
    rk = [0.0] * len(vals)
    if not m:
        return rk
    order = sorted(range(len(vals)),
                   key=lambda i: (vals[i] is not None, vals[i]))
    pos = 0
    for i in order:
        if vals[i] is not None:
            pos += 1
            rk[i] = pos / m
    return rk


def _ablation_pool(cands, min_trades):
    """交易活跃度下限池：优先 >=min_trades，不足降到 3 笔，再不足全量。"""
    pool = [c for c in cands if c["train"].get("trades", 0) >= min_trades]
    if not pool:
        pool = [c for c in cands if c["train"].get("trades", 0) >= 3]
    if not pool:
        pool = list(cands)
    return pool


def _ablation_weights(objective):
    """目标权重：稳健/保守偏 Calmar+PF；均衡/激进偏年化+Calmar。"""
    return ({"calmar": 0.45, "pf": 0.25, "winrate": 0.20, "ann": 0.10}
            if objective == "稳健" else
            {"calmar": 0.30, "pf": 0.20, "winrate": 0.15, "ann": 0.35})


def _ablation_ranks(pool, objective, key="train"):
    """4 指标横截面 rank(0~1) 加权得分（与 pick_ablation_multi 同口径）。
    key="train" 用训练段指标，key="recent" 用近端子窗指标。"""

    def calmar(m):
        return m.get("ann", 0) / max(abs(m.get("mdd", 0.05)), 0.05)

    def pf(m):
        return min(m.get("pf") or 0.0, 5.0)

    def wr(m):
        return m.get("winrate") or 0.0

    def ann(m):
        return m.get("ann") or 0.0

    r = {
        "calmar": _rank01([calmar(c[key]) for c in pool]),
        "pf": _rank01([pf(c[key]) for c in pool]),
        "winrate": _rank01([wr(c[key]) for c in pool]),
        "ann": _rank01([ann(c[key]) for c in pool]),
    }
    w = _ablation_weights(objective)
    return [sum(w[mk] * r[mk][k] for mk in w) for k in range(len(pool))]


def pick_ablation_multi(cands, objective="稳健", min_trades=8):
    """消融多指标结合选优（v6.1，仅用训练集指标，防前视）：
    稳健 = 偏 Calmar+PF；均衡/激进 = 偏年化+Calmar；
    四个指标各自横截面 rank 后加权，避免量纲/单指标过拟合。"""
    pool = _ablation_pool(cands, min_trades)
    if not pool:
        return None
    sc = _ablation_ranks(pool, objective)
    return dict(pool[max(range(len(pool)), key=lambda k: sc[k])])


def pick_ablation_consistent(cands, objective="稳健", min_trades=8,
                             recent_of=None, fallback=True):
    """全窗选优 + 近端子窗一致性（v6.1.5 热修②，防风格切换失配）：

    在全训练窗和最近 `RECENT_ABL_BARS` 根子窗里，各自按同一权重 rank 取
    前 25%（下限 3 个）；两窗同时在前列者中取全窗得分最高。
    交集为空（近端 regime 与全窗不一致）→ 回退该档「多维评分」候选；
    近窗可评估候选 <3 个时视为无法判断，按纯全窗选优。
    近窗（n=1000 时=验证段）只用于门槛否决，权重打分仍只用训练段指标。

    recent_of(c) 返回候选的近窗指标 dict（trades>=2），无则 None。
    返回 (picked, note)。"""
    pool = _ablation_pool(cands, min_trades)
    if not pool:
        return None, "无候选"
    full = _ablation_ranks(pool, objective)
    if recent_of is None:
        i = max(range(len(pool)), key=lambda k: full[k])
        return dict(pool[i]), "仅全窗"
    rpool = [i for i, c in enumerate(pool) if recent_of(c)]
    if len(rpool) < 3:
        i = max(range(len(pool)), key=lambda k: full[k])
        return dict(pool[i]), "近窗样本不足，按全窗"
    rs = _ablation_ranks([pool[i] for i in rpool], objective, key="recent")
    kf = max(3, (len(pool) + 3) // 4)
    kr = max(2, (len(rpool) + 3) // 4)
    ftop = set(sorted(range(len(pool)), key=lambda k: -full[k])[:kf])
    rtop = {rpool[j]
            for j in sorted(range(len(rpool)), key=lambda k: -rs[k])[:kr]}
    both = ftop & rtop
    if both:
        i = max(both, key=lambda k: full[k])
        return dict(pool[i]), "全窗+近窗一致"
    if not fallback:
        i = max(range(len(pool)), key=lambda k: full[k])
        return dict(pool[i]), "近窗不一致（未回退）"
    comp = [c for c in pool if c.get("algo") == "composite"]
    if comp:
        cs = _ablation_ranks(comp, objective)
        i = max(range(len(comp)), key=lambda k: cs[k])
        return dict(comp[i]), "近窗不一致→回退多维评分"
    i = max(range(len(pool)), key=lambda k: full[k])
    return dict(pool[i]), "近窗不一致→无多维候选，按全窗"


def _pick_one_from_pool(pool, key):
    """从候选池按某档目标选优（GUI run_ablation 与研究导出共用，口径一致）。

    key: 保守/稳健/激进；保守档限定「保守/稳健参数」候选，其余目标：
    保守/稳健=偏 Calmar+PF，激进=偏年化+Calmar（见 _ablation_weights）。
    返回 (picked, note)。"""
    p = pool
    if key == "保守":
        p2 = [c for c in p if c.get("mode") in ("保守", "稳健")]
        if p2:
            p = p2
    obj = "激进" if key in ("均衡", "激进") else "稳健"
    picked, note = pick_ablation_consistent(
        p, obj, min_trades=0, recent_of=lambda c: c.get("recent"))
    if not picked:
        return dict(p[0]), "回退池内首个"
    return picked, note


def _ablation_pf(trades):
    """由逐笔收益算盈亏比。"""
    gp = sum(x for x in trades if x > 0)
    gl = -sum(x for x in trades if x <= 0)
    return (gp / gl) if gl > 1e-9 else None


# ---- 区间事件回测（信号日收盘成交 + ATR止损/移动止盈，防前视）----

def _bt_events(rows, signals, rp, i0=0, i1=None, atrs=None, trade_out=None,
               arrays=None):
    """在 rows[i0:i1] 上模拟交易。返回指标dict；交易数不足返回 None。
    早盘信号：信号在 T 日收盘生成，T+1 日收盘执行；止损单用 T-1 日 ATR 设定。
    atrs 可外部预计算加速。
    arrays（v6.1.3，numpy 加速）：(o,h,l,c) 平行列表，已由调用方从 rows 抽出，
      供批量回测复用，避免每次回测重复做 4×N 次字典取值。
    trade_out（可选 list）：追加逐笔收益率。"""
    i1 = len(rows) if i1 is None else min(i1, len(rows))
    if i1 - i0 < 30:
        return None
    # 早盘信号：执行日 = 信号日 + 1（信号日+1 须落在评估区间内）
    sig_map = {s[0] + 1: s[2] for s in signals if i0 <= s[0] + 1 < i1}
    # ATR(14) 预计算（若未传入）
    if atrs is None:
        atrs = _precompute_atr(rows, i0, i1)
    # OHLC 平行数组（numpy 加速路径：调用方传入；否则就地抽取一次）
    if arrays is not None:
        o_a, h_a, l_a, c_a = arrays
    else:
        o_a = [(r.get("open") or 0.0) for r in rows]
        h_a = [(r.get("high") or 0.0) for r in rows]
        l_a = [(r.get("low") or 0.0) for r in rows]
        c_a = [(r.get("close") or 0.0) for r in rows]
    eq = 1.0
    exec_open = _exec_mode() == "open"
    atr_mult = rp["atr_mult"]
    trail_ratio = rp["trail_ratio"]
    trail_trig = rp["trail_trigger"]
    entry = None
    highest = None
    trades = []
    curve = []
    sig_get = sig_map.get
    for i in range(i0, i1):
        c = c_a[i]
        h = h_a[i]
        l = l_a[i]
        px_fill = (o_a[i] or c) if exec_open else c
        typ = sig_get(i)
        if entry is not None:
            prev_high = highest
            highest = max(highest, h) if highest else h
            atr_prev = atrs[i - 1] if i > 0 else 0.0
            atr_stop = (entry - atr_mult * atr_prev) if atr_prev > 0 \
                else entry * 0.95
            trail_stop = (prev_high * trail_ratio
                          if prev_high > entry * trail_trig
                          else atr_stop)
            if l <= trail_stop:
                o_i = o_a[i]
                exit_px = o_i if (o_i and o_i <= trail_stop) else trail_stop
                trades.append(exit_px / entry - 1)
                eq *= exit_px / entry
                entry = None
                highest = None
        if typ == "BUY" and entry is None and c:
            entry = px_fill
            highest = px_fill         # 成交时点之前的盘中高点不计入
        elif typ == "SELL" and entry:
            trades.append(px_fill / entry - 1)
            eq *= px_fill / entry
            entry = None
            highest = None
        curve.append(eq * (c / entry) if entry else eq)
    if len(trades) < 2:
        return None
    if trade_out is not None:
        trade_out.extend(trades)
    import datetime
    try:
        d0 = datetime.date.fromisoformat(rows[i0]["date"])
        d1 = datetime.date.fromisoformat(rows[i1 - 1]["date"])
        years = max((d1 - d0).days / 365.25, 0.25)
    except Exception:
        years = max((i1 - i0) / 250.0, 0.25)
    if np is not None:
        # numpy 加速：累计峰值/回撤/胜率一次算完
        cur = np.asarray(curve, float)
        tr = np.asarray(trades, float)
        total = float(cur[-1]) if cur.size else 1.0
        wins = int((tr > 0).sum())
        peak = np.maximum.accumulate(cur)
        mdd = float(np.min(cur / peak - 1.0)) if cur.size else 0.0
    else:
        total = curve[-1] if curve else 1.0
        wins = len([t for t in trades if t > 0])
        peak, mdd = 0.0, 0.0
        for v in curve:
            peak = max(peak, v)
            if peak > 0:
                mdd = min(mdd, v / peak - 1)
    ann = total ** (1 / years) - 1 if total > 0 else -1.0
    return {"trades": len(trades), "wins": wins,
            "winrate": wins / len(trades),
            "total": total - 1, "ann": ann, "mdd": mdd,
            "curve": curve, "i0": i0}


RECENT_ABL_BARS = 250       # 选型一致性子窗长度（取训练段末尾，不碰验证段）
ABL_BARS = 1000             # 消融/工具面板回测窗口（与 run_ablation 一致）


def _ablation_recent(rows, sigs, rp, n, atrs, arrays=None):
    """最近 ~250 根（截至最新）的"近端 regime"回测指标；交易<2 返回 None。

    用途：`pick_ablation_consistent` 的近端一致性否决——防"长期横盘/老 regime
    选出在当下风格里沉默的策略"（如 688012 于 2025-09 突破后的失配）。
    注意：n=1000 时该子窗即验证段，因此验证段参与"top 门槛否决"但不参与
    指标 rank 打分；这是时点可观测数据，属选型的一部分（见 ARCHITECTURE 3.8）。"""
    r0 = max(0, n - RECENT_ABL_BARS)
    if r0 <= 0 or n - r0 < 100:
        return None
    tr_out = []
    rc = _bt_events(rows, sigs, rp, r0, n, atrs=atrs, trade_out=tr_out,
                    arrays=arrays)
    if not rc:
        return None
    rc = {k: v for k, v in rc.items() if k != "curve"}
    if tr_out:
        rc["pf"] = _ablation_pf(tr_out)
    return rc


def _regime_map(idx_rows, n):
    """牛市/熊市日历：指数收盘 ≥ MA120 视为牛市。返回 {date: bool}。"""
    closes = [r["close"] for r in (idx_rows or [])]
    if len(closes) < 130:
        return {}
    ma = sma_period(closes, 120)
    out = {}
    for r, m in zip(idx_rows, ma):
        if m:
            out[r["date"]] = r["close"] >= m
    return out


def _bull_bear_score(rows, curve, i0, regime, dates=None):
    """分段年化收益：牛市段/熊市段（无数据段返回 None）。

    v6.1.3：改为 numpy 向量化（逐日收益、区间掩码、对数求和一次性算完）；
    dates 可选传入日期列表（与 curve 前 len 项对齐），避免重复做 rows[i] 取值。"""
    if not regime or not curve:
        return None, None
    m = min(i0 + len(curve), len(rows))
    n_idx = m - i0
    if n_idx < 21:
        return None, None
    if dates is None:
        dates = [rows[i]["date"] for i in range(i0, m)]
    else:
        dates = dates[i0:m]        # 只取本区间（train/val 曲线可能短于全量）
    if len(dates) != n_idx:
        return None, None
    cur = np.asarray(curve[:n_idx], float) if np is not None else None
    if cur is None:                       # 无 numpy 的纯 Python 回退
        bull_r, bear_r, prev = [], [], None
        for k, i in enumerate(range(i0, m)):
            reg = regime.get(rows[i]["date"])
            if prev is not None and prev[1] > 0:
                r = curve[k] / prev[1] - 1
                if reg is True:
                    bull_r.append(r)
                elif reg is False:
                    bear_r.append(r)
            prev = (rows[i]["date"], curve[k])
        def _ann0(rets):
            if len(rets) < 20:
                return None
            s = 0.0
            for r in rets:
                s += math.log1p(max(-0.95, min(r, 0.95)))
            return math.expm1(s * 252.0 / len(rets))
        return _ann0(bull_r), _ann0(bear_r)
    prev = cur[:-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = np.where(prev > 0, cur[1:] / np.where(prev > 0, prev, 1.0) - 1.0,
                       np.nan)
    regs = [regime.get(d) for d in dates[1:]]
    is_bull = np.fromiter((r is True for r in regs), bool, len(regs))
    is_bear = np.fromiter((r is False for r in regs), bool, len(regs))
    valid = np.isfinite(ret)

    def _ann(mask):
        x = ret[valid & mask]
        if x.size < 20:
            return None
        s = float(np.log1p(np.clip(x, -0.95, 0.95)).sum())
        return math.expm1(s * 252.0 / x.size)

    return _ann(is_bull), _ann(is_bear)


def _precompute_atr(rows, i0=0, i1=None):
    """预计算 ATR(14)，供 run_ablation 批量回测复用（numpy 向量化）。"""
    i1 = len(rows) if i1 is None else min(i1, len(rows))
    n = len(rows)
    start = i0 + 14
    if i1 <= start:
        return [0.0] * n
    if np is None:
        atrs = [0.0] * n
        for i in range(start, i1):
            s = 0.0
            for j in range(i - 13, i + 1):
                h, l, pc = rows[j]["high"], rows[j]["low"], rows[j - 1]["close"]
                s += max(h - l, abs(h - pc), abs(l - pc))
            atrs[i] = s / 14
        return atrs
    h = np.array([(r.get("high") or 0.0) for r in rows], float)
    l = np.array([(r.get("low") or 0.0) for r in rows], float)
    c = np.array([(r.get("close") or 0.0) for r in rows], float)
    pc = np.empty(n, float)
    pc[1:] = c[:-1]
    pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    cs = np.insert(np.cumsum(tr), 0, 0.0)
    atrs = np.zeros(n, float)
    atrs[start:i1] = (cs[start + 1:i1 + 1] - cs[start - 13:i1 - 13]) / 14.0
    return atrs.tolist()


def _annualized_vol(rows, lookback=250):
    """近 lookback 日对数收益年化波动率（无数据返回 None）。"""
    closes = [r["close"] for r in rows[-lookback:] if r.get("close")]
    if len(closes) < 30:
        return None
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 20:
        return None
    mu = sum(rets) / len(rets)
    var = sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252.0)


def run_ablation(full, rows, idx_rows=None, progress=None):
    """多算法消融回测（近1000交易日）。训练集选策略/验证集验证，防过拟合。
    v2026-09-12: 预计算ATR + 线程池并行候选评估，速度提升。

    返回 {"mode_candidates": {保守:strat, 稳健:strat, 激进:strat},
          "ts": ..., "bars": n, "train_n":, "val_n":} 或 None。
    strat = {"algo","mode","params","train","val","bull","bear","label"}"""
    rows = [r for r in rows if r.get("close") and r["close"] > 0]
    if len(rows) > ABL_BARS:
        rows = rows[-ABL_BARS:]
    if len(rows) < 200:
        return None
    n = len(rows)
    val_n = max(200, n // 4)
    split = n - val_n
    regime = _regime_map(idx_rows, n)
    # 预计算ATR，所有候选复用
    atrs = _precompute_atr(rows, 0, n)
    if progress:
        progress("策略消融回测中(近1000交易日)...")

    # 行业（板块轮动信号用；本地缓存表，AI 不参与）
    industry = ""
    try:
        with db_conn() as conn:
            row = conn.execute("select industry from stocks where code=?",
                               (full,)).fetchone()
            industry = (row[0] or "") if row else ""
    except Exception:
        pass

    # 生成所有基础信号（只生成一次）
    sig_cache = {}
    gens = {
        "macd": lambda: _sig_macd(rows),
        "kdj": lambda: _sig_kdj(rows),
        "rsi": lambda: _sig_rsi(rows),
        "boll": lambda: _sig_boll(rows),
        "ma_trend": lambda: _sig_ma_trend(rows),
        "l1_pattern": lambda: _sig_l1_pattern(rows),
        "l2_ind": lambda: _sig_l2_industry(rows, industry=industry),
        "chip_peak": lambda: _sig_chip_peak(rows),
        "sector_rot": lambda: _sig_sector_rot(rows, industry=industry),
    }
    for algo, gen in gens.items():
        try:
            sig_cache[algo] = gen()
        except Exception:
            log.warning("消融信号生成失败 %s", algo, exc_info=True)
            sig_cache[algo] = []

    # 任务列表：(algo, mode, rp, is_composite)
    tasks = []
    for algo in gens:
        for mode, rp in CFG.RISK_PARAMS.items():
            tasks.append((algo, mode, rp, False))
    for mode, rp in CFG.RISK_PARAMS.items():
        tasks.append(("composite", mode, rp, True))

    comp_pre = _composite_precompute(rows)

    def _eval_task(task):
        algo, mode, rp, is_comp = task
        if is_comp:
            try:
                sigs = _composite_signals(rows, rp, idx_chg_by_date=None,
                                          pre=comp_pre)
            except Exception:
                log.warning("composite信号生成失败 %s", mode, exc_info=True)
                return None
        else:
            sigs = sig_cache.get(algo)
            if not sigs:
                return None
        tr_tr, va_tr = [], []
        tr = _bt_events(rows, sigs, rp, 0, split, atrs=atrs, trade_out=tr_tr)
        va = _bt_events(rows, sigs, rp, split, n, atrs=atrs, trade_out=va_tr)
        if not tr:
            return None
        bull, bear = _bull_bear_score(rows, tr["curve"], tr["i0"], regime)
        label = (f"多维评分·{mode}" if is_comp
                 else f"{ALGO_LABEL.get(algo, algo)}·{mode}")
        train = {k: v for k, v in tr.items() if k != "curve"}
        val = ({k: v for k, v in va.items() if k != "curve"} if va else None)
        if tr_tr:
            train["pf"] = _ablation_pf(tr_tr)
        if va_tr and val is not None:
            val["pf"] = _ablation_pf(va_tr)
        # 近端一致性门槛只用「训练段末尾」(split 前) 的数据：
        # 此前用 rows[0:n] 的最近 250 根，而 n=1000 时那正是验证段 →
        # 验证集参与了选型门槛（top25% 否决），验证段指标偏乐观（过拟合）。
        rc = _ablation_recent(rows, sigs, rp, split, atrs)
        return {"algo": algo, "mode": mode, "params": dict(rp),
                "label": label, "train": train, "val": val,
                "recent": rc, "bull": bull, "bear": bear}

    cands = []
    # 使用线程池并行评估候选（I/O轻、计算密集，GIL会部分释放）
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as exe:
        for r in exe.map(_eval_task, tasks):
            if r is not None:
                cands.append(r)

    if not cands:
        return None
    # ---- 完整消融：每档在【全部候选】(算法 × 风险参数) 上按其目标选优 ----
    def _calmar(m):
        return m.get("ann", 0) / max(abs(m.get("mdd", 0.05)), 0.05)

    # 交易活跃度下限：避免选到“几乎不交易、回撤自然为 0”的假策略
    _MIN_TR = 8
    _pick_notes = {}

    def _pick(key):
        pool = _ablation_pool(cands, _MIN_TR)
        if not pool:
            _pick_notes[key] = "样本不足"
            return {"algo": "composite", "mode": key,
                    "params": dict(CFG.RISK_PARAMS.get(
                        key, CFG.RISK_PARAMS["稳健"])),
                    "label": f"多维评分·{key}（样本不足，固定回退）",
                    "train": {}, "val": {}}
        # v6.1：多指标结合（Calmar/PF/胜率/年化 rank 加权，仅训练集）；
        # v6.1.5 热修②：+近端子窗一致性（训练段末尾），不一致回退该档「多维评分」
        picked, note = _pick_one_from_pool(pool, key)
        _pick_notes[key] = note
        return picked

    mode_candidates = {"保守": _pick("保守"), "稳健": _pick("稳健"),
                       "激进": _pick("激进")}
    # 三档可能选中同一候选（同一算法×参数在两个加权目标下都排第一，
    # 属训练集选型结果而非故障）；记录选型与一致性结论，便于日志核对。
    log.info("消融选型 %s: 保守=%s[%s] | 稳健=%s[%s] | 激进=%s[%s]", full,
             mode_candidates["保守"]["label"], _pick_notes.get("保守"),
             mode_candidates["稳健"]["label"], _pick_notes.get("稳健"),
             mode_candidates["激进"]["label"], _pick_notes.get("激进"))

    # ---- 风险档推荐：只用训练集 Calmar 选（验证集仅报告，不参与选择）----
    def _tc(t):
        tr = (mode_candidates.get(t) or {}).get("train") or {}
        if not tr or tr.get("trades", 0) < 3:
            return None
        return _calmar(tr)
    scored = [(t, _tc(t)) for t in ("保守", "稳健", "激进")]
    scored = [(t, s) for t, s in scored if s is not None]
    recommend = max(scored, key=lambda x: x[1])[0] if scored else "稳健"
    vol = _annualized_vol(rows)
    high_vol = bool(vol is not None and vol > 0.45)
    # 高波动股保守档紧止损易被反复触发：推荐改在 稳健/激进 中取较优，
    # 与弹窗提示保持一致（否则会出现"推荐保守但提示别选保守"的自相矛盾）
    if high_vol and recommend == "保守":
        alt = [(t, s) for t, s in scored if t in ("稳健", "激进")]
        if alt:
            recommend = max(alt, key=lambda x: x[1])[0]

    out = {"mode_candidates": mode_candidates,
           "recommend": recommend,
           "pick_notes": dict(_pick_notes),
           "vol_ann": vol,
           "high_vol": high_vol,
           "ts": time.time(), "bars": n, "train_n": split,
           "val_n": n - split}
    if progress:
        progress(f"策略消融完成：{len(cands)}个候选 (训练{split}/验证{n - split})")
    return out


# ================= 分析 =================

def _fusion_prediction(o_today, pre_open, live, levels):
    """三级融合。盘中用open->close分布；盘前/收盘后用close->next-close分布。"""
    LW=_dynamic_lv_weights(levels); tot_w=sum(LW.get(k,0.0) for k,_ in levels) or 1.0
    target=("oc","oh","ol") if live is not None else ("cl","hi","lo")
    def _wfield(field):
        pairs=[]
        for k,smp in levels:
            lw=LW.get(k,0.0)/tot_w; valid=[s for s in smp if s.get(field) is not None]
            tw=sum(max(1e-9,s.get("weight",1.0)) for s in valid) or 1.0
            pairs.extend((s[field],lw*s.get("weight",1.0)/tw) for s in valid)
        return pairs
    PS=(10,25,50,75,90); cf,hf,lf=(f"n1_{x}" for x in target)
    cp,hp,lp=_wfield(cf),_wfield(hf),_wfield(lf)
    if not cp:
        # 无有效样本：输出平线预测而非空区间，保证报告/图表不崩
        ps=(10,25,50,75,90)
        flat={"cl":{p:o_today for p in ps},"hi":{p:o_today for p in ps},
              "lo":{p:o_today for p in ps},"up_prob":0.5,"confidence":None}
        return (flat,
                {"date":"T+1预测","open":o_today,"close":o_today,
                 "high":o_today,"low":o_today,"vol":None,
                 "confidence":None},
                False, None)
    # 区间校准（INTERVAL_K，n=4500标定）：分位偏离P50放大，消除过窄
    K=CFG.INTERVAL_K
    c50=wpct(cp,50); h50=wpct(hp or cp,50); l50=wpct(lp or cp,50)
    t_pred={"cl":{p:o_today*(1+c50+K*(wpct(cp,p)-c50)) for p in PS},
            "hi":{p:o_today*(1+h50+K*(wpct(hp or cp,p)-h50)) for p in PS},
            "lo":{p:o_today*(1+l50+K*(wpct(lp or cp,p)-l50)) for p in PS},
            "up_prob":sum(w for v,w in cp if v>0)}
    confidence=_calculate_confidence(levels); t_pred["confidence"]=confidence
    # ---- T+5 累计预测：样本 close→close 5日累计（n1..n5全有才计入）----
    def _cum5(s):
        f=[s.get(f"n{d}_cl") for d in range(1,6)]
        if any(x is None for x in f):
            return None
        c=1.0
        for x in f:
            c*=(1+x)
        return c-1.0
    t5pairs=[]
    for k,smp in levels:
        lw=LW.get(k,0.0)/tot_w
        v=[(c,s.get("weight",1.0)) for s in smp if (c:=_cum5(s)) is not None]
        if not v:
            continue
        tw=sum(w for _,w in v) or 1.0
        t5pairs.extend((c, lw*w/tw) for c,w in v)
    if t5pairs:
        K5=CFG.INTERVAL_K5
        c50=wpct(t5pairs,50)
        t5={"cl":{p:o_today*(1+c50+K5*(wpct(t5pairs,p)-c50)) for p in PS},
            "up_prob":sum(w for v,w in t5pairs if v>0),"n":len(t5pairs)}
        t_pred["t5"]=t5
    else:
        t_pred["t5"]=None
    clamped=False
    if live is not None:
        clamped=True
        for pp in PS:
            t_pred["hi"][pp]=round(max(t_pred["hi"][pp],live["high"]),2); t_pred["lo"][pp]=round(min(t_pred["lo"][pp],live["low"]),2)
    pred={"date":"T日预测" if live is not None else ("今日(T)" if pre_open else "T+1预测"),"open":o_today,"close":t_pred["cl"][50],"high":t_pred["hi"][50],"low":t_pred["lo"][50],"vol":None,"confidence":confidence}
    tpred_bar={"date":"T日预测","open":o_today,"close":t_pred["cl"][50],"high":t_pred["hi"][50],"low":t_pred["lo"][50],"vol":None,"confidence":confidence} if live is not None else None
    return t_pred,pred,clamped,tpred_bar


def _calculate_confidence(levels):
    """计算预测置信度，基于多个维度评估样本质量。"""
    if not CFG.CONFIDENCE_ENABLED:
        return None
    
    confidence_score = 1.0
    total_samples = sum(len(smp) for _, smp in levels)
    
    # 1. 样本数量因子
    if total_samples < CFG.MIN_SAMPLES_REQUIRED:
        confidence_score *= 0.5
    elif total_samples < CFG.MIN_SAMPLES_REQUIRED * 2:
        confidence_score *= 0.8
    
    # 2. 相似度一致性因子（所有样本的相似度分数方差）
    if CFG.SIMILARITY_WEIGHTING and total_samples > 0:
        all_scores = []
        for _, smp in levels:
            all_scores.extend([s.get("similarity_score", float('inf')) for s in smp])
        
        if len(all_scores) > 1:
            mean_score = sum(all_scores) / len(all_scores)
            variance = sum((s - mean_score) ** 2 for s in all_scores) / len(all_scores)
            std_dev = variance ** 0.5
            
            # 相似度方差越小，置信度越高
            consistency_factor = max(0.6, 1.0 - min(std_dev / 2.0, 0.4))
            confidence_score *= consistency_factor
    
    # 3. 权重分布因子（有效权重占比）
    if CFG.SIMILARITY_WEIGHTING and total_samples > 0:
        all_weights = []
        for _, smp in levels:
            all_weights.extend([s.get("weight", 1.0) for s in smp])
        
        # 计算高权重样本占比（权重>1.0的样本）
        high_weight_count = sum(1 for w in all_weights if w > 1.0)
        weight_quality = high_weight_count / len(all_weights) if all_weights else 0.5
        confidence_score *= (0.7 + 0.3 * weight_quality)  # 范围0.7-1.0
    
    # 4. 层级完整性因子（三个层级是否都有样本）
    active_levels = sum(1 for _, smp in levels if smp)
    if active_levels == 3:
        confidence_score *= 1.0  # 完整的三级样本
    elif active_levels == 2:
        confidence_score *= 0.9  # 缺少一个层级
    elif active_levels == 1:
        confidence_score *= 0.7  # 只有一个层级
    
    # 归一化置信度分数到0-1范围
    confidence_score = max(0.0, min(1.0, confidence_score))
    
    # 根据置信度分数返回等级
    if confidence_score >= CFG.MEDIUM_CONFIDENCE_SCORE:
        confidence_level = "高"
    elif confidence_score >= CFG.LOW_CONFIDENCE_SCORE:
        confidence_level = "中"
    else:
        confidence_level = "低"
    
    return {
        "score": confidence_score,
        "level": confidence_level,
        "total_samples": total_samples,
        "active_levels": active_levels,
    }


def _multi_day_prediction(o_today, levels, max_days=10):
    """多日预测：基于样本的统计分布，预测T+1到T+max_days的走势。
    带均值回归修正：长期预测向零回归，减少累积误差。
    使用动态三级权重 + 样本质量权重。"""
    LW = _dynamic_lv_weights(levels)
    tot_w = sum(LW.get(k, 0.0) for k, _ in levels) or 1.0

    def _wfield(field):
        pairs = []
        for k, smp in levels:
            lw = LW.get(k, 0.0) / tot_w
            if CFG.SIMILARITY_WEIGHTING and smp:
                tw = sum(s.get("weight", 1.0) for s in smp
                         if s.get(field) is not None) or 1.0
                for s in smp:
                    if s.get(field) is not None:
                        pairs.append((s[field], lw * s.get("weight", 1.0) / tw))
            else:
                w = lw / len(smp) if smp else 0
                pairs.extend((s[field], w) for s in smp if s.get(field) is not None)
        return pairs
    
    PS = (10, 25, 50, 75, 90)
    multi_pred = []
    
    for d in range(1, max_days + 1):
        cl_field = f"n{d}_cl"
        hi_field = f"n{d}_hi"
        lo_field = f"n{d}_lo"
        
        cl_pairs = _wfield(cl_field)
        hi_pairs = _wfield(hi_field)
        lo_pairs = _wfield(lo_field)
        
        if not cl_pairs:
            break
        
        # 区间校准：分位偏离P50放大 INTERVAL_K 倍
        K = CFG.INTERVAL_K
        c50 = wpct(cl_pairs, 50)
        h50 = wpct(hi_pairs or cl_pairs, 50)
        l50 = wpct(lo_pairs or cl_pairs, 50)
        day_pred = {
            "day": d,
            "label": f"T+{d}",
            "cl": {p: c50 + K * (wpct(cl_pairs, p) - c50) for p in PS},
            "hi": {p: h50 + K * (wpct(hi_pairs or cl_pairs, p) - h50)
                   for p in PS},
            "lo": {p: l50 + K * (wpct(lo_pairs or cl_pairs, p) - l50)
                   for p in PS},
            "up_prob": sum(w for p, w in cl_pairs if p > 0) if cl_pairs else 0.5,
        }
        
        # 均值回归修正：预测天数越多，向零回归越强
        decay = 1.0 - 0.04 * (d - 1)
        decay = max(0.6, decay)
        
        # 计算累计涨跌幅
        if d == 1:
            day_pred["cum_cl"] = day_pred["cl"][50]
            day_pred["cum_hi"] = day_pred["hi"][75]
            day_pred["cum_lo"] = day_pred["lo"][25]
        else:
            prev = multi_pred[-1]
            day_pred["cum_cl"] = (1 + prev["cum_cl"]) * (1 + day_pred["cl"][50]) - 1
            day_pred["cum_hi"] = (1 + prev["cum_hi"]) * (1 + day_pred["hi"][75]) - 1
            day_pred["cum_lo"] = (1 + prev["cum_lo"]) * (1 + day_pred["lo"][25]) - 1
        
        # 应用均值回归修正
        day_pred["cum_cl_raw"] = day_pred["cum_cl"]
        day_pred["cum_cl"] = day_pred["cum_cl"] * decay
        day_pred["cum_hi"] = day_pred["cum_hi"] * decay
        day_pred["cum_lo"] = day_pred["cum_lo"] * decay
        
        # 预测价格
        day_pred["price_cl"] = o_today * (1 + day_pred["cum_cl"])
        day_pred["price_hi"] = o_today * (1 + day_pred["cum_hi"])
        day_pred["price_lo"] = o_today * (1 + day_pred["cum_lo"])
        
        multi_pred.append(day_pred)
    
    return multi_pred


def _build_ghosts(o_today, multi_pred):
    """由多日预测构造幽灵K线（T+5/T+10；T+1 由 pred 承担）。

    2026-09-26：抽成独立函数，增量加载（_apply_progressive）也会重算，
    避免快速分析后幽灵K线与更新后的多日预测脱节/缺失。"""
    def _ghost(day_pred, label):
        if not day_pred:
            return None
        o = o_today
        if label != "T+1" and multi_pred:
            idx = int(label[2:]) - 2
            if 0 <= idx < len(multi_pred):
                o = multi_pred[idx]["price_cl"]      # 前一预测日收盘为开
        hi = day_pred.get("price_hi") or day_pred["close"]
        lo = day_pred.get("price_lo") or day_pred["close"]
        cl = day_pred.get("price_cl") or day_pred["close"]
        hi = max(hi, o, cl)
        lo = min(lo, o, cl)
        return {"date": f"{label}预测", "open": round(o, 2),
                "close": round(cl, 2), "high": round(hi, 2),
                "low": round(lo, 2), "vol": None}
    ghosts = []
    for dd in (5, 10):
        if multi_pred and len(multi_pred) >= dd:
            g = _ghost(multi_pred[dd - 1], f"T+{dd}")
            if g:
                ghosts.append(g)
    return ghosts


def analyze(full, progress=None, quick=False):
    """全量分析，切片交给GUI。
    quick=True 只做快速预览（本股缓存 + L1预测，秒开），完整历史/样本池
    由后台增量加载器继续补齐并实时更新预测K线。"""
    W = W_WINDOW
    # ---- 并发拉取全部数据源（个股行情/K线、上证行情/K线、板块、样本池）----
    now_ts = time.time()
    with _STATE_LOCK:
        sec_cached = _SECTOR_CACHE.get(full)
        sec_hit = bool(sec_cached and now_ts - sec_cached[0] < _SECTOR_CACHE_TTL)
    ex = _SHARED_EX              # 全局共享线程池
    f_q = ex.submit(fetch_quote, full)
    if CACHE_OK:
        f_rows = ex.submit(get_daily, full)
    else:
        f_rows = ex.submit(fetch_daily, full)
    f_iq = ex.submit(fetch_quote_cached, "sh000001")
    if CACHE_OK:
        f_ir = ex.submit(get_daily, "sh000001")
    else:
        f_ir = ex.submit(fetch_daily, "sh000001")
    f_sec = None if sec_hit else ex.submit(fetch_sector_context, full)
    pool_info = None
    if CACHE_OK and not quick and stocks_age() < STOCKS_TTL * 4:
        try:
            pool_info = ex.submit(pool_codes, full).result(timeout=15)
        except Exception:
            log.warning("analyze: pool_codes 失败 %s", full, exc_info=True)
            pool_info = None
    q = f_q.result()
    rows = f_rows.result()

    # 识别今日盘中bar（缓存库只存已收盘日K，实时bar由快照合成）
    today_str = time.strftime("%Y-%m-%d")
    today_compact = today_str.replace("-", "")
    live = None
    snap_full = (q.get("time") or "")
    snap_d = snap_full[:8]
    try:
        hhmm = int(snap_full[8:12])
    except ValueError:
        hhmm = 0
    # 盘中 live 仅限交易时段（9:25~15:00）；盘后不再伪装盘中
    if (snap_d == today_compact and q["price"] > 0 and 925 <= hhmm < 1500):
        pc0 = q["prev_close"] or (rows[-1]["close"] if rows else 0)
        lo0 = q["low"] if q["low"] > 0 else min(q["price"], q["open"] or q["price"])
        live = {"date": today_str, "open": q["open"] or pc0,
                "close": q["price"],
                "high": max(q["high"], q["price"]),
                "low": min(lo0, q["price"]), "vol": 0.0}
    had_today_bar = live is not None
    post_close = bool(snap_d == today_compact and q["price"] > 0
                      and hhmm >= 1500)      # 已收盘：快照为今日最终价
    if len(rows) < 30:
        raise ValueError(
            f"该股上市不足30个交易日(现仅{len(rows)}根日K)，"
            "暂无法统计预测，请过阵子再来")

    try:
        iq = f_iq.result()
        idx_chg_today = ((iq["price"] / iq["prev_close"]) * 100 - 100
                         if iq["prev_close"] else 0.0)
    except Exception:
        idx_chg_today = None
    try:
        idx_rows = f_ir.result()
        idx_chg_by_date = {
            b["date"]: (b["close"] / a["close"]) * 100 - 100
            for a, b in zip(idx_rows, idx_rows[1:])
        }
    except Exception:
        idx_chg_by_date = {}
    try:
        if sec_hit:
            sec_name, sec_chg_by_date, sec_chg_today = sec_cached[1]
        else:
            sec_name, sec_chg_by_date, sec_chg_today = f_sec.result(
                timeout=8)
            if sec_name:
                _SECTOR_CACHE[full] = (
                    time.time(),
                    (sec_name, sec_chg_by_date, sec_chg_today))
    except Exception:
        sec_name, sec_chg_by_date, sec_chg_today = None, {}, None

    closes_m = [r["close"] for r in rows]
    rets = logret(closes_m, is_etf=_is_etf(full))

    vols_m = [r.get("vol") or 0.0 for r in rows]
    vr_arr = [vol_ratio_at(vols_m, k) for k in range(len(vols_m))]
    vr_now = vr_arr[-1]
    cur_regime = vol_regime(vr_now)

    # 新股自适应：历史不足时缩短匹配窗口（最低5日）。
    # 不重叠匹配要求窗口数 = len(rets) - 2W + 1，至少保留2个样本窗口
    while W > 5 and len(rets) - 2 * W + 1 < 2:
        W -= 1
    if len(rets) - 2 * W + 1 < 1:
        raise ValueError("历史K线过短，暂无法统计预测")

    def _dist_vol(i):
        vr_i = vr_arr[i]
        if vr_now is None or vr_i is None:
            return None
        return abs(math.log(max(vr_now, 1e-6) / max(vr_i, 1e-6)))

    def _dist_idx(i):
        ic = idx_chg_by_date.get(rows[i]["date"])
        if ic is None or idx_chg_today is None:
            return None
        return abs(ic - idx_chg_today)          # 百分点差

    def _dist_sec(i):
        sc = sec_chg_by_date.get(rows[i]["date"])
        if sc is None or sec_chg_today is None:
            return None
        return abs(sc - sec_chg_today)

    cur = znorm(rets[-W:])
    # 今日侧多维特征（供 L1/L2/L3 匹配共用）
    cur_ctx = _cur_context(rows, rets, vols_m, closes_m)
    # 样本侧特征预计算（O(1)查表，避免逐日重复计算）
    _l1_rsi = [rsi_at(closes_m, k) for k in range(len(closes_m))]
    _l1_vola = [vola_at(rets, k) for k in range(len(rets))]
    _l1_volchg = [volchg_at(vols_m, k) for k in range(len(vols_m))]
    _l1_weekly = [weekly_ctx(rows, k, CFG.WEEKLY_N) for k in range(len(rows))]
    _l1_struct = [candle_feats(rows, k) for k in range(len(rows))]
    sims = []
    d_px_arr = _px_distances(rets, cur, W)   # numpy向量化（可用时）
    for i in range(W, len(rets) - W + 1):
        d_px = d_px_arr[i - W]
        d_v = _dist_vol(i)
        d_i = _dist_idx(i)
        d_s = _dist_sec(i)
        d_x = _dist_extra(cur_ctx, {
            "struct": _l1_struct[i], "vola": _l1_vola[i],
            "rsi": _l1_rsi[i], "volchg": _l1_volchg[i],
            "weekly": _l1_weekly[i]})
        score = (d_px
                 + (0.6 * min(d_v, 2.5) if d_v is not None else 0.30)
                 + (min(1.5, 0.3 * d_i) if d_i is not None else 0.40)
                 + (min(1.2, 0.25 * d_s) if d_s is not None else 0.30)
                 + d_x)
        sims.append((score, i))
    top = heapq.nsmallest(CFG.CANDIDATE_TOPK, sims, key=lambda x: x[0])

    prev_close = q["prev_close"] or closes_m[-1]
    # 盘前检测：快照日期已切到今日，但日K还没有今日bar → 尚未开盘，
    # 无今开可锚，改锚昨收（否则会把上一交易日的开价误当"今开"）
    snap_d = (q.get("time") or "")[:8].replace("-", "")
    today_compact = today_str.replace("-", "")
    pre_open = (not had_today_bar and not post_close
                and snap_d >= today_compact)
    stale_snap = bool(snap_d) and snap_d < today_compact
    if post_close:
        o_today = q["price"] or prev_close
        anchor = "今收"
    elif pre_open:
        o_today = prev_close
        anchor = "昨收(未开盘)"
    elif live is not None:
        o_today = q["open"] or prev_close
        anchor = "今开"
    elif stale_snap:
        # 快照停在上一交易日（休市/停牌/数据未更新）：以快照收盘为锚。
        # 不能再用 q["prev_close"]（那是再前一日收盘，会把预测整体锚错一天）
        o_today = q["price"] or prev_close
        anchor = f"最近收盘({snap_d[4:6]}-{snap_d[6:8]})"
    else:
        o_today = prev_close
        anchor = "今收"
    gap_today = (o_today / prev_close - 1) * 100

    # 市场阶段（按行情快照时间）
    phase = market_phase_text(q.get("time"))
    next_label = ("今日(T)" if (pre_open and not post_close)
                  else ("下一交易日(T+1)" if stale_snap else "次日(T+1)"))
    t_pred_label = ("下一交易日收盘预测" if stale_snap
                    else "今日(T)收盘预测")

    samples = []
    max_pred_days = 10  # 最多预测10天
    for score, i in top:
        r = rows[i]
        ic = idx_chg_by_date.get(r["date"])
        sc = sec_chg_by_date.get(r["date"])
        sample = {
            "t_date": r["date"],
            "vr": vr_arr[i],
            "idx_chg": (ic - idx_chg_today
                        if (ic is not None and idx_chg_today is not None)
                        else None),
            "sec_d": (sc - sec_chg_today
                      if (sc is not None and sec_chg_today is not None)
                      else None),
            "gap": rows[i + 1]["open"] / r["close"] - 1 if i + 1 < len(rows) else None,
            "similarity_score": score,
            "weight": (math.exp(-min(max(0.0, score - top[0][0]), 6.0) / 0.9)
                       if CFG.SIMILARITY_WEIGHTING else 1.0),
            "_match_i": i,        }
        # 时间衰减（L1样本；与_pool_match同款，消融回测证实有益）
        if CFG.TIME_DECAY_ENABLED:
            try:
                age = (time.mktime(time.strptime(
                    time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
                    - time.mktime(time.strptime(r["date"], "%Y-%m-%d"))) / 86400.0
                if age > CFG.TIME_DECAY_DAYS:
                    sample["weight"] *= max(
                        CFG.TIME_DECAY_RATE,
                        1.0 - (age - CFG.TIME_DECAY_DAYS) / 365.0 * 0.5)
            except (ValueError, TypeError):
                pass
        # 扩展样本：记录T+1到T+max_pred_days的相对前日收盘涨跌幅
        # 与 Open→Close/High/Low（盘中预测用，逻辑与今开锚定一致）
        for d in range(1, max_pred_days + 1):
            if i + d < len(rows):
                nd = rows[i + d]
                prev_c = rows[i + d - 1]["close"] if i + d - 1 >= 0 else r["close"]
                op = nd.get("open") or prev_c
                sample[f"n{d}_date"] = nd["date"]
                sample[f"n{d}_cl"] = nd["close"] / prev_c - 1
                sample[f"n{d}_hi"] = nd["high"] / prev_c - 1
                sample[f"n{d}_lo"] = nd["low"] / prev_c - 1
                sample[f"n{d}_oc"] = nd["close"] / op - 1 if op > 0 else None
                sample[f"n{d}_oh"] = nd["high"] / op - 1 if op > 0 else None
                sample[f"n{d}_ol"] = nd["low"] / op - 1 if op > 0 else None
            else:
                for suf in ("date", "cl", "hi", "lo", "oc", "oh", "ol"):
                    sample[f"n{d}_{suf}"] = None
        samples.append(sample)
    # 样本分层筛选：① 量能状态+大盘涨跌接近 ② 开盘缺口接近 ③ 全部
    for s in samples:
        s["regime"] = vol_regime(s.get("vr"))
    sel_ctx = [
        s for s in samples
        if s["regime"] == cur_regime
        and s["idx_chg"] is not None and abs(s["idx_chg"]) <= 0.8
        and (s["sec_d"] is None or abs(s["sec_d"]) <= 1.2)
    ]
    sel_gap = [s for s in samples if abs(s["gap"] * 100 - gap_today) <= 1.0]
    if len(sel_ctx) >= 3:
        src, filter_note = sel_ctx, f"量能({cur_regime})+大盘(±0.8pp)筛选"
    elif len(sel_gap) >= 3:
        src, filter_note = sel_gap, "按开盘缺口筛选"
    else:
        src, filter_note = samples, "使用全部样本"
    # 去重挑选：间隔 W//2 → 不足则分级放宽(3/1)，避免样本被砍光
    src_sorted = sorted(src, key=lambda x: x.get("similarity_score", 9.0))
    picked = []

    def _pick(gap):
        pl, used = [], []
        for s in src_sorted:
            ii = s.get("_match_i")
            if ii is not None and any(abs(ii - j) < gap for j in used):
                continue
            pl.append(s)
            if ii is not None:
                used.append(ii)
            if len(pl) >= TOPK:
                break
        return pl

    # v6.1.5 热修⑦：首选 W（样本窗口互不重叠），不足再逐级放宽；
    # 原来首档就 W//2，选出的样本窗口彼此重叠一半（同一次波动被重复计入，
    # 融合权重被同一行情灌水）。
    for gap in (W, max(3, W // 2), 3, 1):
        picked = _pick(gap)
        if len(picked) >= min(TOPK, 6):
            break
    if picked:
        src = picked

    # ---- 二三级样本池：L2 同行业(传统行业+ETF) / L3 已删 ----
    # 题材行业/ETF 只用 L1（交易回测：题材L2无增益，ETF L2劣于持有）
    level_map = {}
    pool_note = ""
    my_info = get_stock_info(full) or {}
    solo_l1 = _is_etf(full) or _is_theme_industry(my_info.get("industry"))
    if solo_l1:
        pool_note = "样本池: 题材/ETF仅L1"
    elif CACHE_OK and pool_info:
        try:
            if progress:
                progress("拉取同行/同市值层K线(首次回填较慢)...")
            level_map = _load_pools(pool_info, cur, vr_now,
                                    idx_chg_by_date, idx_chg_today,
                                    progress, cur_ctx=cur_ctx)
            parts = [f"{LV_LABEL['L1']}{len(src)}"]
            parts += [f"{LV_LABEL[k]}{len(v)}"
                      for k, v in sorted(level_map.items()) if v]
            pool_note = "样本池: " + "+".join(parts)
        except Exception as e:
            pool_note = f"样本池不可用({e.__class__.__name__})"

    # ---- 三级加权融合：L1 0.6 / L2 0.3 / L3 0.1 ----
    levels = [("L1", src)] + [(k, v) for k, v in sorted(level_map.items())
                              if v]
    t_pred, pred, clamped, tpred_bar = _fusion_prediction(
        o_today, pre_open, live, levels)
    
    # ---- 多日预测：T+1到T+10 ----
    multi_pred = _multi_day_prediction(o_today, levels, max_days=CFG.PRED_MAX_DAYS)

    # 指标基于 匹配历史(+今日盘中) 计算
    disp_rows = rows + ([live] if live else [])
    closes_i = [r["close"] for r in disp_rows]
    dif, dea, mhist = calc_macd(closes_i)
    k_, d_, j_ = calc_kdj(disp_rows)
    r6, r12 = calc_rsi(closes_i, 6), calc_rsi(closes_i, 12)
    b_mid, b_up, b_low = calc_boll(closes_i)
    pdi_a, mdi_a, adx_a = calc_adx(disp_rows)
    mas = {n: sma_period(closes_i, n) for n in MA_COLORS}

    # ---- 所选策略（meta缓存，5日过期；无缓存用默认多维·稳健）----
    strat = load_strategy(full)
    rp = dict(strat["params"]) if (strat and strat.get("params")) \
        else CFG.risk_params()
    sel_algo = (strat or {}).get("algo", "composite")
    if sel_algo not in ALGO_LABEL:
        sel_algo = "composite"
    sel_mode = (strat or {}).get("mode") or CFG.RISK_MODE

    signals = []
    # ---- 指标型策略：直接按该算法规则生成历史买卖点（近250根，同策略）----
    if sel_algo in ("macd", "kdj", "rsi", "boll", "ma_trend", "l1_pattern",
                    "chip_peak", "sector_rot"):
        try:
            if sel_algo == "chip_peak":
                raw = _sig_chip_peak(disp_rows)
            elif sel_algo == "sector_rot":
                raw = _sig_sector_rot(
                    disp_rows, industry=(my_info.get("industry") or ""))
            else:
                raw = {"macd": _sig_macd, "kdj": _sig_kdj, "rsi": _sig_rsi,
                       "boll": _sig_boll, "ma_trend": _sig_ma_trend,
                       "l1_pattern": _sig_l1_pattern}[sel_algo](disp_rows)
            cut = max(1, len(disp_rows) - 250)
            signals = [s for s in raw if s[0] >= cut]
        except Exception:
            log.exception("策略%s信号生成失败(回退多维评分)", sel_algo)
            sel_algo = "composite"
    # ---- 多维打分：每日综合评分，方向切换时生成买卖信号 ----
    # 评分维度：MACD趋势、KDJ状态、RSI超买超卖、量价配合、MA20趋势、
    #          筹码位置、统计偏多/偏空；合计≥2→多头信号，≤-2→空头信号
    _bull_scores = []   # (index, date, score, reasons)
    start = max(1, len(disp_rows) - 120) if sel_algo == "composite" \
        else len(disp_rows)      # 指标型策略跳过多维打分循环
    vols_d = [r.get("vol") or 0.0 for r in disp_rows]
    try:
        chip_snaps = chip_snapshots(disp_rows, tail=120)
    except Exception:
        chip_snaps = {}

    for i in range(start, len(disp_rows)):
        if None in (dif[i], dea[i], dif[i - 1], dea[i - 1]):
            _bull_scores.append((i, disp_rows[i]["date"], 0, []))
            continue
        sc = 0
        reasons = []

        def _wadd(dim, pts, reason=None):
            """按 CFG.IND_W 权重加权计分（四舍五入取整，保留符号）。"""
            nonlocal sc
            sc += int(round(pts * CFG.IND_W.get(dim, 1.0)))
            if reason and pts:
                reasons.append(reason)

        # MACD（权重1.1：趋势主指标）
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            _wadd("MACD", 2, "MACD金叉")
        elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            _wadd("MACD", -2, "MACD死叉")
        elif dif[i] > dea[i]:
            _wadd("MACD", 1, "DIF>DEA")
        else:
            _wadd("MACD", -1, "DIF<DEA")
        # KDJ（权重0.9：摆动指标，横盘易钝化）
        if k_[i - 1] <= d_[i - 1] and k_[i] > d_[i] and k_[i] < 45:
            _wadd("KDJ", 2, "KDJ低位金叉")
        elif k_[i - 1] >= d_[i - 1] and k_[i] < d_[i] and k_[i] > 65:
            _wadd("KDJ", -2, "KDJ高位死叉")
        elif k_[i] > d_[i]:
            _wadd("KDJ", 1)
        else:
            _wadd("KDJ", -1)
        # RSI（权重0.9）
        if r6[i] is not None and r6[i - 1] is not None:
            if r6[i - 1] < 20 and r6[i] >= 20:
                _wadd("RSI", 2, "RSI超卖回升")
            elif r6[i - 1] > 80 and r6[i] <= 80:
                _wadd("RSI", -2, "RSI超买回落")
            elif r6[i] < 30:
                _wadd("RSI", 1)
            elif r6[i] > 70:
                _wadd("RSI", -1)
        # 量价（权重1.0）
        c, cp = disp_rows[i]["close"], disp_rows[i - 1]["close"]
        v5 = sum(vols_d[max(0, i - 5):i]) / max(1, min(5, i))
        vr_d = vols_d[i] / v5 if v5 > 0 else 0.0
        if vr_d > 1.5 and c > cp:
            _wadd("量价", 1, "放量上涨")
        elif vr_d > 1.5 and c < cp:
            _wadd("量价", -1, "放量下跌")
        # MA20趋势（权重1.0）
        ma20, ma20p = mas[20][i], mas[20][i - 1]
        if ma20 and ma20p:
            if c > ma20 and ma20 > ma20p:
                _wadd("MA20", 1)
            elif c < ma20 and ma20 < ma20p:
                _wadd("MA20", -1)
        # 筹码（权重0.8）
        snap = chip_snaps.get(disp_rows[i]["date"])
        if snap:
            sup_i, res_i = snap[0], snap[1]
            if sup_i and c <= sup_i * 1.01:
                _wadd("筹码", 1, "贴近支撑")
            elif res_i and c >= res_i * 0.99:
                _wadd("筹码", -1, "贴近压力")
        # 布林带（权重0.8：均值回归参考，震荡市才准）
        bu_i, bl_i = b_up[i], b_low[i]
        if None not in (bu_i, bl_i):
            if c < bl_i:
                _wadd("布林带", 1, "布林下轨超卖")
            elif c > bu_i:
                _wadd("布林带", -1, "布林上轨超买")
            elif (disp_rows[i - 1]["close"] <= (b_low[i - 1] or 0)
                    and c > bl_i):
                _wadd("布林带", 1, "布林下轨回升")
            elif (disp_rows[i - 1]["close"] >= (b_up[i - 1] or 1e18)
                    and c < bu_i):
                _wadd("布林带", -1, "布林上轨回落")
        # ADX（权重0.8：趋势强度过滤——只有 ADX≥20 趋势成立时，
        # DI 方向才计分；横盘时不贡献分数）
        a_i, p_i, m_i = adx_a[i], pdi_a[i], mdi_a[i]
        if None not in (a_i, p_i, m_i) and a_i >= 20:
            if p_i > m_i:
                _wadd("ADX", 1, "ADX趋势偏多" if a_i >= 25 else None)
            elif m_i > p_i:
                _wadd("ADX", -1, "ADX趋势偏空" if a_i >= 25 else None)
        # 统计样本维度不放历史打分：今日匹配样本不能用于标注过去（防前视）
        # 最新一天的样本倾向已由综合评估中的"统计预测"维度体现
        _bull_scores.append((i, disp_rows[i]["date"], sc, reasons))

    # 方向切换触发：多头得分≥2且前一次信号为空头→BUY；空头得分≤-2且前一次为多头→SELL
    prev_dir = 0   # 0=无信号, 1=多头, -1=空头
    cooldown = 0
    _bear_words = {"DIF<DEA", "放量下跌", "贴近压力",
                   "布林上轨超买", "布林上轨回落"}
    _bull_words = {"DIF>DEA", "放量上涨", "贴近支撑",
                   "布林下轨超卖", "布林下轨回升"}

    def _weak_day(day):
        """该交易日的市场是否弱势——只用当日(及以前)的大盘/板块数据，
        不再引用 idx_chg_today/sec_chg_today 全局值，消除历史信号前视。"""
        ic = idx_chg_by_date.get(day)
        sc = sec_chg_by_date.get(day)
        weak = False
        if ic is not None and ic < CFG.WEAK_IDX_TH:
            weak = True
        if sc is not None and sc < CFG.WEAK_SEC_TH:
            weak = True
        return weak

    for idx_i, day, sc, reasons in _bull_scores:
        if cooldown > 0:
            cooldown -= 1
            continue

        # 弱势行情过滤：当日大盘弱势时 BUY 加严；买入阈值/冷却按所选策略
        buy_threshold = (rp["buy_th"] + 1) if _weak_day(day) else rp["buy_th"]
        sell_threshold = CFG.SIGNAL_SCORE_SELL

        if sc >= buy_threshold and prev_dir <= 0:
            bull_r = [r for r in reasons if r not in _bear_words]
            reason_str = "多维偏多 " + " ".join(bull_r or reasons)
            if _weak_day(day):
                reason_str += " [弱势谨慎]"
            signals.append((idx_i, day, "BUY", reason_str))
            prev_dir = 1
            cooldown = rp["cooldown"]
        elif sc <= sell_threshold and prev_dir >= 0:
            bear_r = [r for r in reasons if r not in _bull_words]
            signals.append((idx_i, day, "SELL",
                            "多维偏空 " + " ".join(bear_r or reasons)))
            prev_dir = -1
            cooldown = CFG.SIGNAL_COOLDOWN

    # ---- 波段适合度路由：仅多维评分策略时替换信号；指标型策略尊重用户选择 ----
    band_score = _band_fit_score(disp_rows, mas, vr_arr)
    band_fit = band_score >= CFG.BAND_FIT_MIN
    if sel_algo != "composite":
        band_algo = f"策略·{ALGO_LABEL.get(sel_algo, sel_algo)}"
    elif band_fit:
        band_algo = "波段·多维融合"
    else:
        # 不适合波段：改用长周期 MA20/MA60 趋势跟踪，信号少而稳
        t_signals = _trend_track_signals(disp_rows, mas,
                                         idx_chg_by_date, idx_chg_today)
        if t_signals:
            signals = t_signals
            band_algo = "趋势跟踪·MA20/60"
        else:
            # 趋势跟踪零信号（震荡股无MA金叉）→ 回退多维信号，避免无买卖点
            band_algo = "趋势跟踪·MA20/60（无信号→回退多维）"
    # 连续同向信号压缩：同一轮机会只保留首个 B/S 标注（回测开平仓语义不变）
    signals = _dedup_signals(signals)
    # ---- 激进档「多交易」兜底：所选策略近250日信号过少时改用多维评分 ----
    # （沿用该档风险参数：激进=买点门槛1/冷却3），保证震荡区间（如 5~6 元
    # 箱体）也能标出足够波段买卖点。只影响展示与样本内统计，不改动消融缓存。
    _win = max(1, len(disp_rows) - 250)
    _recent_n = len([s for s in signals if s[0] >= _win])
    _min_need = 8 if sel_mode == "激进" else 2
    if _recent_n < _min_need:
        try:
            _fb = [s for s in _composite_signals(disp_rows, rp)
                   if s[0] >= max(1, len(disp_rows) - 120)]
        except Exception:
            log.exception("信号过少兜底失败")
            _fb = []
        if _fb:
            signals = _dedup_signals(_fb)
            _why = (f"近250日仅{_recent_n}个信号" if _recent_n
                    else "近250日无信号")
            band_algo += f"（{_why} → 兜底改用多维评分·{sel_mode}）"
    band_note = f"波段适合度 {band_score:.0f}/100 → {band_algo}"

    vols = [r["vol"] for r in disp_rows]
    cur_px = q["price"] if q and q.get("price") else disp_rows[-1]["close"]
    chips = None
    try:
        chips = calc_chips(disp_rows, cur_px)
    except Exception:
        pass

    # ---- 综合评估：多维打分，作为买卖点综合参考 ----
    action = None
    try:
        i = len(disp_rows) - 1
        c = disp_rows[i]["close"]
        pc = disp_rows[i - 1]["close"] if i else c
        items = []
        ma20, ma20p = mas[20][i], mas[20][i - 1] if i else None
        if ma20 and ma20p:
            if c > ma20 and ma20 > ma20p:
                items.append(("MA20趋势", 1, "价站上MA20且MA20向上"))
            elif c < ma20 and ma20 < ma20p:
                items.append(("MA20趋势", -1, "价跌破MA20且MA20向下"))
            else:
                items.append(("MA20趋势", 0, "MA20方向不明"))
        dif_i, dea_i = dif[i], dea[i]
        mh_i, mh_p = mhist[i], mhist[i - 1] if i else None
        if None not in (dif_i, dea_i, mh_i, mh_p):
            if dif_i > dea_i and mh_i >= mh_p:
                items.append(("MACD", 1, "DIF>DEA且柱体走强"))
            elif dif_i < dea_i and mh_i <= mh_p:
                items.append(("MACD", -1, "DIF<DEA且柱体走弱"))
            else:
                items.append(("MACD", 0, "多空转换中"))
        k_i, d_i, j_i = k_[i], d_[i], j_[i]
        if None not in (k_i, d_i):
            if k_i > d_i and j_i < 90:
                items.append(("KDJ", 1, f"K{k_i:.0f}>D{d_i:.0f}"))
            elif k_i < d_i and j_i > 10:
                items.append(("KDJ", -1, f"K{k_i:.0f}<D{d_i:.0f}"))
            else:
                items.append(("KDJ", 0, "超买超卖区待修复"))
        r6_i = r6[i]
        if r6_i is not None:
            if r6_i < 30:
                items.append(("RSI", 1, f"RSI6={r6_i:.0f} 超卖"))
            elif r6_i > 70:
                items.append(("RSI", -1, f"RSI6={r6_i:.0f} 超买"))
            else:
                items.append(("RSI", 0, f"RSI6={r6_i:.0f} 中性"))
        v_i = vols_d[i] if disp_rows[i].get("vol") else 0.0
        v5 = (sum(vols_d[max(0, i - 5):i]) / 5) if i >= 5 else 0.0
        if v_i and v5 and v_i > v5 * 1.2:
            if c > pc:
                items.append(("量价", 1, "放量上涨"))
            else:
                items.append(("量价", -1, "放量下跌"))
        else:
            items.append(("量价", 0, "量能平稳"))
        if chips:
            sup_i, res_i = chips.get("sup"), chips.get("res")
            if sup_i and c <= sup_i * 1.01:
                items.append(("筹码", 1, f"贴近支撑{sup_i:.2f}"))
            elif res_i and c >= res_i * 0.99:
                items.append(("筹码", -1, f"贴近压力{res_i:.2f}"))
            elif chips["p5"] <= c <= chips["p95"]:
                items.append(("筹码", 0, "处于筹码密集区中部"))
        # 布林带：位置 + 中轨方向
        bu_i, bl_i, bm_i = b_up[i], b_low[i], b_mid[i]
        if None not in (bu_i, bl_i, bm_i):
            bm_p = b_mid[i - 1] if i else None
            mid_up = (bm_p is not None and bm_i > bm_p)
            if c > bu_i:
                items.append(("布林带", -1,
                              f"高于上轨{bu_i:.2f} 超买注意回落"))
            elif c < bl_i:
                items.append(("布林带", 1,
                              f"低于下轨{bl_i:.2f} 超卖关注反弹"))
            elif c > bm_i and mid_up:
                items.append(("布林带", 1,
                              f"中轨{bm_i:.2f}上方且中轨向上"))
            elif c < bm_i and not mid_up:
                items.append(("布林带", -1,
                              f"中轨{bm_i:.2f}下方且中轨向下"))
            else:
                items.append(("布林带", 0,
                              f"中轨{bm_i:.2f}附近 方向不明"))
        # ADX：趋势强度
        a_i, p_i, m_i = adx_a[i], pdi_a[i], mdi_a[i]
        if None not in (a_i, p_i, m_i):
            if a_i >= 25:
                items.append(("ADX", 1 if p_i > m_i else -1,
                              f"ADX={a_i:.0f} 强趋势"
                              f"{'偏多' if p_i > m_i else '偏空'}"))
            elif a_i >= 20:
                items.append(("ADX", 1 if p_i > m_i else -1 if p_i != m_i else 0,
                              f"ADX={a_i:.0f} 趋势形成中"))
            else:
                items.append(("ADX", 0, f"ADX={a_i:.0f} 无趋势震荡"))
        up_p = t_pred["up_prob"]
        if up_p >= 0.55:
            items.append(("统计预测", 1, f"上行概率{up_p*100:.0f}%"))
        elif up_p <= 0.45:
            items.append(("统计预测", -1, f"上行概率{up_p*100:.0f}%"))
        else:
            items.append(("统计预测", 0, f"上行概率{up_p*100:.0f}%"))
        # 多日预测趋势评估
        if multi_pred and len(multi_pred) >= 3:
            short_trend = multi_pred[2]["cum_cl"] if len(multi_pred) >= 3 else 0
            mid_trend = multi_pred[min(4, len(multi_pred) - 1)]["cum_cl"] if len(multi_pred) >= 5 else short_trend
            
            if short_trend > 0.02 and mid_trend > 0.03:
                items.append(("多日预测", 1, f"短期+{short_trend*100:.1f}% 中期+{mid_trend*100:.1f}% 看涨"))
            elif short_trend < -0.02 and mid_trend < -0.03:
                items.append(("多日预测", -1, f"短期{short_trend*100:.1f}% 中期{mid_trend*100:.1f}% 看跌"))
            elif short_trend > 0.01:
                items.append(("多日预测", 1, f"短期+{short_trend*100:.1f}% 偏多"))
            elif short_trend < -0.01:
                items.append(("多日预测", -1, f"短期{short_trend*100:.1f}% 偏空"))
            else:
                items.append(("多日预测", 0, f"短期{short_trend*100:+.1f}% 震荡"))
        if samples:
            avg1 = sum(x["n1_cl"] for x in samples if x.get("n1_cl") is not None) / len(samples)
            items.append(("相似样本", 1 if avg1 > 0 else -1,
                          f"次日均涨跌{avg1*100:+.1f}%"))
        # 各维度加权求和（权重与信号打分共用 CFG.IND_W）
        score = sum(int(round(s * CFG.IND_W.get(lab, 1.0)))
                    for lab, s, _ in items)
        if score >= 4:
            verdict = "多维共振偏多·买点参考"
        elif score >= 2:
            verdict = "略偏多·轻仓试探"
        elif score > -2:
            verdict = "多空交织·观望"
        elif score > -4:
            verdict = "略偏空·减仓留意"
        else:
            verdict = "多维共振偏空·卖点参考"
        action = {"score": score, "verdict": verdict, "items": items,
                  "band_fit": band_fit, "band_score": band_score,
                  "band_note": band_note}
    except Exception:
        log.exception("综合评估计算失败(action=None)")

    # ---- 回测统计：基于全部历史信号计算胜率/盈亏/年化（按所选策略参数）----
    bt_stats = None
    if signals and len(signals) >= 2:
        try:
            bt_stats = backtest_signals(disp_rows, signals, rp=rp)
        except Exception:
            log.exception("回测统计失败(bt_stats=None)")

    # ---- 幽灵K线：T+5 / T+10（白色虚线边框，随所选策略融合预测）----
    # T+1 已由 pred 承担（slice_view 追加）；增量加载时同口径重算（见 _apply_progressive）
    ghosts = _build_ghosts(o_today, multi_pred)

    return {
        "quote": q, "full_code": full, "disp_rows": disp_rows,
        "anchor": anchor, "pre_open": pre_open, "stale_snap": stale_snap,
        "t_pred_label": t_pred_label,
        "phase": phase, "next_label": next_label,
        "tpred_bar": tpred_bar,
        "t5_pred": t_pred.get("t5"),
        "pred": pred, "t_pred": t_pred, "multi_pred": multi_pred,
        "ghosts": ghosts, "o_today": o_today,
        "strategy": strat,
        "risk_mode": (strat or {}).get("mode",
                                       CFG.RISK_MODE if sel_algo == "composite"
                                       else "稳健"),
        "sel_algo": sel_algo,
        "samples": samples, "src_n": len(src),
        "filtered": src is not samples,
        "filter_note": filter_note,
        "levels": [{"key": k, "label": LV_LABEL[k], "n": len(smp),
                    "samples": smp,
                    "up_prob": (len([s for s in smp if s.get("n1_cl") is not None and s["n1_cl"] > 0])
                                / len(smp)) if smp else 0.5}
                   for k, smp in levels],
        "pool_note": pool_note,
        "idx_chg_today": idx_chg_today, "vr_now": vr_now,
        "cur_regime": cur_regime,
        "sector_name": sec_name, "sector_chg_today": sec_chg_today,
        "ind": {"ma": mas, "dif": dif, "dea": dea, "mhist": mhist,
                "k": k_, "d": d_, "j": j_, "rsi6": r6, "rsi12": r12,
                "boll_mid": b_mid, "boll_up": b_up, "boll_low": b_low, "pdi": pdi_a, "mdi": mdi_a, "adx": adx_a},
        "vols": vols,
        "chips": chips,
        "action": action,
        "signals": signals,
        "bt_stats": bt_stats,
        "band_fit": band_fit, "band_score": band_score,
        "band_algo": band_algo, "band_note": band_note,
        "gap_today": gap_today, "prev_close": prev_close,
        "has_live": bool(live),
        "live_high": live["high"] if live else None,
        "live_low": live["low"] if live else None,
        "clamped": clamped,
        "quick": bool(quick),
        "_ctx": {
            "full": full, "o_today": o_today, "pre_open": pre_open,
            "live": live, "src": src, "cur": cur,
            "cur_ctx": cur_ctx,
            "vr_now": vr_now, "idx_chg_by_date": idx_chg_by_date,
            "idx_chg_today": idx_chg_today, "gap_today": gap_today,
            "prev_close": prev_close, "pool_info": pool_info,
        },
    }


# ================= v4.0 自适应ML研究引擎 =================
# 设计约束（防数据泄漏）：
#   1. 特征只用 T 日及以前数据；标签 = Close[T+H]/Close[T]-1 仅作训练目标
#   2. StandardScaler / Lasso 因子筛选 / Horizon 选择 全部只看训练段
#   3. Walk-Forward 扩展窗：每折用折前全部数据训练，折内预测，折间不重叠
#   4. LightGBM 超参先验固定（浅树/少叶/强正则/子采样），不用 Test 调参
#   5. ATR/移动止盈风控保留为对照退出（消融 + 极端行情防火墙），不删除

_V4_FACTORS = ("l1_up", "l1_ret", "bias20", "bias60", "ret5", "ret10",
               "ret20", "rsi14", "atr_pct", "vola20", "volchg",
               "mkt5", "ind5")
_V4_HORIZONS = (1, 5, 10)
_V4_FOLD = 40            # Walk-Forward 折大小（交易日）
_V4_WARMUP = 60          # 因子预热期
_V4_TRAIN_MIN = 180      # 首折最少训练样本
_V4_EMBARGO = 5          # 训练/测试折间 embargo（根），防标签通过自相关泄漏
_V4_QTS = (10, 25, 50, 75, 90)
# v4.0.1：去掉佣金/印花税（记 0），只保留滑点——用户实际交易成本以滑点为主；
# 键名保留以兼容旧报告结构，数值为 0 时乘法天然退化
_V4_COST = {"slip": 0.001, "commission": 0.0, "stamp": 0.0}
_V4_CAPITAL = 1_000_000.0
# 预测缓存版本：**改因子/模型/折参数代码后必须 +1**，否则旧缓存被误用
_V4_CACHE_VER = "3"

# 三档风险：同一套 v4 模型输出上的不同决策层参数（不分别训练）
# a_th 为自适应分数的 σ 阈值（训练段标准化后）
_V4_TIERS = {
    "保守": {"p_th": 0.62, "r_th": 0.012, "a_th": 1.00,
             "frac": 0.12, "max_pos": 10, "exit_p": 0.50},
    "平衡": {"p_th": 0.55, "r_th": 0.006, "a_th": 0.70,
             "frac": 0.20, "max_pos": 6, "exit_p": 0.45},
    "激进": {"p_th": 0.52, "r_th": 0.000, "a_th": 0.40,
             "frac": 0.33, "max_pos": 4, "exit_p": 0.45},
}

# LightGBM 先验超参（防过拟合：浅树/少叶/强正则/子采样，不按Test调）
_V4_LGBM = {"objective": "regression", "num_leaves": 15, "max_depth": 4,
            "learning_rate": 0.05, "n_estimators": 200,
            "min_child_samples": 40, "subsample": 0.8, "subsample_freq": 1,
            "colsample_bytree": 0.8, "reg_lambda": 1.0, "reg_alpha": 0.0,
            "random_state": 42, "n_jobs": 1, "verbose": -1}


# 分档默认规则补丁（backtest_v4_entry_exit_opt.py 进入/退出专项，2026-09-13）：
# - 保守：新增低分化入场闸门 disp_max=0.5（行业离散度因果分位≤0.5 才开新仓）。
#   T12选参 / S3留出切片 / 5折滚动WF / 连续滚动 四口径一致改善，且
#   disp_max 0.4~0.7 为平台非孤峰（见 research/v4_entry_exit_opt.json）。
#   另叠加 Rot-T10only（h_only=10+冷却5+最短持有5+行业前30%）：连续滚动
#   P50 -10.8%→+6.3%、最差窗 -20.5%→-4.6%、逐折 3/5 胜且两处巨亏折修复
#   （validate：研究脚本 / README 进入退出专节）。
# - 平衡：p_up 退出阈值 0.45→0.50（各口径一致小幅改善）。
# - 激进：无稳健胜出候选（weak 止损切片口径好、连续口径差，属口径矛盾），
#   维持默认；Rot-T10only 对激进削左尾但压上限（逐折中位反而略降），保留
#   为 _V4_VARIANTS 可选变体，不写死默认。详见 opt 报告与 README。
# - 同时修正 disp_rank 前视：原全样本 argsort 排名 → 截至当日的扩张窗口
#   分位（60 个有效日起），旧 disp 系变体（如 RotT10+DispHi）成绩作废。
# 注：每股「多算法消融选策略」是独立机制（run_ablation /
# backtest_strategy_ablation.py），本补丁只作用于 v4 组合默认。
_V4_TIER_EXTRA = {
    "保守": {"weak_q": 25, "weak_mkt": -0.006, "disp_max": 0.5,
             "h_only": 10, "cooldown": 5, "min_hold": 5, "rot_top": 0.70},
    "平衡": {"weak_q": 25, "weak_mkt": -0.006, "exit_p": 0.50},
    "激进": {},
}


def _v4_deps():
    """依赖探测：缺库时如实记录，不偷偷换模型。"""
    out = {}
    for m in ("numpy", "sklearn", "lightgbm", "scipy"):
        try:
            mod = __import__(m)
            out[m] = getattr(mod, "__version__", "?")
        except ImportError:
            out[m] = None
    return out


def _v4_rankic(a, b):
    """Spearman 秩相关（numpy 实现）。样本不足/无差异返回 None。"""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    m = np.isfinite(a) & np.isfinite(b)
    a, b = a[m], b[m]
    if len(a) < 25:
        return None
    ra = a.argsort().argsort().astype(np.float64)
    rb = b.argsort().argsort().astype(np.float64)
    ra -= ra.mean()
    rb -= rb.mean()
    den = math.sqrt(float((ra * ra).sum()) * float((rb * rb).sum()))
    return float((ra * rb).sum() / den) if den > 0 else None


def _v4_roll_mean(a, w):
    """滚动均值（NaN 感知：窗口内跳过缺失，不向后续传播）。"""
    a = np.asarray(a, float)
    n = len(a)
    out = np.full(n, np.nan)
    if n >= w:
        x = np.nan_to_num(a, nan=0.0)
        v = np.isfinite(a).astype(float)
        cx = np.insert(np.cumsum(x), 0, 0.0)
        cv = np.insert(np.cumsum(v), 0, 0.0)
        cnt = cv[w:] - cv[:-w]
        s = cx[w:] - cx[:-w]
        out[w - 1:] = np.divide(s, cnt,
                                out=np.full_like(s, np.nan), where=cnt > 0)
    return out


def _v4_roll_std(a, w):
    m = _v4_roll_mean(a, w)
    m2 = _v4_roll_mean(np.asarray(a, float) ** 2, w)
    return np.sqrt(np.maximum(m2 - m * m, 0.0))


# ---- 市场/行业日收益上下文（worker 进程初始化时注入，只读） ----
_V4_MKT_CTX = None      # (dates, cumsum, bisect_right)
_V4_IND_CTX = None      # {industry: (dates, cumsum, bisect_right)}


def _v4_worker_init(mkt, ind):
    global _V4_MKT_CTX, _V4_IND_CTX
    from bisect import bisect_right as _br

    def _ctx(dct):
        if not dct:
            return None
        dates = sorted(dct)
        rets = np.array([dct[x] for x in dates], float)
        c = np.cumsum(np.insert(rets, 0, 0.0))
        return (dates, c, _br)

    _V4_MKT_CTX = _ctx(mkt)
    _V4_IND_CTX = {k: _ctx(v) for k, v in (ind or {}).items() if v}


def _v4_roll5_ret(ctx, d):
    """截至日期 d（含）近5个交易日的等权累计收益；不足按实际天数；缺→0。"""
    if ctx is None:
        return 0.0
    dates, c, br = ctx
    i = br(dates, d) - 1
    if i < 0:
        return 0.0
    j0 = max(0, i - 4)
    return float((c[i + 1] - c[j0]) / (i + 1 - j0))


def _v4_factor_matrix(bars, industry):
    """T 日因子矩阵（仅用 T 及以前数据）。返回 (F[n×13], aux dict)。

    因子：L1形态上行概率/相似样本收益（逐日匹配，与 _sig_l1_pattern 同口径）、
    MA20/60 乖离、5/10/20 日收益、RSI14、ATR14/Close、20日波动、量变(5/20)、
    大盘5日收益、行业5日收益（市场日历口径，停牌日不产生缺口）。"""
    n = len(bars)
    dates = [b["date"] for b in bars]
    close = np.array([(b["close"] if b["close"] else np.nan)
                      for b in bars], float)
    high = np.array([(b["high"] if b["high"] else np.nan)
                     for b in bars], float)
    low = np.array([(b["low"] if b["low"] else np.nan)
                    for b in bars], float)
    vol = np.array([(b.get("vol") or 0.0) for b in bars], float)
    F = np.full((n, len(_V4_FACTORS)), np.nan)
    fi = {f: k for k, f in enumerate(_V4_FACTORS)}

    lr = np.zeros(n)                    # lr[t]=log(c[t]/c[t-1])，与logret同口径
    lr[1:] = np.diff(np.log(np.maximum(close, 1e-9)))
    ret1 = np.zeros(n)
    ret1[1:] = close[1:] / close[:-1] - 1.0

    ma20 = _v4_roll_mean(close, 20)
    ma60 = _v4_roll_mean(close, 60)
    F[:, fi["bias20"]] = close / np.maximum(ma20, 1e-9) - 1.0
    F[:, fi["bias60"]] = close / np.maximum(ma60, 1e-9) - 1.0
    for k, f in ((5, "ret5"), (10, "ret10"), (20, "ret20")):
        F[k:, fi[f]] = close[k:] / np.maximum(close[:-k], 1e-9) - 1.0

    d1 = np.diff(close, prepend=close[:1])
    up = _v4_roll_mean(np.where(d1 > 0, d1, 0.0), 14)
    dn = _v4_roll_mean(np.where(d1 < 0, -d1, 0.0), 14)
    rsi = 100.0 - 100.0 / (1.0 + up / np.maximum(dn, 1e-12))
    rsi[(up + dn) <= 0] = 50.0
    F[:, fi["rsi14"]] = rsi

    tr = np.zeros(n)
    tr[1:] = np.maximum(high[1:] - low[1:], np.maximum(
        np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    atr = _v4_roll_mean(tr, 14)
    F[:, fi["atr_pct"]] = atr / np.maximum(close, 1e-9)
    F[:, fi["vola20"]] = _v4_roll_std(lr, 20)
    v5 = _v4_roll_mean(vol, 5)
    v20 = _v4_roll_mean(vol, 20)
    F[:, fi["volchg"]] = v5 / np.maximum(v20, 1e-9) - 1.0

    if _V4_MKT_CTX is not None:
        F[:, fi["mkt5"]] = [_v4_roll5_ret(_V4_MKT_CTX, x) for x in dates]
    ictx = _V4_IND_CTX.get(industry) if _V4_IND_CTX else None
    if ictx is not None:
        F[:, fi["ind5"]] = [_v4_roll5_ret(ictx, x) for x in dates]
    else:
        F[:, fi["ind5"]] = 0.0

    # L1 形态两因子：逐日自身历史匹配（只用当日以前窗口，防前视）
    W = W_WINDOW
    L = lr[1:]                          # 与 logret(closes) 完全同口径
    for t in range(2 * W + 1, n - 1):
        cur = znorm(L[t - W:t])
        d_arr = _px_distances(L[:t], cur, W)
        ks = [k for k in range(len(d_arr)) if k + W <= t - W]
        if len(ks) < 6:
            continue
        ks.sort(key=lambda k: d_arr[k])
        ups = tot = sret = 0.0
        for k in ks[:CFG.TOPK]:
            j = k + W
            if j + 1 < n:
                tot += 1.0
                o = close[j + 1] / close[j] - 1.0
                sret += o
                ups += 1.0 if o > 0 else 0.0
        if tot >= 5:
            F[t, fi["l1_up"]] = ups / tot
            F[t, fi["l1_ret"]] = sret / tot

    aux = {"dates": dates, "close": close, "high": high, "low": low,
           "open": np.array([(b["open"] if b["open"] else np.nan)
                             for b in bars], float),
           "atr": atr, "ret1": ret1}
    return F, aux


def _v4_mkt_ind_ctx(ind_of):
    """市场/行业等权日收益（一遍扫描全库）：
    返回 (mkt={date: ret}, ind={date: {industry: ret}})。"""
    mkt_acc, ind_acc, prev_px = {}, {}, {}
    with db_conn() as conn:
        cur = conn.execute(
            "SELECT code, date, close FROM daily_bars ORDER BY date")
        for code, d, cl in cur:
            pv = prev_px.get(code)
            if pv and pv > 0 and cl and cl > 0:
                r = cl / pv - 1.0
                a = mkt_acc.get(d)
                if a is None:
                    mkt_acc[d] = [r, 1]
                else:
                    a[0] += r
                    a[1] += 1
                b = ind_acc.setdefault(d, {}).setdefault(
                    ind_of.get(code, ""), [0.0, 0])
                b[0] += r
                b[1] += 1
            if cl:
                prev_px[code] = cl
    mkt = {d: s / c for d, (s, c) in mkt_acc.items() if c}
    ind = {d: {k: s / c for k, (s, c) in v.items() if c}
           for d, v in ind_acc.items()}
    return mkt, ind


def _v4_walkforward_one(job):
    """多进程 worker：单股 Walk-Forward 训练+预测（顶层函数可 pickle）。"""
    code, bars, industry = job
    try:
        return _v4_wf_impl(code, bars, industry)
    except Exception:
        log.exception("v4 worker 失败 %s", code)
        return None


def _v4_wf_impl(code, bars, industry):
    from sklearn.linear_model import (Lasso, LogisticRegression,
                                      QuantileRegressor)
    try:
        from lightgbm import LGBMRegressor
        has_lgbm = True
    except ImportError:
        has_lgbm = False

    n = len(bars)
    if n < _V4_WARMUP + _V4_TRAIN_MIN + _V4_FOLD + max(_V4_HORIZONS):
        return None
    F, aux = _v4_factor_matrix(bars, industry)
    dates, close, high, low, open_, atr, ret1 = (
        aux["dates"], aux["close"], aux["high"], aux["low"], aux["open"],
        aux["atr"], aux["ret1"])
    y = {}
    for H in _V4_HORIZONS:
        a = np.full(n, np.nan)
        a[:n - H] = close[H:] / close[:n - H] - 1.0
        y[H] = a

    # 有效区间：因子全 finite 的最长连续尾段
    finite = np.all(np.isfinite(F), axis=1)
    t0 = _V4_WARMUP
    last_ok = n - 1 - max(_V4_HORIZONS)
    while t0 <= last_ok and not finite[t0]:
        t0 += 1
    t1 = last_ok
    while t1 >= t0 and not finite[t1]:
        t1 -= 1
    S = t1 - t0 + 1
    if S < _V4_TRAIN_MIN + _V4_FOLD:
        return None
    n_folds = max(1, int(S * 0.4) // _V4_FOLD)
    if S - n_folds * _V4_FOLD < _V4_TRAIN_MIN:
        n_folds -= 1
    if n_folds < 1:
        return None
    fa = S - n_folds * _V4_FOLD
    TS = S - fa

    Fc = F[t0:t1 + 1].copy()
    bad = ~np.isfinite(Fc)
    if bad.any():
        Fc[bad] = 0.0                   # 中段孤立缺失中性填充
    Yc = {H: y[H][t0:t1 + 1] for H in _V4_HORIZONS}
    base_sigs = {}
    try:
        for i, d_, typ, _rs in _composite_signals(
                bars, CFG.RISK_PARAMS["稳健"], use_chips=False):
            if i - t0 >= fa:
                base_sigs[d_] = typ
    except Exception:
        base_sigs = {}

    ml = {H: np.full(TS, np.nan) for H in _V4_HORIZONS}
    ml_dyn = np.full(TS, np.nan)        # 逐日按 h_choice 取对应H的预测
    p_up = np.full(TS, np.nan)
    adaptive = np.full(TS, np.nan)
    qpred = {q: np.full(TS, np.nan) for q in _V4_QTS}
    hch = np.zeros(TS, dtype=np.int32)
    ins_ic = []                         # LGBM 训练段内IC（过拟合检查）
    q_cross_raw = [0, 0]                # 违反序的相邻对数 / 总对数
    factor_table = None
    feat_imp = None
    per_stock_ic = {H: [] for H in _V4_HORIZONS}

    for j in range(n_folds):
        b = S - j * _V4_FOLD
        a = b - _V4_FOLD
        ta, tb = a - fa, b - fa         # 测试段输出数组内的偏移索引
        # Purge + embargo：训练标签 close[t+H] 不得跨越测试折起点 a
        maxH = max(_V4_HORIZONS)
        tr_end = max(0, a - maxH - _V4_EMBARGO)
        if tr_end < _V4_TRAIN_MIN:
            continue                    # 数据不足，该折不产出预测
        tr = np.arange(0, tr_end)
        mu = Fc[tr].mean(axis=0)
        sd = Fc[tr].std(axis=0)
        sd[sd < 1e-9] = 1.0
        Z = (Fc - mu) / sd              # 仅训练段统计量，测试行同缩放
        # 1) Horizon 选择：训练段内层 75/25 时序切分的 LGBM IC（不看测试）
        cut = int(len(tr) * 0.75)
        h_ic = {}
        for H in _V4_HORIZONS:
            # 内层训练同样要 purge，避免训练标签泄漏到内层验证集
            inner_tr_end = max(0, cut - maxH - _V4_EMBARGO)
            if not has_lgbm or inner_tr_end < 60 or len(tr) - cut < 30:
                h_ic[H] = 0.0
                continue
            try:
                m_ = LGBMRegressor(**_V4_LGBM).fit(Z[tr[:inner_tr_end]],
                                                   Yc[H][tr[:inner_tr_end]])
                h_ic[H] = _v4_rankic(m_.predict(Z[tr[cut:]]),
                                     Yc[H][tr[cut:]]) or 0.0
            except Exception:
                h_ic[H] = 0.0
        h_star = max(_V4_HORIZONS, key=lambda H: h_ic[H])
        hch[ta:tb] = h_star
        yh = Yc[h_star][tr]
        # 2) Lasso 因子筛选/权重（标准化后，训练段 only）
        las = Lasso(alpha=0.005, max_iter=5000).fit(Z[tr], yh)
        coef = np.asarray(las.coef_, float)
        ic_m = np.zeros(len(_V4_FACTORS))
        icir = np.zeros(len(_V4_FACTORS))
        segs = np.array_split(tr, 4)
        for i in range(len(_V4_FACTORS)):
            ics = [_v4_rankic(Z[s_, i], yh[s_]) for s_ in segs]
            ics = [x for x in ics if x is not None]
            if ics:
                ic_m[i] = float(np.mean(ics))
                icir[i] = ic_m[i] / max(float(np.std(ics)), 1e-6)
        sel = np.array([
            (coef[i] != 0.0) or (abs(ic_m[i]) > 0.02 and icir[i] > 0.2)
            for i in range(len(_V4_FACTORS))])
        if sel.sum() < 3:
            sel = np.ones(len(_V4_FACTORS), bool)
        # 自适应权重：Lasso 稀疏权重优先；若被全量收缩为零，
        # 退回 IC×稳定性 加权（同样只看训练段）
        if np.any(coef != 0.0):
            w_eff = coef.copy()
        else:
            w_eff = np.array([
                ic_m[i] * min(icir[i], 3.0)
                if (abs(ic_m[i]) > 0.02 and icir[i] > 0.2) else 0.0
                for i in range(len(_V4_FACTORS))])
        Xs = Z[:, sel]
        # 3) LightGBM：全训练段拟合，折内预测（各 H 都要，Horizon 实验用）
        if has_lgbm:
            for H in _V4_HORIZONS:
                try:
                    m_ = LGBMRegressor(**_V4_LGBM).fit(Xs[tr], Yc[H][tr])
                    ml[H][ta:tb] = m_.predict(Xs[a:b])
                    if H == h_star:
                        ml_dyn[ta:tb] = ml[H][ta:tb]
                        ins_ic.append(_v4_rankic(m_.predict(Xs[tr]),
                                                 yh) or 0.0)
                        if j == 0:
                            feat_imp = sorted(
                                zip([f for i_, f in enumerate(_V4_FACTORS)
                                     if sel[i_]],
                                    m_.booster_.feature_importance("gain")),
                                key=lambda x: -x[1])
                except Exception:
                    pass
            for H in _V4_HORIZONS:
                ic = _v4_rankic(ml[H][ta:tb], Yc[H][a:b])
                if ic is not None:
                    per_stock_ic[H].append(ic)
        else:
            for H in _V4_HORIZONS:
                ml[H][ta:tb] = 0.0      # 缺库占位（如实标记，不偷偷换模型）
            ml_dyn[ta:tb] = 0.0
        # 4) Logistic 方向概率
        try:
            lg = LogisticRegression(C=1.0, max_iter=500).fit(
                Xs[tr], (yh > 0))
            p_up[ta:tb] = lg.predict_proba(Xs[a:b])[:, 1]
        except Exception:
            pass
        # 5) Quantile Regression（训练段；排序消除交叉并记录原始交叉率）
        try:
            qs = []
            for q in _V4_QTS:
                qm = QuantileRegressor(quantile=q / 100.0, alpha=0.01,
                                       solver="highs").fit(Xs[tr], yh)
                qs.append(qm.predict(Xs[a:b]))
            Q = np.column_stack(qs)
            for r_ in range(Q.shape[0]):
                for c_ in range(Q.shape[1] - 1):
                    q_cross_raw[1] += 1
                    if Q[r_, c_] > Q[r_, c_ + 1] + 1e-12:
                        q_cross_raw[0] += 1
            Q = np.sort(Q, axis=1)      # 投影到非交叉（保守修复，如实记录）
            for qi, q in enumerate(_V4_QTS):
                qpred[q][ta:tb] = Q[:, qi]
        except Exception:
            pass
        adaptive[ta:tb] = (Z[a:b] @ w_eff) / max(float((Z[tr] @ w_eff).std()),
                                                 1e-9)  # 训练段σ归一
        if j == 0:
            factor_table = [{"factor": f, "weight": float(w_eff[i]),
                             "train_ic": float(ic_m[i]),
                             "stability": float(icir[i]),
                             "selected": bool(sel[i])}
                            for i, f in enumerate(_V4_FACTORS)]

    # 注：此处不再按「测试折 IC」逐股弃用 LightGBM 预测。
    # 原过拟合 guard 用同一测试折的 OOS IC 决定是否使用该折预测，属于
    # 测试集泄漏（后视选模型），且会把 train(0.6)/oos(0) 的股票几乎全部误杀。
    # 模型是否有效交由报告中的 Train/Test IC 与 "Full - LightGBM" 消融如实呈现。

    if not np.isfinite(p_up).any() and not any(
            np.isfinite(ml[H]).any() for H in _V4_HORIZONS):
        return None
    out = {
        "code": code,
        "dates": dates[t0 + fa:t0 + S],
        "close": close[t0 + fa:t0 + S].astype(np.float32),
        "open": open_[t0 + fa:t0 + S].astype(np.float32),
        "high": high[t0 + fa:t0 + S].astype(np.float32),
        "low": low[t0 + fa:t0 + S].astype(np.float32),
        "atr": atr[t0 + fa:t0 + S].astype(np.float32),
        "ret1": ret1[t0 + fa:t0 + S].astype(np.float32),
        "l1_up": F[t0 + fa:t0 + S, 0].astype(np.float32),
        "l1_ret": F[t0 + fa:t0 + S, 1].astype(np.float32),
        "y": {H: Yc[H][fa:].astype(np.float32) for H in _V4_HORIZONS},
        "ml": {H: ml[H].astype(np.float32) for H in _V4_HORIZONS},
        "ml_dyn": ml_dyn.astype(np.float32),
        "p_up": p_up.astype(np.float32),
        "adaptive": adaptive.astype(np.float32),
        "q": {str(q): qpred[q].astype(np.float32) for q in _V4_QTS},
        "h_choice": hch,
        "base_sigs": base_sigs,
        "ins_ic": float(np.mean(ins_ic)) if ins_ic else None,
        "q_cross": (q_cross_raw[0] / q_cross_raw[1]) if q_cross_raw[1] else 0.0,
        "factor_table": factor_table,
        "feat_imp": feat_imp,
        "per_stock_ic": per_stock_ic,
        "n_folds": n_folds,
        "train_last": int(t0 + fa),
    }
    return out


# ---- v4 组合级回测（统一：初始资金/成本/滑点/成交时点/涨跌停/停牌） ----

def _v4_limit_pct(code):
    """涨跌停幅度：创业板/科创板20%，主板10%（北交所不参与回测）。"""
    if code.startswith(("sz30", "sh68")):
        return 0.20
    return 0.10


def _v4_metrics(eq_curve, dates, trades, stock_days=0):
    """组合绩效：总收益/年化/最大回撤/Calmar/Sharpe/胜率/盈亏比/交易数/
    平均持仓天数/最大连续亏损/收益波动率。"""
    import datetime as _dt
    eq = np.asarray(eq_curve, float)
    n = len(eq)
    out = {"total": 0.0, "ann": 0.0, "mdd": 0.0, "calmar": None,
           "sharpe": None, "vol": 0.0, "winrate": None, "pf": None,
           "trades": len(trades), "avg_hold": None,
           "max_consec_loss": 0, "stock_days": stock_days, "days": n}
    if n < 2 or eq[0] <= 0:
        return out
    total_mult = float(eq[-1] / eq[0])
    out["total"] = total_mult - 1.0
    try:
        d0 = _dt.date.fromisoformat(dates[0])
        d1 = _dt.date.fromisoformat(dates[-1])
        years = max((d1 - d0).days / 365.25, 1e-6)
    except Exception:
        years = max(n / 252.0, 1e-6)
    out["ann"] = total_mult ** (1.0 / years) - 1.0 if total_mult > 0 else -1.0
    daily = np.diff(eq) / eq[:-1]
    sd = float(daily.std())
    out["vol"] = sd * math.sqrt(252.0) if n > 2 else 0.0
    if sd > 1e-12:
        out["sharpe"] = float(daily.mean()) / sd * math.sqrt(252.0)
    peak = eq[0]
    mdd = 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    out["mdd"] = float(mdd)
    if out["mdd"] < -1e-9:
        out["calmar"] = out["ann"] / abs(out["mdd"])
    if trades:
        wins = [t for t in trades if t["ret"] > 0]
        losses = [t for t in trades if t["ret"] <= 0]
        out["winrate"] = len(wins) / len(trades)
        gp = sum(t["pnl"] for t in wins)
        gl = -sum(t["pnl"] for t in losses)
        out["pf"] = (gp / gl) if gl > 1e-9 else (None if gp <= 0 else 99.0)
        out["avg_hold"] = float(np.mean([t["hold"] for t in trades]))
        consec = worst = 0
        for t in trades:
            consec = consec + 1 if t["ret"] <= 0 else 0
            worst = max(worst, consec)
        out["max_consec_loss"] = worst
    return out


# 消融变体（决策层开关；同一套 Walk-Forward 预测输出，不重训）
_V4_VARIANTS = {
    "Full v4": {},
    "Full - Adaptive": {"use_adaptive": False},
    "Full - Logistic": {"use_logistic": False},
    "Full - LightGBM": {"use_lgbm": False},
    "Full - Quantile": {"use_quantile": False, "use_dist_exit": False},
    "Full - DistributionExit": {"use_dist_exit": False},
    "Adaptive+Horizon": {"use_logistic": False, "use_lgbm": False},
    "Adaptive+LightGBM": {"use_logistic": False, "use_dist_exit": False},
    "Logistic+LightGBM": {"use_adaptive": False, "use_dist_exit": False},
    "LightGBM+Quantile": {"use_logistic": False, "use_adaptive": False},
    "Adaptive+Horizon+LightGBM": {"use_logistic": False,
                                  "use_dist_exit": False},
    # v4.0.1 退出结构消融（hybrid = Q10棘轮 + 移动止盈 + p_up）：
    "Exit: Dist(v4.0)": {"exit_mode": "dist", "cooldown": 0,
                         "min_hold": 0},  # 忠实复现 v4.0 纯分布退出（含旧高频）
    "Hybrid - Trailing": {"use_trailing": False},    # 去移动止盈
    "Hybrid - Q10Stop": {"use_q10_stop": False},     # 去Q10棘轮
    "Hybrid - Cooldown5": {"cooldown": 5, "min_hold": 5},  # 降频实验（全策略下有害）
    "Hybrid - T10only": {"h_only": 10, "cooldown": 5,
                         "min_hold": 5},  # 只交易 T+10+冷却：低频低回撤首选
    # 以小博大 / 交易后升档再入场（reentry_tier 已设为 full 模式默认开）：
    "Reentry-Off": {"reentry_tier": False},   # 隔离升档再入场的贡献
    "TrailSlow": {"trail_slow": True},
    "T10only+Reentry": {"h_only": 10, "cooldown": 5,
                        "min_hold": 5, "reentry_tier": True},
    "Dist+Reentry": {"exit_mode": "dist", "reentry_tier": True},
    "Dist+Reentry+Cd5": {"exit_mode": "dist", "reentry_tier": True,
                         "cooldown": 5, "min_hold": 5},
    "Hybrid (v4.0.1)": {"exit_mode": "hybrid"},   # 隔离默认 dist vs hybrid
    # Exit Ablation 头部候选（2026-09-13，激进档为主；详见
    # backtest_exit_ablation.py / research/v4_exit_ablation_all.json）：
    "Exit: Q25+Q90": {"stop_q": 25, "target_q": 90},
    "Exit: Q25+Q90+NoReentry": {"stop_q": 25, "target_q": 90,
                                "reentry_tier": False},
    "Exit: Q25+Q90+MinHold5": {"stop_q": 25, "target_q": 90, "min_hold": 5},
    "Exit: Q25+Q90+ExitP55": {"stop_q": 25, "target_q": 90, "exit_p": 0.55},
    "Exit: Q25+Q90+NoPup+Cd10": {"stop_q": 25, "target_q": 90,
                                 "use_logistic": False, "cooldown": 10},
    # regime 条件化止损（连续口径复核：平衡档有效、激进档无稳健增益）：
    "RegimeStop: Q25 mkt<-0.6%": {"weak_q": 25, "weak_mkt": -0.006},
    "RegimeStop: Q25 mkt<-1.0%": {"weak_q": 25, "weak_mkt": -0.010},
    # 板块轮动（行业5日收益动量门槛）：
    "Rot-Top30": {"rot_top": 0.70},               # 只买行业强度前30%的个股
    "Rot-Top50": {"rot_top": 0.50},
    "Rot-Strong": {"rot_strong": True},           # 只买跑赢大盘的行业
    "Rot-T10only": {"h_only": 10, "cooldown": 5, "min_hold": 5,
                    "rot_top": 0.70},             # T10王牌+板块轮动叠加
    "RotT10+DispHi": {"h_only": 10, "cooldown": 5, "min_hold": 5,
                      "rot_top": 0.70,
                      "disp_min": 0.5, "disp_max": None},  # regime：仅高分化
    # （disp_max=None 显式清掉保守档新默认的低分化闸门，否则两闸门互斥 0 交易）
    # ⚠️ 2026-09-13：disp_rank 全样本排名前视已修（改因果扩张分位），
    # 本变体原成绩作废、尚未按修正口径重评，仅保留作对照。
}


def _v4_entry_score(mats, rules):
    """与入场逻辑一致的 (score, label) pooled IC/MAE（矩阵向量化）。"""
    cal, M, codes = mats
    if rules.get("use_lgbm", True):
        x, y = M["ml_dyn"], M["y_dyn"]
    elif rules.get("use_quantile", True):
        x, y = M["q50"], M["y5"]
    else:
        return {"ic": None, "mae": None, "n": 0}
    return _v4_pool_eval(x, y)


def _v4_pool_eval(x, y, th=None):
    """矩阵 pooled IC / MAE / 方向命中率（finite 掩码）。"""
    m = np.isfinite(x) & np.isfinite(y)
    n = int(m.sum())
    out = {"ic": None, "mae": None, "hit": None, "n": n, "ic_ir": None}
    if n < 25:
        return out
    xs = x[m].astype(np.float64)
    ys = y[m].astype(np.float64)
    out["ic"] = _v4_rankic(xs, ys)
    out["mae"] = float(np.mean(np.abs(xs - ys)))
    if th is not None:
        out["hit"] = float(np.mean((xs > th) == (ys > 0)))
    return out


def _v4_stack(preds):
    """把逐股测试段堆叠为日历对齐矩阵（一次构建，指标/回测共用）。

    返回 (cal, M, codes)。M 各键为 (n_stocks × n_days) 矩阵；
    无 bar 处为 NaN。"""
    cal = sorted({d for r in preds for d in r["dates"]})
    idx = {d: k for k, d in enumerate(cal)}
    ns, nc = len(preds), len(cal)

    def mk():
        return np.full((ns, nc), np.nan, np.float32)

    keys = ("close", "open", "high", "low", "ret1", "atr", "p_up",
            "adaptive", "ml_dyn", "l1_up", "l1_ret")
    M = {k: mk() for k in keys}
    for H in _V4_HORIZONS:
        M["ml%d" % H] = mk()
        M["y%d" % H] = mk()
    for q in _V4_QTS:
        M["q%d" % q] = mk()
    M["h_choice"] = np.full((ns, nc), -1, np.int16)
    M["base_buy"] = np.zeros((ns, nc), bool)
    M["base_sell"] = np.zeros((ns, nc), bool)
    codes = []
    lim_rows = np.full(ns, 0.10, np.float64)
    for k, r in enumerate(preds):
        codes.append(r["code"])
        cols = np.array([idx[d] for d in r["dates"]], np.int64)
        for key in keys:
            M[key][k, cols] = r[key]
        for H in _V4_HORIZONS:
            M["ml%d" % H][k, cols] = r["ml"][H]
            M["y%d" % H][k, cols] = r["y"][H]
        for q in _V4_QTS:
            M["q%d" % q][k, cols] = r["q"][str(q)]
        M["h_choice"][k, cols] = r["h_choice"]
        for i, d in enumerate(r["dates"]):
            typ = r["base_sigs"].get(d)
            if typ == "BUY":
                M["base_buy"][k, idx[d]] = True
            elif typ == "SELL":
                M["base_sell"][k, idx[d]] = True
        lim_rows[k] = _v4_limit_pct(r["code"])
    # 动态 H 的标签 y_dyn
    M["y_dyn"] = mk()
    for H in _V4_HORIZONS:
        m = M["h_choice"] == H
        M["y_dyn"][m] = M["y%d" % H][m]
    # 涨跌停掩码
    with np.errstate(invalid="ignore"):
        M["limit_up"] = M["ret1"] >= (lim_rows[:, None] - 0.005)
        M["limit_dn"] = M["ret1"] <= -(lim_rows[:, None] - 0.005)
    M["has_bar"] = np.isfinite(M["close"])
    return cal, M, codes


def _v4_stack_subset(preds_sub):
    """为时间对齐子样本重建堆叠矩阵（日历轴也重建，避免旧日期稀释）。"""
    return _v4_stack(preds_sub)


def _v4_icir_rows(Mx, My, min_n=30):
    """逐行 IC 序列 → IC_IR。"""
    ics = []
    for i in range(Mx.shape[0]):
        ic = _v4_rankic(Mx[i], My[i])
        if ic is not None:
            ics.append(ic)
    return _v4_icir(ics)


def _v4_icir(ics):
    if not ics:
        return None
    m = float(np.mean(ics))
    s = float(np.std(ics))
    return m / max(s, 1e-6)


def _v4_quantile_diag_m(M):
    """Pinball / 覆盖率 / 交叉率（矩阵向量化）。"""
    y = M["y_dyn"]
    out = {}
    for q in _V4_QTS:
        pred = M["q%d" % q]
        m = np.isfinite(pred) & np.isfinite(y)
        if m.sum() < 25:
            out["pinball_%d" % q] = None
            continue
        a = y[m].astype(np.float64) - pred[m].astype(np.float64)
        loss = np.where(a > 0, a * (q / 100.0), -a * (1 - q / 100.0))
        out["pinball_%d" % q] = float(np.mean(loss))
    m10 = np.isfinite(M["q10"]) & np.isfinite(M["q90"]) & np.isfinite(y)
    out["coverage_10_90"] = (float(np.mean(
        (M["q10"][m10] <= y[m10]) & (y[m10] <= M["q90"][m10])))
        if int(m10.sum()) else None)
    out["n"] = int(m10.sum())
    return out


def _v4_factor_agg(preds):
    """跨股聚合因子表：中位 train_ic / 稳定性 / 入选率 / 中位|权重|。"""
    agg = {f: {"ic": [], "stab": [], "sel": 0, "w": [], "n": 0}
           for f in _V4_FACTORS}
    n_st = 0
    for r in preds:
        ft = r.get("factor_table")
        if not ft:
            continue
        n_st += 1
        for row in ft:
            a = agg[row["factor"]]
            a["n"] += 1
            a["ic"].append(row["train_ic"])
            a["stab"].append(row["stability"])
            if row["selected"]:
                a["sel"] += 1
                a["w"].append(abs(row["weight"]))
    out = {}
    for f, a in agg.items():
        if not a["n"]:
            continue
        out[f] = {
            "train_ic_med": float(np.median(a["ic"])),
            "stability_med": float(np.median(a["stab"])),
            "sel_pct": a["sel"] / a["n"],
            "weight_med": float(np.median(a["w"])) if a["w"] else 0.0,
        }
    out["_n_stocks"] = n_st
    return out


def _v4_entry_mask(M, rules, tier):
    """入场资格矩阵（向量化；NaN 比较为 False）。"""
    mode = rules.get("mode", "full")
    with np.errstate(invalid="ignore"):
        if mode == "baseline":
            ok = M["base_buy"].copy()
        else:
            ok = M["has_bar"].copy()
            if rules.get("use_logistic", True):
                ok &= (M["p_up"] >= tier["p_th"])
            if rules.get("use_lgbm", True):
                ok &= (M["ml_dyn"] >= tier["r_th"])
                if rules.get("use_quantile", True) \
                        and rules.get("q50_entry", True):
                    ok &= (M["q50"] > 0)
            elif rules.get("use_quantile", True):
                ok &= (M["q50"] >= tier["r_th"])
            if rules.get("use_adaptive", True):
                ok &= (M["adaptive"] >= tier["a_th"])
            h_only = rules.get("h_only")        # 只交易指定 Horizon（如 T+10）
            if h_only:
                ok &= (M["h_choice"] == int(h_only))
            rot_top = rules.get("rot_top")      # 板块轮动：行业5日收益排名前 N%
            if rot_top and "ind_rank5" in M:
                with np.errstate(invalid="ignore"):
                    ok &= (M["ind_rank5"] >= float(rot_top))
            if rules.get("rot_strong") and "ind5" in M and "mkt5" in M:
                with np.errstate(invalid="ignore"):
                    ok &= (M["ind5"] > M["mkt5"])   # 行业跑赢大盘
            disp_min = rules.get("disp_min")    # regime：离散度分位门槛
            if disp_min and "disp_rank" in M:
                with np.errstate(invalid="ignore"):
                    ok &= (M["disp_rank"] >= float(disp_min))
            disp_max = rules.get("disp_max")    # regime：只在高/低分化环境交易
            if disp_max is not None and "disp_rank" in M:
                with np.errstate(invalid="ignore"):
                    ok &= (M["disp_rank"] <= float(disp_max))
        ok &= ~M["limit_up"]
    return ok & M["has_bar"]


def _v4_entry_ok_cell(M, ks, t, tier, rules):
    """单格入场判定（与 _v4_entry_mask 同逻辑，供平仓后升档再入场用）。"""
    if not M["has_bar"][ks, t] or M["limit_up"][ks, t]:
        return False
    if rules.get("use_logistic", True) \
            and not (M["p_up"][ks, t] >= tier["p_th"]):
        return False
    if rules.get("use_lgbm", True):
        if not (M["ml_dyn"][ks, t] >= tier["r_th"]):
            return False
        if rules.get("use_quantile", True) and rules.get("q50_entry", True) \
                and not (M["q50"][ks, t] > 0):
            return False
    elif rules.get("use_quantile", True) \
            and not (M["q50"][ks, t] >= tier["r_th"]):
        return False
    if rules.get("use_adaptive", True) \
            and not (M["adaptive"][ks, t] >= tier["a_th"]):
        return False
    h_only = rules.get("h_only")
    if h_only and M["h_choice"][ks, t] != int(h_only):
        return False
    rot_top = rules.get("rot_top")
    if rot_top and "ind_rank5" in M:
        v = M["ind_rank5"][ks, t]
        if not np.isfinite(v) or v < float(rot_top):
            return False
    if rules.get("rot_strong") and "ind5" in M and "mkt5" in M:
        if not (M["ind5"][ks, t] > M["mkt5"][ks, t]):
            return False
    disp_min = rules.get("disp_min")
    if disp_min and "disp_rank" in M:
        v = M["disp_rank"][ks, t]
        if not np.isfinite(v) or v < float(disp_min):
            return False
    disp_max = rules.get("disp_max")
    if disp_max is not None and "disp_rank" in M:
        v = M["disp_rank"][ks, t]
        if not np.isfinite(v) or v > float(disp_max):
            return False
    return True


def _v4_attach_rotation(M, cal, codes_s, ind_of, mkt, ind):
    """往堆叠矩阵注入板块轮动上下文：
    ind_rank5（行业5日收益当日横截面百分位 0~1）、ind5、mkt5。"""
    ind_names = [ind_of.get(c, "") for c in codes_s]
    uniq = sorted({x for x in ind_names if x})
    if not uniq:
        return
    jdx = {x: j for j, x in enumerate(uniq)}
    didx = {d: k for k, d in enumerate(cal)}
    IR = np.zeros((len(cal), len(uniq)))
    CNT = np.zeros((len(cal), len(uniq)))
    for d, dmap in ind.items():
        k = didx.get(d)
        if k is None:
            continue
        for x, r in dmap.items():
            j = jdx.get(x)
            if j is not None:
                IR[k, j] = r
                CNT[k, j] = 1.0
    c = np.cumsum(IR, 0)
    cc = np.cumsum(CNT, 0)
    S5 = c.copy()
    S5[5:] -= c[:-5]
    N5 = cc.copy()
    N5[5:] -= cc[:-5]
    R5 = np.where(N5 > 0, S5 / np.maximum(N5, 1.0), np.nan)
    marr = np.array([mkt.get(d, np.nan) for d in cal], float)
    m0 = np.where(np.isfinite(marr), marr, 0.0)
    cm = np.cumsum(m0)
    MS5 = cm.copy()
    MS5[5:] -= cm[:-5]
    mn = np.isfinite(marr).astype(float)
    ccn = np.cumsum(mn)
    NN5 = ccn.copy()
    NN5[5:] -= ccn[:-5]
    MR5 = np.where(NN5 > 0, MS5 / np.maximum(NN5, 1.0), np.nan)
    RANK = np.full(R5.shape, np.nan)
    for k in range(R5.shape[0]):
        row = R5[k]
        m = np.isfinite(row)
        n = int(m.sum())
        if n >= 3:
            v = row[m]
            RANK[k, m] = v.argsort().argsort() / max(n - 1, 1)
    # 行业动量离散度（轮动富集度）：行业5日收益横截面 std。
    # 分位必须因果：只与「截至当日」的扩张窗口历史比较（至少 60 个有效日）。
    # 原全样本 argsort 排名会看到未来，使 disp_min/disp_max/weak_disp 类
    # regime 闸门产生前视高估（2026-09-13 修正）。
    DISP = np.array([np.nanstd(R5[k]) if np.isfinite(R5[k]).sum() >= 3
                     else np.nan for k in range(R5.shape[0])])
    DRANK = np.full(len(DISP), np.nan)
    _hist = []
    _MIN_HIST = 60
    for _k in range(len(DISP)):
        _v = DISP[_k]
        if not np.isfinite(_v):
            continue
        _hist.append(_v)
        _n = len(_hist)
        if _n >= _MIN_HIST:
            _a = np.asarray(_hist)
            DRANK[_k] = ((_a < _v).sum() + 0.5 * (_a == _v).sum()) / _n
    ns, nc = M["close"].shape
    M["ind_rank5"] = np.full((ns, nc), np.nan)
    M["ind5"] = np.full((ns, nc), np.nan)
    M["mkt5"] = np.broadcast_to(MR5[None, :], (ns, nc)).copy()
    M["disp_rank"] = np.broadcast_to(DRANK[None, :], (ns, nc)).copy()
    for k, x in enumerate(ind_names):
        j = jdx.get(x)
        if j is not None:
            M["ind_rank5"][k] = RANK[:, j]
            M["ind5"][k] = R5[:, j]


def _v4_morning_view(M):
    """早盘视图：决策/模型列整体后移一日。

    T 日早盘只能看到 T-1 收盘及以前的因子与预测，故 p_up/q*/ml_dyn/adaptive/
    base_buy/base_sell/atr/regime 等决策列取 T-1 值；价格、涨跌停、停牌等
    执行日字段保持当日不变，供 T 日收盘执行。"""
    keys = ["p_up", "adaptive", "ml_dyn", "l1_up", "l1_ret",
            "base_buy", "base_sell", "atr", "h_choice",
            "ind_rank5", "ind5", "mkt5", "disp_rank"]
    keys += ["q%d" % q for q in _V4_QTS]
    out = dict(M)
    for k in keys:
        A = M.get(k)
        if A is None:
            continue
        if A.dtype == bool:
            B = np.zeros_like(A)
        elif np.issubdtype(A.dtype, np.integer):
            B = np.full_like(A, -1)
        else:
            B = np.full_like(A, np.nan)
        B[:, 1:] = A[:, :-1]
        out[k] = B
    return out


# 退市/长期停牌持仓：连续 N 根无 bar 后按最后已知收盘价了结
_V4_STALE_BARS = 20


def _v4_portfolio_sim(mats, tier, rules, initial=_V4_CAPITAL):
    """组合级事件回测（矩阵版，唯一实现）。

    早盘信号：因子/预测只用 T-1 收盘信息（_v4_morning_view），T 日收盘执行；
    止损单在前一日收盘后设定（T-1 的 q/ATR），T 日盘中触发才是可执行的挂单。
    - 买入=收盘×(1+滑点)(1+佣金)；卖出=×(1-滑点)(1-佣金-印花税)
      （v4.0.1 起佣金/印花税记 0，仅保留滑点）
    - 涨停禁买、跌停顺延；停牌持仓顺延；期末强平；退市/长停超 _V4_STALE_BARS
      根按最后收盘价了结
    - dist 分布退出（默认）：Q10棘轮止损（只收紧）+ Q75目标 + p_up 信号退出
    - hybrid 混合退出（消融对照）：Q10棘轮 + 移动止盈棘轮 + p_up（修正 bug 后实证劣于 dist）
    - 对照退出：ATR止损/移动止盈（v3.3 稳健参数，highest 逐日更新）
    - baseline：v3.3 多维评分信号进出
    """
    cal, M, codes = mats
    M = _v4_morning_view(M)             # 早盘信号：决策列只到 T-1
    # 成交价口径：close=信号次日收盘（默认）；open=信号次日开盘
    exec_open = (rules.get("exec") or _exec_mode()) == "open"
    rp = CFG.RISK_PARAMS["稳健"]
    mode = rules.get("mode", "full")
    use_dist = rules.get("use_dist_exit", True) \
        and rules.get("use_quantile", True)
    # 默认 dist（v4.0 纯分布退出）：修正 highest 逐日更新 bug 后的全量实证
    # 显示 1.02/0.94 移动止盈在组合级是最大拖累（-13pp vs dist），dist 全场最优
    exit_mode = rules.get("exit_mode", "dist")
    # 降频开关（默认关闭！300只实测：冷却5根反而把年化 +10.6%→-3.1%——
    # 退出后信号仍有效时快速再入场是收益来源之一，勿硬压频率）。
    # cooldown=平仓后同股再入场冷却根数；min_hold=p_up 信号退出前最少持仓根数
    # （止损/移动止盈不受 min_hold 限制）。baseline 信号自带冷却，不重复加。
    cd = 0 if mode == "baseline" else int(rules.get("cooldown", 0))
    mh = 0 if mode == "baseline" else int(rules.get("min_hold", 0))
    cool = {}                           # code_idx -> 最后一卖出的 t
    # 以小博大选项：stop_q=止损参考分位(10/25, 越小越宽/越大越紧)；
    # trail_slow=移动止盈用激进参数(触发1.05/回落10%, 让盈利跑更久)；
    # reentry_tier=平仓后 reentry_bars 根内按更高一档阈值再入场（质量门槛替代时间门槛）
    _sq = int(rules.get("stop_q", 10))
    qstop = M["q%d" % _sq] if _sq in _V4_QTS else M["q10"]
    trp = CFG.RISK_PARAMS["激进"] if rules.get("trail_slow") else rp
    # 退出参数覆盖（Exit Ablation 用；未指定时保持原行为不变）
    trail_trigger = float(rules.get("trail_trigger", trp["trail_trigger"]))
    trail_ratio = float(rules.get("trail_ratio", trp["trail_ratio"]))
    atr_mult = float(rules.get("atr_mult", rp["atr_mult"]))
    exit_p = float(rules.get("exit_p", tier["exit_p"]))
    _tq = int(rules.get("target_q", 75))
    qkey = "q%d" % _tq if _tq in _V4_QTS else "q75"
    # ---- regime 条件化止损（默认全关，不影响原有行为）----
    # weak_q: 弱市时改用更紧的止损分位（如 25/50）
    # weak_mkt: 弱市判定一：M["mkt5"] < weak_mkt（大盘5日累计收益阈值）
    # weak_disp: 弱市判定二：M["disp_rank"] >= weak_disp（行业分化度分位）
    # weak_exit_p: 弱市时提高 p_up 退出阈值（更易退出）
    weak_q = int(rules.get("weak_q", 0) or 0)
    weak_qstop = M["q%d" % weak_q] if (weak_q and weak_q in _V4_QTS) \
        else None
    weak_mkt = rules.get("weak_mkt")
    weak_disp = rules.get("weak_disp")
    weak_exit_p = rules.get("weak_exit_p")
    weak_mask = None
    if weak_qstop is not None or weak_exit_p is not None:
        weak_mask = np.zeros(M["has_bar"].shape, dtype=bool)
        if weak_mkt is not None and "mkt5" in M:
            weak_mask |= (M["mkt5"] < float(weak_mkt))
        if weak_disp is not None and "disp_rank" in M:
            weak_mask |= (M["disp_rank"] >= float(weak_disp))
    _strict = _V4_TIERS.get({"平衡": "保守", "激进": "平衡"}
                            .get(rules.get("tier_name", "")))
    re_bars = int(rules.get("reentry_bars", 8) or 8)
    cost = _V4_COST
    buy_mult = (1 + cost["slip"]) * (1 + cost["commission"])
    sell_mult = (1 - cost["slip"]) * (1 - cost["commission"]
                                      - cost["stamp"])
    ns, nc = M["close"].shape
    ok_entry = _v4_entry_mask(M, rules, tier)
    cash = initial
    pos = {}
    last_px = np.zeros(ns, np.float64)
    eq_curve = []
    trades = []
    for t in range(nc):
        col = M["close"][:, t]
        upd = np.isfinite(col)
        last_px[upd] = col[upd].astype(np.float64)
        # ---- 退出 ----
        for ks in sorted(pos):
            if not M["has_bar"][ks, t]:
                p = pos[ks]
                p["miss"] = p.get("miss", 0) + 1
                if p["miss"] >= _V4_STALE_BARS:
                    # 退市/长期停牌：按最后已知收盘价了结（不再等复牌）
                    px = last_px[ks] if last_px[ks] > 0 else p["entry"]
                    net = px * sell_mult
                    cash += p["shares"] * net
                    trades.append({"code": codes[ks],
                                   "ret": net / p["buy_net"] - 1.0,
                                   "pnl": p["shares"] * (net - p["buy_net"]),
                                   "hold": t - p["t_in"]})
                    del pos[ks]
                continue                # 停牌顺延
            p = pos[ks]
            px_c = float(col[ks])
            px_o = float(M["open"][ks, t])
            hi = float(M["high"][ks, t])
            lo = float(M["low"][ks, t])
            # 用昨日最高价判定今日止损，收盘后再更新 highest，
            # 避免“同日先看 high 再看 low”的日内顺序前视
            prev_high = p["highest"]
            sold = False
            if not M["limit_dn"][ks, t]:
                if mode == "baseline" or p["atr_fallback"] or not use_dist:
                    atr_t = float(M["atr"][ks, t])
                    if prev_high > p["entry"] * trail_trigger:
                        stop = prev_high * trail_ratio
                    else:
                        stop = p["entry"] - atr_mult * max(atr_t, 1e-9)
                    if lo <= stop:
                        px = px_o if px_o <= stop else min(stop, hi)
                        sold = True
                    elif mode == "baseline" and M["base_sell"][ks, t]:
                        px = px_o if exec_open else px_c
                        sold = True
                else:
                    # Q 棘轮止损（只收紧不放宽；弱市可切换更紧分位）
                    qsel, ep = qstop, exit_p
                    if weak_mask is not None and weak_mask[ks, t]:
                        if weak_qstop is not None:
                            qsel = weak_qstop
                        if weak_exit_p is not None:
                            ep = float(weak_exit_p)
                    if rules.get("use_q10_stop", True):
                        qs = qsel[ks, t]
                        if np.isfinite(qs):
                            p["stop"] = max(p["stop"], p["entry"]
                                            * (1.0 + float(qs)))
                    # 移动止盈棘轮：浮盈触发后随最高价上移（拉长持仓）
                    if exit_mode == "hybrid" \
                            and rules.get("use_trailing", True) \
                            and prev_high > p["entry"] * trail_trigger:
                        p["stop"] = max(p["stop"],
                                        prev_high * trail_ratio)
                    if lo <= p["stop"]:
                        px = px_o if px_o <= p["stop"] else min(p["stop"], hi)
                        sold = True
                    elif exit_mode == "dist" and hi >= p["target"]:
                        px = px_o if px_o >= p["target"] else p["target"]
                        sold = True
                    elif t - p["t_in"] >= mh \
                            and rules.get("use_logistic", True) \
                            and np.isfinite(M["p_up"][ks, t]) \
                            and float(M["p_up"][ks, t]) < ep:
                        px = px_o if exec_open else px_c
                        sold = True
            if sold:
                cool[ks] = t
                net = px * sell_mult
                cash += p["shares"] * net
                trades.append({"code": codes[ks],
                               "ret": net / p["buy_net"] - 1.0,
                               "pnl": p["shares"] * (net - p["buy_net"]),
                               "hold": t - p["t_in"]})
                del pos[ks]
            else:
                p["highest"] = max(prev_high, hi)   # 收盘后更新最高价
        # ---- 入场 ----
        if len(pos) < tier["max_pos"]:
            cand = np.nonzero(ok_entry[:, t])[0]
            # 名额不足时按信号强度排序，而非数据库行序（保证可复现）
            if len(cand) > 1:
                if rules.get("use_lgbm", True):
                    sc = M["ml_dyn"][cand, t]
                elif rules.get("use_quantile", True):
                    sc = M["q50"][cand, t]
                else:
                    sc = M["p_up"][cand, t]
                sc = np.where(np.isfinite(sc), sc, -np.inf)
                cand = cand[np.argsort(-sc, kind="stable")]
            for ks in cand:
                if len(pos) >= tier["max_pos"]:
                    break
                if ks in pos:
                    continue
                if t - cool.get(ks, -10**9) < cd:
                    continue             # 平仓冷却，防同股频繁进出
                if mode == "full" \
                        and rules.get("reentry_tier", True) \
                        and _strict is not None \
                        and t - cool.get(ks, -10**9) < re_bars \
                        and not _v4_entry_ok_cell(M, ks, t, _strict, rules):
                    continue             # 交易后再入场：按更高一档信号要求
                px_c = float(M["close"][ks, t])
                # 开盘成交口径：信号次日开盘价买入（open 缺失时退回收盘）
                px_fill = (float(M["open"][ks, t]) if exec_open else px_c)
                if not (px_fill and px_fill > 0):
                    px_fill = px_c
                buy_net = px_fill * buy_mult
                eq0 = cash + sum(pp["shares"] * last_px[k2]
                                 for k2, pp in pos.items())
                shares = int(eq0 * tier["frac"] / buy_net / 100.0) * 100
                if shares <= 0 or shares * buy_net > cash:
                    continue
                cash -= shares * buy_net
                p = {"t_in": t, "shares": shares, "buy_net": buy_net,
                     "entry": px_fill,
                     "highest": px_fill}   # 成交时点之前的盘中高点不计入
                _qset = qstop
                if weak_qstop is not None and weak_mask is not None \
                        and weak_mask[ks, t]:
                    _qset = weak_qstop
                if use_dist and np.isfinite(_qset[ks, t]) \
                        and np.isfinite(M[qkey][ks, t]):
                    if rules.get("use_q10_stop", True):
                        p["stop"] = p["entry"] * (1.0 + float(_qset[ks, t]))
                    else:
                        p["stop"] = 0.0     # 无初始止损，随棘轮/移动止盈上移
                    p["target"] = p["entry"] * (1.0 + float(M[qkey][ks, t]))
                    p["atr_fallback"] = False
                else:
                    p["stop"] = None
                    p["target"] = None
                    p["atr_fallback"] = True
                pos[ks] = p
        # ---- 收盘权益 ----
        eq = cash + sum(pp["shares"] * last_px[k2]
                        for k2, pp in pos.items())
        eq_curve.append(eq)
    # 期末强平
    n_forced = 0
    for ks in sorted(pos):
        pp = pos[ks]
        px = last_px[ks] if last_px[ks] > 0 else pp["entry"]
        net = px * sell_mult
        cash += pp["shares"] * net
        trades.append({"code": codes[ks], "ret": net / pp["buy_net"] - 1.0,
                       "pnl": pp["shares"] * (net - pp["buy_net"]),
                       "hold": nc - 1 - pp["t_in"]})
        n_forced += 1
    stock_days = int(M["has_bar"].sum())
    m = _v4_metrics(eq_curve, cal, trades, stock_days=stock_days)
    m["forced_closes"] = n_forced
    m["equity"] = eq_curve              # 权益曲线（报告层 _strip 会剔除）
    m["dates"] = cal
    return m


def run_v4_research(min_bars=400, limit=0, progress=None):
    """v4.0 全A研究：Walk-Forward 自适应ML + 三档风险回测 + 消融。

    返回 report dict（同时落盘 research/v4_report.json 与
    research/v4_factors.json）。"""
    from concurrent.futures import ProcessPoolExecutor
    p = progress or (lambda s: None)
    deps = _v4_deps()
    if np is None or deps.get("numpy") is None:
        raise RuntimeError("v4.0 需要 numpy：pip install numpy")
    if deps.get("sklearn") is None:
        raise RuntimeError("v4.0 需要 scikit-learn：pip install scikit-learn")
    p("v4.0：加载股票池与市场/行业上下文 ...")
    with db_conn() as conn:
        codes = [r[0] for r in conn.execute(
            "SELECT code FROM daily_bars GROUP BY code "
            "HAVING COUNT(*) >= ? AND (code LIKE 'sh60%' OR code LIKE 'sh68%'"
            " OR code LIKE 'sz00%' OR code LIKE 'sz30%')",
            (min_bars,)).fetchall()]
        ind_of = {c: (i or "") for c, i in
                  conn.execute("SELECT code, industry FROM stocks")}
    if limit:
        codes = codes[:limit]
    # ---- 预测缓存：Walk-Forward 结果与退出规则/成本无关，命中则跳过重训练 ----
    # sig 绑定 股票池+min_bars+数据指纹+因子版本，任一变化自动失效；
    # V4_NO_CACHE=1 强制重算
    _cache = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "research", "v4_preds.pkl")
    _sig = None
    preds = None
    if os.environ.get("V4_NO_CACHE") != "1":
        try:
            with db_conn() as _c:
                _dmax = _c.execute(
                    "SELECT MAX(date) FROM daily_bars").fetchone()[0] or ""
                _nbars = _c.execute(
                    "SELECT COUNT(*) FROM daily_bars").fetchone()[0]
            _sig = {"codes": codes, "min_bars": min_bars,
                    "dmax": _dmax, "nbars": _nbars, "ver": _V4_CACHE_VER}
            if os.path.exists(_cache):
                import pickle as _pk
                with open(_cache, "rb") as f:
                    _blob = _pk.load(f)
                if _blob.get("sig") == _sig and _blob.get("preds"):
                    preds = _blob["preds"]
                    p("v4.0：命中预测缓存 research/v4_preds.pkl（%d 只，"
                      "跳过重训练）" % len(preds))
        except Exception:
            log.exception("v4 预测缓存读取失败（忽略，重新计算）")
            preds = None
    # 市场/行业等权日收益（一遍扫描；无论命中缓存与否都要——板块轮动门槛用）
    mkt, ind = _v4_mkt_ind_ctx(ind_of)
    while preds is None:            # 未命中缓存：完整重算（最多执行一次）
        # ---- 分块多进程 Walk-Forward ----
        preds = []
        n_done = 0
        CH = 120
        workers = max(1, min(8, os.cpu_count() or 2))
        p(f"v4.0：Walk-Forward 全A计算（{len(codes)}只 × {len(_V4_HORIZONS)}周期, "
          f"{workers}进程）...")
        with ProcessPoolExecutor(max_workers=workers,
                                 initializer=_v4_worker_init,
                                 initargs=(mkt, ind)) as ex:
            for ci in range(0, len(codes), CH):
                chunk = codes[ci:ci + CH]
                bars_map = {}
                with db_conn() as conn:
                    ph = ",".join("?" for _ in chunk)
                    rws = conn.execute(
                        f"SELECT code,date,open,high,low,close,vol FROM ("
                        f" SELECT *, ROW_NUMBER() OVER (PARTITION BY code "
                        f" ORDER BY date DESC) rn FROM daily_bars "
                        f" WHERE code IN ({ph})"
                        f") WHERE rn<=1000 ORDER BY code, date", chunk).fetchall()
                for c, d, o, h, l, cl, v in rws:
                    bars_map.setdefault(c, []).append(
                        {"date": d, "open": o, "high": h, "low": l,
                         "close": cl, "vol": v or 0.0})
                jobs = [(c, b, ind_of.get(c, ""))
                        for c, b in bars_map.items() if len(b) >= min_bars]
                for res in ex.map(_v4_walkforward_one, jobs):
                    if res:
                        preds.append(res)
                        n_done += 1
                p(f"v4.0 进度 {min(ci + CH, len(codes))}/{len(codes)}"
                  f"（有效 {n_done}）")
        if not preds:
            raise RuntimeError("v4.0：无有效股票（缓存不足或依赖缺失）")
        try:
            os.makedirs(os.path.dirname(_cache), exist_ok=True)
            import pickle as _pk
            with open(_cache, "wb") as f:
                _pk.dump({"sig": _sig, "preds": preds}, f, protocol=4)
            p("v4.0：预测已缓存 research/v4_preds.pkl（同股票池/数据下规则迭代秒级重跑）")
        except Exception:
            log.exception("v4 预测缓存落盘失败（忽略）")
        break

    # ---- 堆叠矩阵（一次构建，全部指标/回测共用） ----
    p("v4.0：汇总 Horizon / 模型 / 分位数 指标 ...")
    mats = _v4_stack(preds)
    cal, M, codes_s = mats
    _v4_attach_rotation(M, cal, codes_s, ind_of, mkt, ind)

    # Horizon 实验（矩阵向量化 + 逐股IC序列）
    horizon = {}
    for H in _V4_HORIZONS:
        e = _v4_pool_eval(M["ml%d" % H], M["y%d" % H])
        e["ic_ir"] = _v4_icir_rows(M["ml%d" % H], M["y%d" % H])
        horizon[str(H)] = e
    hh = M["h_choice"][M["h_choice"] > 0]
    cnt = np.bincount(hh.astype(np.int64), minlength=11)
    horizon["h_choice_dist"] = {str(H): int(cnt[H]) for H in _V4_HORIZONS}

    # 模型比较（vs 当日所选 Horizon 标签）
    models = {
        "ml": _v4_pool_eval(M["ml_dyn"], M["y_dyn"], th=0.0),
        "q50": _v4_pool_eval(M["q50"], M["y_dyn"], th=0.0),
        "adaptive": _v4_pool_eval(M["adaptive"], M["y_dyn"], th=0.0),
        "p_up": _v4_pool_eval(M["p_up"], M["y_dyn"], th=0.5),
        "l1_up": _v4_pool_eval(M["l1_up"], M["y_dyn"], th=0.5),
        "l1_ret": _v4_pool_eval(M["l1_ret"], M["y_dyn"], th=0.0),
    }
    models["p_up"]["mae"] = None           # 概率模型 MAE 无意义
    models["l1_up"]["mae"] = None
    # v3.3 多维评分信号状态 pooled IC（事件日，vs T+1 收益）
    mev = M["base_buy"] | M["base_sell"]
    if int(mev.sum()) >= 25:
        xs_e = np.where(M["base_buy"][mev], 1.0, -1.0)
        models["baseline_composite"] = _v4_pool_eval(
            xs_e.astype(np.float32), M["y1"][mev], th=0.0)
    else:
        models["baseline_composite"] = {"ic": None, "mae": None,
                                        "hit": None, "n": int(mev.sum()),
                                        "ic_ir": None}
    models["baseline_composite"]["mae"] = None

    # Quantile 诊断 / 过拟合检查
    qdiag = _v4_quantile_diag_m(M)
    cross = [r["q_cross"] for r in preds if r.get("q_cross") is not None]
    qdiag["crossing_raw_med"] = float(np.median(cross)) if cross else None
    ins = [r["ins_ic"] for r in preds if r.get("ins_ic") is not None]
    overfit = {
        "lgbm_train_ic_med": float(np.median(ins)) if ins else None,
        "lgbm_test_ic": models["ml"]["ic"],
        "flag": None, "note": "Train IC 显著高于 Test IC → 过拟合标记",
    }
    if overfit["lgbm_train_ic_med"] is not None \
            and models["ml"]["ic"] is not None:
        overfit["flag"] = bool(
            overfit["lgbm_train_ic_med"] - models["ml"]["ic"] > 0.15)
    feat_imp_top = {}
    for r in preds:
        if r.get("feat_imp"):
            for f, g in r["feat_imp"]:
                feat_imp_top[f] = feat_imp_top.get(f, 0.0) + float(g)
    feat_imp_top = dict(sorted(feat_imp_top.items(), key=lambda x: -x[1]))

    # 因子聚合
    factors = _v4_factor_agg(preds)

    # ---- 组合回测：Baseline / Adaptive / Full 三档 + 消融 ----
    # 生存者偏差修复：不再按"测试段结束日距最新日 ≤45 天"剔除退市股，
    # 改为统一近端窗口（252 个交易日），窗口内仍存续的股票全部纳入；
    # 窗口内退市/长停的持仓由 _V4_STALE_BARS 规则按最后收盘价了结。
    all_cal_bt = sorted({d for r in preds for d in r["dates"]})
    bt_start = all_cal_bt[-252] if len(all_cal_bt) > 252 else all_cal_bt[0]
    rows_bt = [k for k, r in enumerate(preds)
               if r["dates"] and r["dates"][-1] >= bt_start]
    mats_bt = _v4_stack_subset([preds[k] for k in rows_bt])
    _v4_attach_rotation(mats_bt[1], mats_bt[0], mats_bt[2], ind_of, mkt, ind)
    _j0 = next((i for i, d in enumerate(mats_bt[0]) if d >= bt_start), 0)
    if _j0:
        mats_bt = (mats_bt[0][_j0:],
                   {k: v[:, _j0:] for k, v in mats_bt[1].items()},
                   mats_bt[2])
    p("v4.0：组合级回测（三档风险 × 策略 × 消融，"
      f"{len(rows_bt)}/{len(preds)} 只，窗口 {mats_bt[0][0]}~{mats_bt[0][-1]}）...")
    sims = {}
    tier_of_mode = {"保守": "保守", "稳健": "平衡", "激进": "激进"}
    for mode in ("保守", "稳健", "激进"):
        rules = {"mode": "baseline", "use_logistic": False,
                 "use_adaptive": False, "use_lgbm": False,
                 "use_quantile": False, "use_dist_exit": False}
        sims["baseline:" + mode] = _v4_portfolio_sim(
            mats_bt, _V4_TIERS[tier_of_mode[mode]], rules)
    for tn in ("保守", "平衡", "激进"):
        rules = {"mode": "adaptive", "use_logistic": False,
                 "use_lgbm": False, "use_quantile": False,
                 "use_dist_exit": False}
        sims["adaptive:" + tn] = _v4_portfolio_sim(
            mats_bt, _V4_TIERS[tn], rules)
        full_rules = {"mode": "full", "tier_name": tn}
        full_rules.update(_V4_TIER_EXTRA.get(tn, {}))
        sims["full:" + tn] = _v4_portfolio_sim(
            mats_bt, _V4_TIERS[tn], full_rules)
    for vname, vr in _V4_VARIANTS.items():
        for tn in ("保守", "平衡", "激进"):
            rules = {"mode": "full", "tier_name": tn}
            rules.update(_V4_TIER_EXTRA.get(tn, {}))
            rules.update(vr)
            sims["abl:%s:%s" % (vname, tn)] = _v4_portfolio_sim(
                mats_bt, _V4_TIERS[tn], rules)

    def _strip(m):
        return {k: v for k, v in m.items()
                if k not in ("equity", "dates", "trade_list")}

    strategies = {k: _strip(v) for k, v in sims.items()
                  if not k.startswith("abl:")}
    ablation = {}
    for vname, vr in _V4_VARIANTS.items():
        rules = {"mode": "full"}
        rules.update(vr)
        ic_m = _v4_entry_score(mats, rules)
        ablation[vname] = {
            "tier": {tn: _strip(sims["abl:%s:%s" % (vname, tn)])
                     for tn in ("保守", "平衡", "激进")},
            "entry_score_ic": ic_m["ic"],
            "entry_score_mae": ic_m["mae"],
        }

    # ---- 元信息 / 泄漏检查 ----
    all_dates = [d for r in preds for d in (r["dates"][0], r["dates"][-1])]
    spans = [len(r["dates"]) for r in preds]
    report = {
        "meta": {
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "deps": deps,
            "n_codes_pool": len(codes),
            "n_valid": len(preds),
            "n_valid_bt": len(rows_bt),
            "bt_align_note": "组合回测使用统一近端窗口（最后252个交易日），"
                             "窗口内退市/长停持仓按最后收盘价了结",
            "min_bars": min_bars,
            "date_min": min(all_dates),
            "date_max": max(all_dates),
            "span_med": int(np.median(spans)),
            "fold": _V4_FOLD,
            "warmup": _V4_WARMUP,
            "train_min": _V4_TRAIN_MIN,
            "cost": _V4_COST,
            "capital": _V4_CAPITAL,
            "lgbm_params": _V4_LGBM,
            "note_data": "回测范围为本地缓存可用数据；"
                         "见 n_codes_pool/n_valid。",
        },
        "horizon": horizon,
        "models": models,
        "quantile": qdiag,
        "overfit": overfit,
        "feat_imp_top": feat_imp_top,
        "factors": factors,
        "strategies": strategies,
        "ablation": ablation,
        "leakage_check": [
            "特征仅用 T 日及以前数据（形态匹配样本窗口结束于 T-W 之前）",
            "标签 Close[T+H]/Close[T]-1 仅作训练目标，不进特征",
            "StandardScaler/Lasso筛选/Horizon选择均只在训练段完成",
            "Walk-Forward 扩展窗：每折仅用折前数据训练，折间不重叠",
            "LightGBM 超参先验固定，未用 Test 调参",
            "三档风险为同一模型输出上的决策层参数，未分别训练",
            "组合回测为早盘信号：p_up/q*/ml_dyn/ATR 等决策列取 T-1，"
            "T 日收盘执行；止损由前一日设定、T 日盘中触发",
            "组合回测统一初始资金/手续费/滑点/成交时点/涨跌停/停牌规则",
            "组合回测使用统一近端窗口，窗口内退市/长停持仓按最后收盘价了结",
        ],
    }

    # ---- 落盘 ----
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "research")
    try:
        os.makedirs(out_dir, exist_ok=True)

        def _jb(o):
            if isinstance(o, dict):
                return {str(k): _jb(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [_jb(v) for v in o]
            if isinstance(o, (np.floating, np.integer)):
                return o.item()
            if isinstance(o, float) and not np.isfinite(o):
                return None
            if isinstance(o, np.bool_):
                return bool(o)
            return o

        with open(os.path.join(out_dir, "v4_report.json"), "w",
                  encoding="utf-8") as f:
            json.dump(_jb(report), f, ensure_ascii=False, indent=1)
        with open(os.path.join(out_dir, "v4_factors.json"), "w",
                  encoding="utf-8") as f:
            json.dump(_jb({r["code"]: r["factor_table"] for r in preds
                           if r.get("factor_table")}),
                      f, ensure_ascii=False, indent=1)
        p("v4.0：报告已写入 research/v4_report.json")
    except Exception:
        log.exception("v4 报告落盘失败")
    return report


def _v4_print_report(r):
    """控制台摘要（CLI --v4）。"""
    m = r["meta"]
    print("=" * 76)
    print(f"v4.0 全A研究  {m['ts']}  股票池 {m['n_codes_pool']} 只 / "
          f"有效 {m['n_valid']} 只  "
          f"区间 {m['date_min']} ~ {m['date_max']}（中位{m['span_med']}日）")
    print(f"依赖: numpy={m['deps'].get('numpy')} "
          f"sklearn={m['deps'].get('sklearn')} "
          f"lightgbm={m['deps'].get('lightgbm')}")
    print("-" * 76)
    print("Horizon 实验（LightGBM 预测 vs 真实收益）")
    print(f"{'H':>4}{'IC':>9}{'IC_IR':>8}{'MAE':>9}{'方向命中':>9}{'样本':>9}")
    for H in ("1", "5", "10"):
        e = r["horizon"].get(H, {})
        f = lambda v, k=100: "-" if v is None else f"{v*k:+.3f}"
        print(f"{H:>4}{f(e.get('ic'), 1):>9}{f(e.get('ic_ir'), 1):>8}"
              f"{f(e.get('mae'), 1):>9}"
              f"{f(e.get('hit')):>9}{e.get('n', 0):>9}")
    print("Horizon 自适应选择分布: " + str(r["horizon"].get("h_choice_dist")))
    print("-" * 76)
    print("模型比较（vs 当日所选 Horizon 真实收益）")
    print(f"{'模型':<20}{'IC':>9}{'MAE':>9}{'方向命中':>9}{'样本':>9}")
    NM = {"ml": "LightGBM", "q50": "Quantile Q50", "adaptive": "Lasso得分",
          "p_up": "Logistic p_up", "l1_up": "L1形态up_prob",
          "l1_ret": "L1相似样本收益", "baseline_composite": "v3.3多维评分"}
    for k, e in r["models"].items():
        f = lambda v, k2=100: "-" if v is None else f"{v*k2:+.3f}"
        print(f"{NM.get(k, k):<20}{f(e.get('ic'), 1):>9}"
              f"{f(e.get('mae'), 1):>9}{f(e.get('hit')):>9}{e['n']:>9}")
    q = r["quantile"]
    print("-" * 76)
    print("Quantile 诊断: " + "  ".join(
        f"P{qk.split('_')[1]}={v:.4f}" if v is not None else "-"
        for qk, v in q.items() if qk.startswith("pinball"))
        + f"  覆盖率[10,90]={q.get('coverage_10_90')}"
        + f"  原始交叉率={q.get('crossing_raw_med')}")
    o = r["overfit"]
    print(f"LightGBM 过拟合检查: Train IC 中位 {o['lgbm_train_ic_med']} "
          f"vs Test IC {o['lgbm_test_ic']} → "
          f"{'⚠️疑似过拟合' if o['flag'] else '未见明显过拟合'}")
    print("-" * 76)
    print("因子有效性（跨股聚合）")
    print(f"{'因子':<10}{'IC中位':>9}{'稳定性':>8}{'入选率':>8}{'中位|权重|':>10}")
    for f_, a in r["factors"].items():
        if f_.startswith("_"):
            continue
        print(f"{f_:<10}{a['train_ic_med']:+9.3f}{a['stability_med']:8.2f}"
              f"{a['sel_pct']*100:7.0f}%{a['weight_med']:10.4f}")
    print("-" * 76)
    _c = _V4_COST
    print("组合回测（成本：滑点%.2f%%/佣金%.3f%%/印花税%.2f%%；初始%d万）"
          % (_c["slip"] * 100, _c["commission"] * 100, _c["stamp"] * 100,
             int(_V4_CAPITAL / 10000)))
    print(f"{'策略':<24}{'年化':>9}{'回撤':>9}{'Calmar':>8}{'Sharpe':>8}"
          f"{'胜率':>7}{'盈亏比':>7}{'交易':>6}{'均持仓':>7}")
    for k, v in r["strategies"].items():
        g = lambda v_, d=1: "-" if v_ is None else f"{v_*100:+.{d}f}%"
        g2 = lambda v_: "-" if v_ is None else f"{v_:.2f}"
        hold = "-" if v["avg_hold"] is None else f"{v['avg_hold']:.1f}"
        print(f"{k:<24}{g(v['ann']):>9}{g(v['mdd']):>9}"
              f"{g2(v['calmar']):>8}{g2(v['sharpe']):>8}"
              f"{g(v['winrate']):>7}{g2(v['pf']):>7}{v['trades']:>6}"
              f"{hold:>7}")
    print("-" * 76)
    print("消融实验（平衡档；IC=该变体入场分数 pooled IC）")
    print(f"{'变体':<28}{'年化':>9}{'回撤':>9}{'Calmar':>8}{'Sharpe':>8}"
          f"{'胜率':>7}{'交易':>6}{'IC':>8}")
    for vn, a in r["ablation"].items():
        v = a["tier"]["平衡"]
        g = lambda v_, d=1: "-" if v_ is None else f"{v_*100:+.{d}f}%"
        g2 = lambda v_: "-" if v_ is None else f"{v_:.2f}"
        ic = a.get("entry_score_ic")
        print(f"{vn:<28}{g(v['ann']):>9}{g(v['mdd']):>9}"
              f"{g2(v['calmar']):>8}{g2(v['sharpe']):>8}{g(v['winrate']):>7}"
              f"{v['trades']:>6}{('-' if ic is None else f'{ic:+.3f}'):>8}")
    print("=" * 76)
    print("注：全部为历史统计研究，不构成投资建议。")


# ============ v6.1.3 三档组合策略引擎（稳健/均衡/激进；全A/主板/ETF/全A含ETF） ============
#
# 原理（详见 README 第二节）：
#   稳健 = 全A「20日动量 + 20日低波」横截面合成排名 Top20，每20日调仓，
#          上证 MA20 闸门（T-1 收盘在均线上才持仓）
#   均衡 = 同选股 Top20，每10日调仓，其余同上（更高换手换更高弹性）
#   激进 = 创业板「60日 β（对创业板指）」最高 Top5，每10日调仓，
#          创业板指 MA60 闸门（慢闸门过滤熊市、放大上行 beta）
# 激进档基准（v6.1.1）：两个口径统一对标科创50（sh000688），
#         不再按是否具备科创板权限区分（多数账户无科创板权限，但仍以科创50
#         作为「高弹性成长」这一风格的统一参照）。
# 防前视：信号/闸门/流动性过滤全部截止 T-1，T 日收盘成交；涨停不买、
#         跌停不卖、停牌顺延；退市/长停 20 日后按最后收盘价了结。
# 调仓相位：资金分 reb 份错开相位同时运行后平均（tranche averaging），
#         消除单一调仓日的运气；tier_backtest 报告相位年化区间。

TIER_CFG = {
    "稳健": dict(universe="all", score="blend", top=20, reb=20,
                 gate="sh000001", ma=20),
    "均衡": dict(universe="all", score="blend", top=20, reb=10,
                 gate="sh000001", ma=20),
    "激进": dict(universe="chinext", score="beta", top=5, reb=10,
                 gate="sz399006", ma=60),
}
# 主板口径（v6.1.1）：稳健/均衡在沪主板+深主板内运行；
# 激进档改用 blend_mom（动量0.7/低波0.3）偏弹性 + 高换手（reb10/top20/上证MA20）。
# 依据（2026-09-19 过拟合诊断，全量缓存）：
#   · β 选股（对上证/对创业板指）在主板池全面失效：年化 -6.7%、回撤 -47.2%、
#     参数邻域 top3/5/10/20 与 MA20/60/120 全为负、随机安慰剂不比真实打分差
#     → 判定为口径设计缺陷（主板与上证同源，高β≈上证自身高波动成分，无独立 alpha）；
#   · 改为 blend_mom 后：全期 +6.6%、样本外 +13.0%（超额 +12.3pp）、
#     强势段 +14.1%、交易 9564 笔（原 1746 笔，提升 5.5 倍）、回撤 -12.3%；
#   · 动量权重邻域 0.6/0.7 稳健（+7.0%/+6.6%），0.9/1.0 崩坏（-10.5%/-28.2%）
#     → 取 0.7 并保留低波 0.3 作为防守项。
TIER_CFG_MAIN = {
    "稳健": dict(universe="main", score="blend", top=20, reb=20,
                 gate="sh000001", ma=20),
    "均衡": dict(universe="main", score="blend", top=20, reb=10,
                 gate="sh000001", ma=20),
    "激进": dict(universe="main", score="blend_mom", mom_w=0.7, top=20, reb=10,
                 gate="sh000001", ma=20),
}
# ETF 口径（v6.1.2）：池子仅 ETF/LOF。ETF 无创业板/行业语义，
# 故三档都用「动量+低波」族：稳健/均衡等权 blend，激进偏动量 blend_mom。
# 闸门统一上证 MA20（ETF 池跨沪深，用市场总闸门）。
TIER_CFG_ETF = {
    "稳健": dict(universe="etf", score="blend", top=10, reb=20,
                 gate="sh000001", ma=20),
    "均衡": dict(universe="etf", score="blend", top=10, reb=10,
                 gate="sh000001", ma=20),
    "激进": dict(universe="etf", score="blend_mom", mom_w=0.7, top=10, reb=10,
                 gate="sh000001", ma=20),
}
# 全A含ETF 口径（v6.1.2）：个股 + ETF 同一池排序；激进用 blend_mom
# （池内混入 ETF 后，创业板高β 不再适用）。
TIER_CFG_ALLETF = {
    "稳健": dict(universe="all_etf", score="blend", top=20, reb=20,
                 gate="sh000001", ma=20),
    "均衡": dict(universe="all_etf", score="blend", top=20, reb=10,
                 gate="sh000001", ma=20),
    "激进": dict(universe="all_etf", score="blend_mom", mom_w=0.7, top=20,
                 reb=10, gate="sh000001", ma=20),
}
TIER_UNIVERSES = {"all": TIER_CFG, "main": TIER_CFG_MAIN,
                  "etf": TIER_CFG_ETF, "all_etf": TIER_CFG_ALLETF}
UNIVERSE_NAME = {"all": "全A", "main": "沪深主板", "etf": "ETF",
                 "all_etf": "全A含ETF"}
# 激进档统一对标科创50（不分是否具备科创板权限）：全A 激进选创业板高β，
# 主板/ETF/全A含ETF 激进用 blend_mom 弹性档，都以科创50 作为主基准。
TIER_BENCH = {
    ("all", "稳健"): "sh000001", ("all", "均衡"): "sh000001",
    ("all", "激进"): "sh000688",
    ("main", "稳健"): "sh000001", ("main", "均衡"): "sh000001",
    ("main", "激进"): "sh000688",
    ("etf", "稳健"): "sh000001", ("etf", "均衡"): "sh000001",
    ("etf", "激进"): "sh000688",
    ("all_etf", "稳健"): "sh000001", ("all_etf", "均衡"): "sh000001",
    ("all_etf", "激进"): "sh000688",
}
# 基准指数中文名（报告/对照用）
BENCH_NAME = {"sh000001": "上证指数", "sz399006": "创业板指",
              "sh000688": "科创50", "sz399001": "深证成指"}


def tier_cfg(tier, universe="all"):
    """按口径取某档配置（all=全A / main=主板 / etf=ETF / all_etf=全A含ETF）。"""
    return dict(TIER_UNIVERSES.get(universe, TIER_CFG).get(tier) or
                TIER_CFG.get(tier) or {})


def tier_universe_mask(codes, kind):
    """标的池掩码（v6.1.2）：
      all      = 全A个股（**不含** ETF，保持历史口径不变）
      all_etf  = 全A个股 + ETF
      main     = 沪深主板个股
      chinext  = 创业板个股
      etf      = 仅 ETF/LOF"""
    if kind == "chinext":
        return np.array([c.startswith("sz30") for c in codes])
    if kind == "main":
        return np.array([c.startswith(("sh60", "sz00")) for c in codes])
    if kind == "etf":
        return np.array([_is_etf(c) for c in codes])
    if kind == "all_etf":
        return np.ones(len(codes), bool)
    # all（默认）：显式排除 ETF，保证 2026-09 之前的口径可比
    return np.array([not _is_etf(c) for c in codes])
_TIER_PREFIXES = ("sh60", "sh68", "sz00", "sz30",
                  "sh51", "sh56", "sh58", "sz15", "sz16", "sz18")
_TIER_MIN_PRICE = 1.0
_TIER_MIN_BARS = 250
_TIER_MIN_AMOUNT = 3e5            # V(手)×价 = 成交额/100，3e5 → 3000万元
_TIER_STALE_DAYS = 20
_TIER_SLIP = 0.001
_TIER_COMMISSION = 0.00025
_TIER_MIN_COMMISSION = 5.0
_TIER_STAMP = 0.001
_TIER_TRANSFER = 0.00001
_TIER_LOT = 100
_TIER_CACHE = {}


def tier_segments(cal):
    """回测段定义（tranche 起点由各档 reb 决定，见 tier_eval）。"""
    return {
        "full": ("2022-09-01", cal[-1]),
        "train": (cal[0], "2024-12-31"),
        "val": ("2025-08-29", cal[-1]),
        "val2025": ("2025-01-02", "2025-08-28"),
        "bull": ("2025-03-18", "2026-09-04"),
    }


def tier_load_panel():
    """全A日K面板（hfq × adjust = 乘法前复权≈现价）。结果缓存。"""
    if np is None:
        raise RuntimeError("三档引擎需要 numpy")
    if _TIER_CACHE.get("panel") is not None:
        return _TIER_CACHE["panel"]
    t0 = time.time()
    with db_conn() as conn:
        adj = {c: (k or 1.0) for c, k in
               conn.execute("select code,k from adjust")}
        rows = conn.execute(
            "select code,date,close,vol from daily_bars "
            "where date>=? order by code,date", ("2020-01-01",)).fetchall()
    data, dates = {}, set()
    for c, d, cl, v in rows:
        if not c.startswith(_TIER_PREFIXES):
            continue
        data.setdefault(c, []).append((d, cl, v or 0.0))
        dates.add(d)
    codes = sorted(data)
    cal = sorted(dates)
    didx = {d: i for i, d in enumerate(cal)}
    n, nc = len(codes), len(cal)
    C = np.full((n, nc), np.nan, np.float32)
    V = np.zeros((n, nc), np.float32)
    for r, c in enumerate(codes):
        seq = data[c]
        k = adj.get(c, 1.0)
        cols = np.fromiter((didx[x[0]] for x in seq), np.int64, len(seq))
        C[r, cols] = [(x[1] * k) if x[1] else np.nan for x in seq]
        V[r, cols] = [x[2] for x in seq]
    log.info("tier panel %d 只 × %d 日 (%.0fs)", n, nc, time.time() - t0)
    _TIER_CACHE["panel"] = (codes, cal, C, V)
    return codes, cal, C, V


def tier_idx_series(cal, code):
    """指数收盘序列（按面板日历对齐、前向填充）与日收益。"""
    with db_conn() as conn:
        rows = conn.execute("select date,close from daily_bars where code=? "
                            "order by date", (code,)).fetchall()
    m = {d: c for d, c in rows if c}
    cl = np.full(len(cal), np.nan)
    last = np.nan
    for i, d in enumerate(cal):
        v = m.get(d)
        if v:
            last = v
        cl[i] = last
    r = np.full(len(cal), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        r[1:] = np.where(cl[:-1] > 0, cl[1:] / cl[:-1] - 1.0, np.nan)
    return cl, r


def tier_build_features(cal, C, V):
    """因果特征：20日波动 / 20日均额 / 上市根数 / 60日β（对创业板指）。"""
    NST, NDT = C.shape
    ret1 = np.full_like(C, np.nan)
    ret1[:, 1:] = C[:, 1:] / C[:, :-1] - 1.0
    ret20 = np.full_like(C, np.nan)
    ret20[:, 20:] = C[:, 20:] / C[:, :-20] - 1.0
    x = np.nan_to_num(ret1, nan=0.0)
    v = np.isfinite(ret1).astype(float)
    cx = np.cumsum(np.insert(x, 0, 0, 1), axis=1)
    cv = np.cumsum(np.insert(v, 0, 0, 1), axis=1)
    cxx = np.cumsum(np.insert(x * x, 0, 0, 1), axis=1)
    n20 = cv[:, 20:] - cv[:, :-20]
    s20 = cx[:, 20:] - cx[:, :-20]
    s220 = cxx[:, 20:] - cxx[:, :-20]
    vol20 = np.full_like(C, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        m20 = s20 / np.maximum(n20, 1)
        var = np.where(n20 > 1, s220 / np.maximum(n20, 1) - m20 * m20, np.nan)
        vol20[:, 19:] = np.sqrt(np.maximum(var, 0))
    amt = V * C
    ca = np.cumsum(np.insert(np.nan_to_num(amt), 0, 0, 1), axis=1)
    cva = np.cumsum(np.insert(np.isfinite(amt).astype(float), 0, 0, 1), axis=1)
    amt20 = np.full_like(amt, np.nan)
    amt20[:, 19:] = ((ca[:, 20:] - ca[:, :-20])
                     / np.maximum(cva[:, 20:] - cva[:, :-20], 1))
    barcount = np.cumsum(np.isfinite(C), axis=1)

    def _beta(idx_code):
        _, idx_ret = tier_idx_series(cal, idx_code)
        b = np.full_like(C, np.nan)
        for t in range(60, NDT):
            y = idx_ret[t - 59:t + 1]
            vv = np.isfinite(y)
            if int(vv.sum()) < 48:
                continue
            xs = x[:, t - 59:t + 1]
            m = np.isfinite(ret1[:, t - 59:t + 1]) & vv[None, :]
            nn = np.maximum(m.sum(axis=1), 1)
            ym = np.where(vv, y, 0.0)
            sx = (xs * m).sum(axis=1)
            sy = (ym[None, :] * m).sum(axis=1)
            sxy = (xs * m * ym[None, :]).sum(axis=1)
            syy = (ym[None, :] ** 2 * m).sum(axis=1)
            cov = sxy / nn - (sx / nn) * (sy / nn)
            vr = syy / nn - (sy / nn) ** 2
            ok = (m.sum(axis=1) >= 48) & (vr > 1e-12)
            b[ok, t] = cov[ok] / vr[ok]
        return b

    beta60 = _beta("sz399006")        # 对创业板指（全A 激进档用）
    beta60_sh = _beta("sh000001")     # 对上证（主板激进档用）
    return dict(ret20=ret20, vol20=vol20, amt20=amt20, barcount=barcount,
                beta60=beta60, beta60_sh=beta60_sh)


def _tier_rank01(x):
    m = np.isfinite(x)
    out = np.full(len(x), np.nan)
    n = int(m.sum())
    if n > 5:
        out[m] = x[m].argsort().argsort() / max(1, n - 1)
    return out


def tier_rank_base(codes, universe):
    """横截面排名的**基数池**（v6.1.2）。

    历史口径：个股排名在全A个股池内做，再按口径筛持仓。加入 ETF 后若
    仍按整个面板排名，会污染既有全A/主板数字，故这里显式区分：
      all / main  → 个股池（保持 2026-09 之前口径不变）
      etf         → ETF 池
      all_etf     → 个股 + ETF 合并池
    """
    arr = np.array(codes)
    etf = np.array([_is_etf(c) for c in arr])
    if universe == "etf":
        return etf
    if universe == "all_etf":
        return np.ones(len(arr), bool)
    return ~etf


def tier_make_score(feat, kind, mom_w=None, base=None):
    """合成打分：
      blend      = 动量20 与 低波20 百分位等权（稳健/均衡）
      blend_mom  = 偏动量弹性（动量 mom_w、低波 1-mom_w；激进档用，默认 0.7）
      beta       = 60日β（对创业板指）
      beta_sh    = 60日β（对上证，主板口径）
    base：横截面排名基数掩码（None=全面板）；见 tier_rank_base。
    注：beta 两口径在主板池已证伪（全期年化为负、回撤 40%+），仅保留作研究对照。"""
    NST, NDT = feat["vol20"].shape
    if kind == "beta":
        return feat["beta60"]
    if kind == "beta_sh":
        return feat.get("beta60_sh", feat["beta60"])
    if kind in ("blend", "blend_mom"):
        if kind == "blend_mom":
            mw = 0.7 if mom_w is None else float(mom_w)
        else:
            mw = 0.5
        ret = feat["ret20"]
        vol = feat["vol20"]
        if base is not None and not bool(base.all()):
            b2 = base[:, None]
            ret = np.where(b2, ret, np.nan)
            vol = np.where(b2, vol, np.nan)
        r1 = np.zeros_like(feat["vol20"])
        r2 = np.zeros_like(feat["vol20"])
        for t in range(NDT):
            r1[:, t] = _tier_rank01(ret[:, t])
            r2[:, t] = _tier_rank01(vol[:, t])
        return mw * r1 + (1.0 - mw) * (1.0 - r2)
    raise ValueError("未知评分: " + kind)


def tier_make_gate(cal, code, ma_w):
    """指数趋势闸门：第 t 日可持仓 = T-1 收盘 > MA(ma_w)（截至 T-1）。"""
    cl, _ = tier_idx_series(cal, code)
    ma = np.full(len(cal), np.nan)
    ma[ma_w - 1:] = np.convolve(cl, np.ones(ma_w) / ma_w, "valid")
    gate = np.zeros(len(cal), bool)
    with np.errstate(invalid="ignore"):
        gate[1:] = (cl[:-1] > ma[:-1]) & np.isfinite(ma[:-1])
    return gate


def _tier_limit_pct(code):
    return 0.20 if (code.startswith("sz30") or code.startswith("sh68")) else 0.10


def tier_sim_phase(codes, cal, C, feat, score, gate, i0, i1, cfg, phase=0,
                   capital=1e6):
    """单相位组合模拟：T-1 决策、T 收盘成交、完整费用与整手约束。"""
    NST = C.shape[0]
    top, reb = cfg["top"], cfg["reb"]
    uni = tier_universe_mask(codes, cfg.get("universe", "all"))
    elig = (np.isfinite(C) & (C > _TIER_MIN_PRICE)
            & (feat["barcount"] >= _TIER_MIN_BARS)
            & (feat["amt20"] >= _TIER_MIN_AMOUNT) & uni[:, None])
    lim = np.array([_tier_limit_pct(c) for c in codes])[:, None]
    r1 = np.full_like(C, np.nan)
    r1[:, 1:] = C[:, 1:] / C[:, :-1] - 1.0
    limit_up = r1 >= lim - 0.005
    limit_dn = r1 <= -(lim - 0.005)
    cash = float(capital)
    shares = np.zeros(NST)
    entry = np.zeros(NST)
    t_in = np.zeros(NST, int)
    last = np.full(NST, np.nan)
    miss = np.zeros(NST, int)
    holding = np.zeros(NST, bool)
    target = set()
    trades = []
    eq, eq_cal = [], []

    def sell_fee(amount):
        return (max(amount * _TIER_COMMISSION, _TIER_MIN_COMMISSION)
                + amount * _TIER_STAMP + amount * _TIER_TRANSFER)

    def close_pos(k, t, px, reason):
        nonlocal cash
        amount = shares[k] * px
        net = amount - sell_fee(amount)
        cash += net
        trades.append({"code": str(codes[k]),
                       "ret": net / (shares[k] * entry[k]) - 1.0,
                       "hold": int(t - t_in[k]), "t_in": int(t_in[k]),
                       "t_out": int(t), "entry_px": float(entry[k]),
                       "exit_px": float(px), "reason": reason})
        holding[k] = False
        shares[k] = 0.0
        target.discard(int(k))

    start = i0 + phase
    for j in range(max(0, start - 40), start + 1):
        f = np.isfinite(C[:, j])
        last[f] = C[f, j]
    for t in range(start, i1):
        col = C[:, t]
        fin = np.isfinite(col)
        last[fin] = col[fin]
        miss = np.where(holding & ~fin, miss + 1, 0)
        for k in np.nonzero(holding & (miss >= _TIER_STALE_DAYS))[0]:
            close_pos(k, t, last[k] if last[k] > 0 else entry[k], "delist")
        on = bool(gate[t]) if gate is not None else True
        if (t - start) % reb == 0 and t > start:
            d = t - 1
            cand = np.nonzero(elig[:, d])[0]
            s = score[cand, d]
            m = np.isfinite(s)
            cand, s = cand[m], s[m]
            order = cand[np.argsort(-s, kind="stable")]
            target = set(order[:top].tolist()) if on else set()
        for k in np.nonzero(holding)[0]:
            if int(k) in target or not fin[k] or limit_dn[k, t]:
                continue
            close_pos(k, t, col[k] * (1 - _TIER_SLIP),
                      "target" if on else "gate")
        if (t - start) % reb == 0 and t > start and on:
            equity = cash + float(np.nansum(np.where(holding,
                                                     shares * last, 0.0)))
            for k in order:
                if int(holding.sum()) >= top:
                    break
                if holding[k] or not fin[k] or limit_up[k, t]:
                    continue
                px = col[k] * (1 + _TIER_SLIP)
                budget = min(equity / top, cash)
                n_lot = int(budget / (px * _TIER_LOT))
                if n_lot <= 0:
                    continue
                amount = n_lot * _TIER_LOT * px
                fee = max(amount * _TIER_COMMISSION, _TIER_MIN_COMMISSION) \
                    + amount * _TIER_TRANSFER
                if amount + fee > cash:
                    continue
                cash -= amount + fee
                shares[k] = n_lot * _TIER_LOT
                entry[k] = (amount + fee) / shares[k]
                t_in[k] = t
                holding[k] = True
        eq.append(cash + float(np.nansum(np.where(holding,
                                                  shares * last, 0.0))))
        eq_cal.append(cal[t])
    return np.array(eq), eq_cal, trades


def _tier_metrics(eq, dates, trades=None):
    eq = np.asarray(eq, float)
    out = {"total": None, "ann": None, "mdd": None, "sharpe": None,
           "trades": 0, "winrate": None, "pf": None, "days": len(eq)}
    if len(eq) < 2 or eq[0] <= 0:
        return out
    import datetime as _d
    out["total"] = float(eq[-1] / eq[0] - 1.0)
    years = max((_d.date.fromisoformat(dates[-1])
                 - _d.date.fromisoformat(dates[0])).days / 365.25, 0.05)
    mult = eq[-1] / eq[0]
    out["ann"] = float(mult ** (1 / years) - 1.0) if mult > 0 else -1.0
    peak = np.maximum.accumulate(eq)
    out["mdd"] = float((eq / peak - 1.0).min())
    dr = np.diff(eq) / eq[:-1]
    sd = float(dr.std())
    out["sharpe"] = float(dr.mean() / sd * math.sqrt(252.0)) \
        if sd > 1e-12 else None
    if trades:
        rr = np.array([t["ret"] for t in trades])
        out["trades"] = len(rr)
        out["winrate"] = float((rr > 0).mean())
        gp = rr[rr > 0].sum()
        gl = -rr[rr <= 0].sum()
        out["pf"] = float(gp / gl) if gl > 1e-9 else None
    return out


def tier_eval(segment="full", tiers=None, phases=None, progress=None,
              overrides=None, universe="all"):
    """三档回测（相位平均主口径）。overrides 可覆盖 cfg（研究用）。
    universe: all=全A / main=沪深主板。返回 {tier: metrics}。"""
    codes, cal, C, V = tier_load_panel()
    if progress:
        progress("三档引擎：构建特征 ...")
    key = "feat"
    if _TIER_CACHE.get(key) is None:
        _TIER_CACHE[key] = tier_build_features(cal, C, V)
    feat = _TIER_CACHE[key]
    segs = tier_segments(cal)
    if segment in ("2022", "2023", "2024", "2025", "2026"):
        a = f"{segment}-01-01" if segment != "2022" else "2022-09-01"
        b = f"{segment}-12-31" if segment != "2026" else cal[-1]
    elif segment in segs:
        a, b = segs[segment]
    else:
        raise ValueError("未知区间: " + segment)
    i0 = int(np.searchsorted(cal, a))
    i1 = int(np.searchsorted(cal, b, side="right"))
    base = TIER_UNIVERSES.get(universe, TIER_CFG)
    tiers = list(base) if not tiers else [t for t in tiers if t in base]
    out = {}
    for tier in tiers:
        cfg = tier_cfg(tier, universe)
        if overrides:
            cfg.update(overrides)
        _base = tier_rank_base(codes, universe)
        score = tier_make_score(feat, cfg["score"], cfg.get("mom_w"),
                                base=_base)
        gate = tier_make_gate(cal, cfg["gate"], cfg["ma"]) \
            if cfg.get("gate") else None
        n_ph = min(phases or cfg["reb"], cfg["reb"])
        norms, dates, all_trades = [], None, []
        for p in range(n_ph):
            eq, ec, tr = tier_sim_phase(codes, cal, C, feat, score, gate,
                                        i0, i1, cfg, phase=p, capital=1e6)
            j0 = cfg["reb"] - 1 - p
            if j0 >= len(eq) or eq[j0] <= 0:
                continue
            norms.append(eq[j0:] / eq[j0])
            dates = ec[j0:]
            all_trades.extend(tr)
            if progress and p == 0:
                progress(f"[{tier}] {ec[j0]} ~ {ec[-1]} 相位计算中 ...")
        if not norms:
            continue
        L = min(len(e) for e in norms)
        E = np.mean([e[:L] for e in norms], axis=0) * 1e6
        dates = dates[:L]
        m = _tier_metrics(E, dates, all_trades)
        anns = [_tier_metrics(e[:L], dates)["ann"] for e in norms]
        m["phase_ann_min"] = min(anns)
        m["phase_ann_max"] = max(anns)
        m["phase_anns"] = [float(x) for x in anns]   # 箱线图/版本对比用
        # 相位平均净值曲线（降采样≤600点，网页/绘图用；首值=1）
        cd, cv = _sample_series(dates, E)
        m["curve_dates"], m["curve"] = cd, cv
        bench_code = (TIER_BENCH.get((universe, tier))
                      or TIER_BENCH[("all", tier)])
        # 多基准对照（v6.1.1）：主基准 + 其余指数，避免单一强基准让超额恒负
        extra = [c for c in ("sh000001", "sz399006", "sh000688")
                 if c != bench_code]
        benches = {}
        bseries = {}
        for code in [bench_code] + extra:
            try:
                bcl, _ = tier_idx_series(cal, code)
                bmap = {d: v for d, v in zip(cal, bcl)}
                bseg = np.array([bmap.get(d, np.nan) for d in dates], float)
                bseries[code] = bseg
                benches[code] = _tier_metrics(bseg, dates)
            except Exception:
                continue
        bm = benches.get(bench_code)
        m["benchmark"] = bench_code
        m["bench"] = bm
        m["benches"] = benches
        if bench_code in bseries:
            bd, bv = _sample_series(dates, bseries[bench_code])
            m["bench_curve_dates"], m["bench_curve"] = bd, bv
        m["excess_total"] = (m["total"] - bm["total"]) \
            if bm and bm["total"] is not None else None
        m["range"] = [dates[0], dates[-1]]
        out[tier] = m
    return out


def _sample_returns(rr, cap=1500):
    """箱线图用收益分布：超过 cap 时等步长抽样后升序返回（保留两端尾部）。"""
    a = np.sort(np.asarray(rr, float))
    if len(a) > cap:
        a = a[np.linspace(0, len(a) - 1, cap).astype(int)]
    return [float(round(x, 6)) for x in a]


def _sample_series(dates, arr, cap=600):
    """曲线降采样（保留首尾），净值归一化到首值=1；返回 (dates, values)。
    供报告/网页画收益曲线（控制 JSON 体积）。"""
    a = np.asarray(arr, float)
    n = len(a)
    if n == 0:
        return [], []
    idx = (np.linspace(0, n - 1, cap).astype(int) if n > cap
           else np.arange(n))
    base = a[0] if a[0] else 1.0
    ds = [dates[i] for i in idx] if dates is not None else []
    return ds, [float(round(a[i] / base, 6)) for i in idx]


def tier_picks_stats(segment="full", tiers=None, phases=None, progress=None,
                     overrides=None, universe="all"):
    """荐股收益回测：把三档策略的每一次「推荐→平仓」当一笔交易统计。

    与 tier_eval 同引擎（相位平均），区别是输出逐笔荐股口径：
    推荐次数/平均收益/胜率/盈亏比/持有期/右尾占比/退出原因分布。"""
    codes, cal, C, V = tier_load_panel()
    if _TIER_CACHE.get("feat") is None:
        _TIER_CACHE["feat"] = tier_build_features(cal, C, V)
    feat = _TIER_CACHE["feat"]
    segs = tier_segments(cal)
    if segment in ("2022", "2023", "2024", "2025", "2026"):
        a = f"{segment}-01-01" if segment != "2022" else "2022-09-01"
        b = f"{segment}-12-31" if segment != "2026" else cal[-1]
    elif segment in segs:
        a, b = segs[segment]
    else:
        raise ValueError("未知区间: " + segment)
    i0 = int(np.searchsorted(cal, a))
    i1 = int(np.searchsorted(cal, b, side="right"))
    base = TIER_UNIVERSES.get(universe, TIER_CFG)
    tiers = list(base) if not tiers else [t for t in tiers if t in base]
    out = {}
    for tier in tiers:
        cfg = tier_cfg(tier, universe)
        if overrides:
            cfg.update(overrides)
        _base = tier_rank_base(codes, universe)
        score = tier_make_score(feat, cfg["score"], cfg.get("mom_w"),
                                base=_base)
        gate = tier_make_gate(cal, cfg["gate"], cfg["ma"]) \
            if cfg.get("gate") else None
        n_ph = min(phases or cfg["reb"], cfg["reb"])
        trades = []
        for p in range(n_ph):
            _, _, tr = tier_sim_phase(codes, cal, C, feat, score, gate,
                                      i0, i1, cfg, phase=p, capital=1e6)
            trades.extend(tr)
            if progress and p == 0:
                progress(f"[{tier}] 荐股回测 {cal[i0 + p]} ~ {cal[i1 - 1]} ...")
        if not trades:
            out[tier] = {"n": 0, "range": [cal[i0], cal[i1 - 1]]}
            continue
        rr = np.array([t["ret"] for t in trades], float)
        holds = np.array([t["hold"] for t in trades], float)
        wins = rr[rr > 0]
        losses = rr[rr <= 0]
        reasons = {}
        for t in trades:
            reasons[t.get("reason", "?")] = reasons.get(t.get("reason", "?"), 0) + 1
        out[tier] = {
            "n": len(rr), "range": [cal[i0], cal[i1 - 1]],
            "avg_ret": float(rr.mean()), "med_ret": float(np.median(rr)),
            "winrate": float((rr > 0).mean()),
            "avg_win": float(wins.mean()) if len(wins) else None,
            "avg_loss": float(losses.mean()) if len(losses) else None,
            "payoff": float(wins.mean() / abs(losses.mean()))
            if len(wins) and len(losses) else None,
            "pf": float(wins.sum() / -losses.sum())
            if len(losses) and losses.sum() < 0 else None,
            "avg_hold": float(holds.mean()), "med_hold": float(np.median(holds)),
            "best": float(rr.max()), "worst": float(rr.min()),
            "tail20": float((rr > 0.20).mean()),
            "tail50": float((rr > 0.50).mean()),
            "by_reason": reasons,
            "rets": _sample_returns(rr),      # 逐笔收益分布（箱线图/对比用）
        }
    return out


def tier_picks_report_text(segment="full", tiers=None, capital=0.0,
                           universe="all", overrides=None):
    """GUI/CLI 共用：按风险偏好的荐股收益回测文本表。"""
    st = tier_picks_stats(segment=segment, tiers=tiers, overrides=overrides,
                          universe=universe)
    uni_name = UNIVERSE_NAME.get(universe, universe)
    lines = [f"v6.1.2 荐股收益回测 · {segment} · {uni_name} · "
             f"按风险偏好（逐笔口径）",
             "口径：T-1 打分 → T 日收盘买入 → 调仓/闸门/退市平仓；"
             "含滑点/佣金/印花税/整手；每档分 reb 个相位并行，"
             "笔数为全部相位交易合计、单笔等权",
             ""]
    hdr = (f"{'档位':<5}{'推荐笔数':>8}{'平均收益':>9}{'中位':>8}"
           f"{'胜率':>7}{'盈亏比':>8}{'PF':>6}{'均持有':>7}"
           f"{'最好':>8}{'最差':>8}{'>20%':>7}{'>50%':>7}")
    lines.append(hdr)
    for tier, s in st.items():
        if not s.get("n"):
            lines.append(f"{tier:<5}{'0（闸门长期关闭/无信号）':>40}")
            continue
        lines.append(
            f"{tier:<5}{s['n']:>8}{s['avg_ret']*100:>+8.2f}%"
            f"{s['med_ret']*100:>+7.2f}%{s['winrate']*100:>6.1f}%"
            f"{(s['payoff'] or 0):>8.2f}{(s['pf'] or 0):>6.2f}"
            f"{s['avg_hold']:>7.1f}{s['best']*100:>+7.1f}%"
            f"{s['worst']*100:>+7.1f}%{s['tail20']*100:>6.1f}%"
            f"{s['tail50']*100:>6.1f}%")
    lines.append("")
    for tier, s in st.items():
        if s.get("n"):
            r = s["by_reason"]
            lines.append(f"[{tier}] 退出原因: 调仓 {r.get('target', 0)} / "
                         f"闸门 {r.get('gate', 0)} / 退市 {r.get('delist', 0)}"
                         f"    区间 {s['range'][0]} ~ {s['range'][1]}")
    lines.append("")
    lines.append("组合收益（等权跟买）见 `backtests/backtest_tiers.py --segment " +
                 segment + "`；注：历史统计研究，不构成投资建议。")
    return "\n".join(lines)


def tier_latest_picks(capital=100000.0, min_active=300, tiers=None,
                      universe="all", apply_perms=True):
    """生产端：按最新可用交易日给出目标持仓（含闸门状态/板块权限过滤）。
    universe: all=全A / main=沪深主板。apply_perms 开启时套用设置里的荐股权限。"""
    codes, cal, C, V = tier_load_panel()
    if _TIER_CACHE.get("feat") is None:
        _TIER_CACHE["feat"] = tier_build_features(cal, C, V)
    feat = _TIER_CACHE["feat"]
    NST, NDT = C.shape
    # 最后一个「足够多股票有数据」的交易日作为信号日
    elig_n = (np.isfinite(C) & (C > _TIER_MIN_PRICE)
              & (feat["barcount"] >= _TIER_MIN_BARS)
              & (feat["amt20"] >= _TIER_MIN_AMOUNT)).sum(axis=0)
    good = np.nonzero(elig_n >= min_active)[0]
    if not len(good):
        raise RuntimeError("没有可用交易日")
    d = int(good[-1])
    signal_date = cal[d]
    with db_conn() as conn:
        info = {c: (n or c, ind or "")
                for c, n, ind in
                conn.execute("select code,name,industry from stocks")}
    names = {c: v[0] for c, v in info.items()}
    risky = np.array([("ST" in names.get(c, "").upper()
                       or "退" in names.get(c, "")) for c in codes])
    base = TIER_UNIVERSES.get(universe, TIER_CFG)
    out = {"signal_date": signal_date, "capital": capital,
           "universe": universe, "tiers": {}}
    for tier in (tiers or list(base)):
        if tier not in base:
            continue
        cfg = tier_cfg(tier, universe)
        gate = tier_make_gate(cal, cfg["gate"], cfg["ma"]) \
            if cfg.get("gate") else None
        on = bool(gate[d]) if gate is not None else True
        _base = tier_rank_base(codes, universe)
        score = tier_make_score(feat, cfg["score"], cfg.get("mom_w"),
                                base=_base)
        m = tier_universe_mask(codes, cfg.get("universe", "all"))
        if apply_perms:
            m = m & np.array([pick_allowed(c, info.get(c, ("", ""))[1])
                              for c in codes])
        ok = (np.isfinite(C[:, d]) & (C[:, d] > _TIER_MIN_PRICE)
              & (feat["barcount"][:, d] >= _TIER_MIN_BARS)
              & (feat["amt20"][:, d] >= _TIER_MIN_AMOUNT) & m
              & np.isfinite(score[:, d]) & ~risky)
        cand = np.nonzero(ok)[0]
        order = cand[np.argsort(-score[cand, d], kind="stable")]
        picks = []
        per = capital / cfg["top"] if on else 0.0
        for k in order:
            if len(picks) >= cfg["top"]:
                break
            px = float(C[k, d])
            lots = int(per / (px * 100)) if per > 0 and px > 0 else 0
            v20 = float(feat["vol20"][k, d]) \
                if np.isfinite(feat["vol20"][k, d]) else None
            bkey = "beta60_sh" if cfg["score"] == "beta_sh" else "beta60"
            picks.append({
                "code": str(codes[k]), "name": names.get(codes[k], ""),
                "industry": info.get(codes[k], ("", ""))[1],
                "price": px, "score": float(score[k, d]),
                "vol20": v20,
                "stop_ref": (px * (1 - 2.0 * v20)) if v20 else None,
                "beta60": float(feat[bkey][k, d])
                if np.isfinite(feat[bkey][k, d]) else None,
                "lots": lots, "cost": lots * 100 * px,
            })
        out["tiers"][tier] = {"gate_on": on, "cfg": cfg, "picks": picks}
        if on and picks and picks[0]["cost"] > 0:
            total = sum(p["cost"] for p in picks)
            out["tiers"][tier]["suggested_cost"] = total
    return out


def tier_report_text(capital=100000.0, tiers=None, universe="all"):
    """GUI/CLI 共用：最新目标持仓 + 闸门状态的文本报告。"""
    p = tier_latest_picks(capital=capital, tiers=tiers, universe=universe)
    uni_name = UNIVERSE_NAME.get(universe, universe)
    lines = [f"v6.1.2 三档组合 · {uni_name} · 信号日 {p['signal_date']} · "
             f"建议资金 {capital:,.0f}",
             "口径：T-1 信号 → 下一交易日收盘成交；整手/费用/涨跌停/退市已计入",
             "荐股权限（设置内配置，空=全部）：已按板块/行业过滤",
             ""]
    for tier, d in p["tiers"].items():
        cfg = d["cfg"]
        flag = "在场" if d["gate_on"] else "空仓（闸门关闭→持现金）"
        lines.append(f"【{tier}】{cfg['score']} · top{cfg['top']} · "
                     f"{cfg['reb']}日调仓 · 闸门 {cfg['gate']} MA{cfg['ma']}"
                     f" → {flag}")
        if not d["gate_on"]:
            lines.append("")
            continue
        lines.append(f"  {'代码':<9}{'名称':<10}{'现价':>8}{'手数':>6}"
                     f"{'金额':>10}{'分数':>8}{'20日波动':>9}{'β60':>7}"
                     f"{'参考止损':>9}")
        for x in d["picks"]:
            lines.append(
                f"  {x['code']:<9}{x['name'][:8]:<10}{x['price']:>8.2f}"
                f"{x['lots']:>6}{x['cost']:>10.0f}{x['score']:>8.3f}"
                f"{(x['vol20']*100 if x['vol20'] is not None else 0):>8.1f}%"
                f"{(x['beta60'] if x['beta60'] is not None else 0):>7.2f}"
                f"{(x['stop_ref'] if x['stop_ref'] else 0):>9.2f}")
        if d.get("suggested_cost"):
            lines.append(f"  合计约 {d['suggested_cost']:,.0f} 元"
                         f"（{d['suggested_cost']/capital*100:.0f}% 仓位，"
                         f"买不起的票自动跳过）")
        lines.append(f"  出局规则（回测同口径）：跌出 Top{cfg['top']} / "
                     f"闸门关闭 / 退市；预计持有 ~{cfg['reb']} 个交易日；"
                     f"参考止损=现价−2×20日波动（仅风险提示，回测未用）")
        lines.append("")
    lines.append("注：历史统计研究，不构成投资建议。")
    return "\n".join(lines)




# ================= AI 客户端（OpenAI 兼容：DeepSeek/智谱/opencode 等） =================

AI_SYSTEM_PROMPT = (
    "你是专业A股短线分析师。必须给出明确、果断的结论：直接说买/卖/观望"
    "（或加仓/持有/减仓），给出唯一首选方向、具体价位区间与建议仓位；"
    "禁止模棱两可、禁止罗列所有可能性。结论先行，再用不超过3条核心依据支撑，"
    "最后一行给风险提示。回答简洁，不写套话。")

AI_CACHE_MAX = 24          # 单股缓存对话条数上限（含首条数据上下文）

# 自有客户端标识：opencode zen 等网关要求非通用 HTTP 库 UA（否则 Cloudflare
# 以 error code 1010 拦截），并推荐以客户端名标识
AI_UA = f"stock-analyzer/{APP_VERSION}"


def _ai_session_id(*parts) -> str:
    """稳定会话 ID（请求头 x-opencode-session）：opencode zen 据此做路由与
    提示词缓存；同股同模型跨请求复用，换股/换模型/换用途自然区分。"""
    import hashlib
    raw = "stock-analyzer|" + "|".join(str(p) for p in parts)
    return "sa-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _ai_chat_url(base_url="") -> str:
    base = _normalize_ai_base(base_url or AI_BASE_URL)
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _ai_models_url(base_url="") -> str:
    base = _normalize_ai_base(base_url or AI_BASE_URL)
    if base.endswith("/chat/completions"):
        base = base[:-len("/chat/completions")]
    if base.endswith("/models"):
        return base
    return base + "/models"


def _ai_http_json(url, payload=None, api_key="", timeout=60, session=""):
    """OpenAI 兼容请求：代理失败自动回退直连；429/5xx 重试一次。
    session 非空时携带 x-opencode-session（opencode zen 必需，缺省报 400
    MissingSessionID；其他平台忽略该头）。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") \
        if payload is not None else None

    def one(opener):
        hdr = {"Content-Type": "application/json",
               "User-Agent": AI_UA,
               "Authorization": f"Bearer {api_key}"}
        if session:
            hdr["x-opencode-session"] = session
        req = urllib.request.Request(url, data=data, headers=hdr)
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    openers = ([_PROXY_OPENER]
               if _PROXY_OPENER is not None and not _proxy_dead() else []) \
        + [urllib.request.build_opener()]
    last = None
    for attempt in range(2):
        for opener in openers:
            try:
                return one(opener)
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8")[:200]
                except Exception:
                    pass
                if e.code == 401:
                    raise RuntimeError("API Key 无效 (401)，请检查设置中的"
                                       " Key 与接口地址是否匹配")
                last = RuntimeError(f"HTTP {e.code}: {detail or e.reason}")
                if e.code in (429, 500, 502, 503, 504):
                    continue        # 换通道/重试
                raise last from e
            except urllib.error.URLError as e:
                last = RuntimeError(f"网络错误: {e.reason}")
                continue            # 代理失败回退直连
        if attempt == 0:
            time.sleep(1.5)
    raise last or RuntimeError("AI 请求失败")


def fetch_ai_models(api_key: str, base_url: str = "", timeout: int = 20) -> list:
    """获取 OpenAI 兼容接口模型列表（GET {base}/models）。"""
    d = _ai_http_json(_ai_models_url(base_url), None, api_key, timeout)
    items = []
    if isinstance(d, dict):
        items = d.get("data") or d.get("models") or []
    out = []
    for it in items if isinstance(items, list) else []:
        mid = it.get("id") if isinstance(it, dict) else it
        if mid:
            out.append(str(mid))
    return sorted(set(out))


def deepseek_chat(api_key: str, prompt: str, model=None, timeout: int = 90,
                  session=""):
    """单轮调用 OpenAI 兼容 chat 接口（纯标准库）。model 缺省用 AI_MODEL。"""
    return _deepseek_chat(api_key, [{"role": "user", "content": prompt}],
                          model, timeout, session=session)


def _deepseek_chat(api_key, messages, model=None, timeout=90, session=""):
    """多轮调用 OpenAI 兼容 chat 接口。messages 为 [{role,content},...]，
    首条 user 消息应携带完整共享数据上下文，后续追问只追加新问题，
    从而复用同一份数据（不重复拼装）。model 缺省用 ini 配置的 AI_MODEL。
    session 见 _ai_session_id（opencode zen 必需）。"""
    payload = {
        "model": model or AI_MODEL,
        "messages": [{"role": "system", "content": AI_SYSTEM_PROMPT}]
                    + list(messages),
        "temperature": 0.3,
    }
    d = _ai_http_json(_ai_chat_url(), payload, api_key, timeout,
                      session=session)
    try:
        return d["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"接口返回格式异常: {str(d)[:200]}")


def _ai_trim_msgs(msgs, keep=None):
    """多轮缓存截断：保留首条数据上下文 + 最近 keep 条（默认 AI_CACHE_MAX）。"""
    keep = AI_CACHE_MAX if keep is None else keep
    if len(msgs) <= keep + 1:
        return list(msgs)
    return [msgs[0]] + list(msgs[-keep:])


def _ai_prompt_hash(text: str) -> str:
    import hashlib
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()[:12]


def _ai_cache_key(code, model=""):
    return f"ai:{code}:{model or AI_MODEL}"


def ai_session_load(code, model=""):
    """从 SQLite meta 读取缓存的对话（JSON），无则返回 None。"""
    try:
        with db_conn() as conn:
            row = conn.execute("select value from meta where key=?",
                               (_ai_cache_key(code, model),)).fetchone()
        if row:
            d = json.loads(row[0])
            if isinstance(d, dict) and isinstance(d.get("msgs"), list):
                return d
    except Exception:
        log.exception("读取AI对话缓存失败")
    return None


def ai_session_save(code, msgs, prompt_hash="", model=""):
    """把对话缓存写入 SQLite meta（截断后），失败不影响主流程。"""
    try:
        with db_conn(commit=True) as conn:
            conn.execute(
                "insert or replace into meta(key,value) values(?,?)",
                (_ai_cache_key(code, model),
                 json.dumps({"hash": prompt_hash,
                             "msgs": _ai_trim_msgs(msgs),
                             "ts": time.strftime("%Y-%m-%d %H:%M")},
                            ensure_ascii=False)))
    except Exception:
        log.exception("写入AI对话缓存失败")


def ai_market_brief():
    """给AI的市场环境简报：指数均线位置/近段涨跌 + 三档闸门状态（单次DB查询）。"""
    series = {}
    try:
        with db_conn() as conn:
            for code in ("sh000001", "sz399006"):
                rows = conn.execute(
                    "select date,close from daily_bars where code=? "
                    "order by date desc limit 90", (code,)).fetchall()
                series[code] = [(d, c) for d, c in reversed(rows) if c]
    except Exception:
        log.exception("AI市场简报失败")
    lines = ["市场环境（截至最新交易日收盘）："]
    for code, name in (("sh000001", "上证指数"), ("sz399006", "创业板指")):
        rows = series.get(code) or []
        if len(rows) < 61:
            lines.append(f"· {name}：数据不足")
            continue
        cl = [c for _, c in rows]
        last = cl[-1]
        a20 = sum(cl[-20:]) / 20
        a60 = sum(cl[-60:]) / 60
        r20 = last / cl[-21] - 1.0
        r60 = last / cl[-61] - 1.0
        lines.append(
            f"· {name} {rows[-1][0]} 收{last:.2f}；MA20 {a20:.2f}"
            f"（{'上方' if last > a20 else '下方'}）、MA60 {a60:.2f}"
            f"（{'上方' if last > a60 else '下方'}）；近20日{r20*100:+.1f}%、"
            f"近60日{r60*100:+.1f}%")
    gates = []
    for tier, cfg in TIER_CFG.items():
        rows = series.get(cfg.get("gate")) or []
        ma_w = cfg.get("ma") or 20
        if len(rows) < ma_w + 1:
            gates.append(f"{tier}=数据不足")
            continue
        cl = [c for _, c in rows]
        ma_v = sum(cl[-ma_w:]) / ma_w
        gates.append(f"{tier}{'开(可持仓)' if cl[-1] > ma_v else '关(空仓)'}")
    lines.append("· 三档闸门（T-1）：" + "；".join(gates))
    return "\n".join(lines)


def ai_choose_tier(model="", pref="均衡", timeout=60):
    """AI 在三档内选一档（按市场环境+用户风险偏好），失败回退 pref。
    返回 (tier, reason)。"""
    pref = pref if pref in TIER_CFG else "均衡"
    key = get_ai_key()
    if not key:
        return pref, "未配置 API Key，按配置风险偏好回退"
    prompt = (
        "你是量化组合风控官。下面是当前市场环境与三档策略定义：\n"
        f"{ai_market_brief()}\n\n"
        "三档策略：\n"
        "· 稳健：全A动量+低波Top20，20日调仓，上证MA20闸门\n"
        "· 均衡：全A动量+低波Top20，10日调仓，上证MA20闸门\n"
        "· 激进：创业板高βTop5，10日调仓，创业板指MA60闸门\n\n"
        f"用户风险偏好：{pref}（作为默认与锚定）。\n"
        "请判断当前市场环境最适合哪一档；可以偏离偏好，但必须给出一句理由。\n"
        "只输出一行严格 JSON（不要代码块、不要多余文字）："
        '{"tier": "稳健|均衡|激进", "reason": "不超过40字"}')
    try:
        text = deepseek_chat(key, prompt, model=model or AI_MODEL,
                             timeout=timeout,
                             session=_ai_session_id("tier", model or AI_MODEL))
        m = re.search(r'"tier"\s*:\s*"([^"]+)"', text or "")
        tier = m.group(1).strip() if m else ""
        r = re.search(r'"reason"\s*:\s*"([^"]*)"', text or "")
        reason = (r.group(1).strip() if r else "") or "AI 判断"
        if tier in TIER_CFG:
            return tier, reason
        return pref, f"AI 输出无法解析，回退 {pref}"
    except Exception as e:
        return pref, f"AI 选档失败，回退 {pref}（{e}）"

# ==================== 以下为 CLI 专属 ====================

def build_payload(full, res):
    q, tp, pred = res["quote"], res["t_pred"], res["pred"]
    return {
        "source": "stock_predict_cli",
        "code": full,
        "name": q["name"],
        "snapshot_time": q["time"],
        "price": q["price"],
        "prev_close": res["prev_close"],
        "gap_today_pct": round(res["gap_today"], 2),
        "market_pct": round(res["idx_chg_today"], 2)
        if res["idx_chg_today"] is not None else None,
        "volume_regime": res["cur_regime"],
        "sector": res["sector_name"],
        "sector_pct": round(res["sector_chg_today"], 2)
        if res["sector_chg_today"] is not None else None,
        "t_pred": {
            "close_p50": tp["cl"][50], "close_p10": tp["cl"][10],
            "close_p90": tp["cl"][90],
            "up_prob": round(tp["up_prob"] * 100, 1),
        },
        "t5_pred": ({
            "close_p50": t5["cl"][50], "close_p25": t5["cl"][25],
            "close_p75": t5["cl"][75],
            "up_prob": round(t5["up_prob"] * 100, 1), "n": t5["n"],
        } if (t5 := tp.get("t5")) else None),
        "next_day_pred": pred,
        "signals": [
            {"date": d, "type": t, "reason": x}
            for _, d, t, x in res["signals"][-6:]
        ],
        "filter_note": res["filter_note"],
        "disclaimer": DISCLAIMER,
    }


def push_report(full, res):
    """把分析报告 JSON 推到 Pi 的 ai-quant 收件箱（SSH 免密需已配置）。

    PUSH_HOST / PUSH_USER 可经环境变量覆盖，默认推到 Orangepi ai-quant。"""
    import os as _os
    push_user = _os.environ.get("PUSH_USER", "orangepi")
    push_host = _os.environ.get("PUSH_HOST", "192.168.3.28")
    inbox_dir = _os.environ.get(
        "INBOX_DIR", "~/ai-quant/memory/inbox")
    target = f"{push_user}@{push_host}"
    payload = build_payload(full, res)
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        fname = ("stock_%s_%s.json"
                 % (full, time.strftime("%Y%m%d")))
        subprocess.run(["ssh", target, f"mkdir -p {inbox_dir}"],
                       check=True, timeout=20, capture_output=True)
        r = subprocess.run(
            ["scp", tmp, f"{target}:{inbox_dir}/{fname}"],
            timeout=30, capture_output=True, text=True)
        if r.returncode == 0:
            print(f"[push] 已推送 -> {target}:{inbox_dir}/{fname}")
        else:
            print(f"[push] 推送失败: {r.stderr.strip()}")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ==================== 全A研究报告跑批（--research） ====================

def _rank_ic(a, b):
    """Spearman 秩相关（numpy 实现，无 scipy 依赖）。"""
    import numpy as _np
    a, b = _np.asarray(a, float), _np.asarray(b, float)
    m = ~( _np.isnan(a) | _np.isnan(b))
    a, b = a[m], b[m]
    if len(a) < 30:
        return None
    ra = _np.argsort(_np.argsort(a)).astype(float)
    rb = _np.argsort(_np.argsort(b)).astype(float)
    ra -= ra.mean(); rb -= rb.mean()
    den = (_np.sqrt((ra ** 2).sum()) * _np.sqrt((rb ** 2).sum()))
    return float((ra * rb).sum() / den) if den > 0 else None


def _research_daily_scores(rows):
    """每日多维评分（无冷却/无筹码，跨股IC用）。返回 [score,...]（与rows对齐）。"""
    closes = [r["close"] for r in rows]
    dif, dea, _ = calc_macd(closes)
    k_, d_, _ = calc_kdj(rows)
    r6 = calc_rsi(closes, 6)
    _, b_up, b_low = calc_boll(closes)
    pdi_a, mdi_a, adx_a = calc_adx(rows)
    ma20 = sma_period(closes, 20)
    vols = [r.get("vol") or 0.0 for r in rows]
    out = []
    for i in range(len(rows)):
        if None in (dif[i], dea[i], dif[i - 1], dea[i - 1]):
            out.append(None)
            continue
        sc = 0.0
        sc += CFG.IND_W["MACD"] * (
            2 if (dif[i - 1] <= dea[i - 1] and dif[i] > dea[i])
            else -2 if (dif[i - 1] >= dea[i - 1] and dif[i] < dea[i])
            else 1 if dif[i] > dea[i] else -1)
        if None not in (k_[i], d_[i], k_[i - 1], d_[i - 1]):
            sc += CFG.IND_W["KDJ"] * (
                2 if (k_[i - 1] <= d_[i - 1] and k_[i] > d_[i] and k_[i] < 45)
                else -2 if (k_[i - 1] >= d_[i - 1] and k_[i] < d_[i]
                            and k_[i] > 65)
                else 1 if k_[i] > d_[i] else -1)
        if r6[i] is not None and r6[i - 1] is not None:
            sc += CFG.IND_W["RSI"] * (
                2 if (r6[i - 1] < 20 and r6[i] >= 20)
                else -2 if (r6[i - 1] > 80 and r6[i] <= 80)
                else 1 if r6[i] < 30 else -1 if r6[i] > 70 else 0)
        c, cp = closes[i], closes[i - 1]
        v5 = sum(vols[max(0, i - 5):i]) / max(1, min(5, i))
        vr = vols[i] / v5 if v5 > 0 else 0.0
        sc += CFG.IND_W["量价"] * (1 if (vr > 1.5 and c > cp)
                                  else -1 if (vr > 1.5 and c < cp) else 0)
        if ma20[i] and ma20[i - 1]:
            sc += CFG.IND_W["MA20"] * (
                1 if (c > ma20[i] and ma20[i] > ma20[i - 1])
                else -1 if (c < ma20[i] and ma20[i] < ma20[i - 1]) else 0)
        if None not in (b_up[i], b_low[i]):
            sc += CFG.IND_W["布林带"] * (1 if c < b_low[i]
                                        else -1 if c > b_up[i] else 0)
        a_i, p_i, m_i = adx_a[i], pdi_a[i], mdi_a[i]
        if None not in (a_i, p_i, m_i) and a_i >= 20:
            sc += CFG.IND_W["ADX"] * (1 if p_i > m_i else -1)
        out.append(sc)
    return out


def _research_one(bars):
    """单只股票全算法研究计算（多进程worker，顶层函数可pickle）。
    返回 {key: (ic, wr, ann, mdd, trades), ..., "bh": (ann, mdd)}。"""
    import datetime as _dt
    try:
        out = {}
        closes = [r["close"] for r in bars]
        rets = logret(closes)
        if len(rets) < 60:
            return None
        nxt = closes[1:]

        def _pack(key, ic=None, bt=None, wr=None):
            out[key] = (ic,
                        bt["winrate"] if bt else wr,
                        bt["ann"] if bt else None,
                        bt["mdd"] if bt else None,
                        bt["trades"] if bt else None)

        # 状态序列（日线IC用）
        dif_s, dea_s, _ = calc_macd(closes)
        kd, dd, _ = calc_kdj(bars)
        r6s = calc_rsi(closes, 6)
        _, bu_s, bl_s = calc_boll(closes)
        ma60s = sma_period(closes, 60)
        states = {
            "macd": [1 if (a is not None and b is not None and a > b)
                     else -1 for a, b in zip(dif_s, dea_s)],
            "kdj": [1 if (a is not None and b is not None and a > b)
                    else -1 for a, b in zip(kd, dd)],
            "rsi": [(-1 if v is None else (1 if v < 50 else -1))
                    for v in r6s],
            "boll": [(1 if (lo is not None and cl < lo)
                      else -1 if (up is not None and cl > up) else 0)
                     for cl, up, lo in zip(closes, bu_s, bl_s)],
            "ma_trend": [1 if (m is not None and c > m) else -1
                         for c, m in zip(closes, ma60s)],
            "composite": _research_daily_scores(bars),
        }
        sigs_all = {"macd": _sig_macd(bars), "kdj": _sig_kdj(bars),
                    "rsi": _sig_rsi(bars), "boll": _sig_boll(bars),
                    "ma_trend": _sig_ma_trend(bars)}
        rp0 = CFG.RISK_PARAMS["稳健"]
        for key in ("macd", "kdj", "rsi", "boll", "ma_trend"):
            ic = _rank_ic(states[key][:-1], nxt)
            bt = _bt_events(bars, sigs_all[key], rp0) if sigs_all[key] \
                else None
            _pack(key, ic=ic, bt=bt)
        # composite：状态IC + 三档事件回测
        stc = states["composite"]
        ic = _rank_ic([s for s in stc[:-1] if s is not None],
                      [r for s, r in zip(stc[:-1], nxt) if s is not None])
        _pack("composite", ic=ic)
        for mode, rp in CFG.RISK_PARAMS.items():
            sg_ = _composite_signals(bars, rp, use_chips=False)
            bt = _bt_events(bars, sg_, rp) if sg_ else None
            _pack(f"composite:{mode}", bt=bt)
        # L1形态：逐步匹配 up_prob 的 IC 与方向命中率
        W = W_WINDOW
        ups, nxts = [], []
        for i in range(W, len(rets) - W + 1, 5):
            cur = znorm(rets[i - W:i])
            d_arr = _px_distances(rets[:i], cur, W)
            cand = [d_arr[k] for k in range(len(d_arr))
                    if k + W <= i - W]
            if len(cand) < 6:
                continue
            cand.sort()
            idxs = sorted(range(len(d_arr)),
                          key=lambda k2: d_arr[k2])[:CFG.TOPK]
            tot = ups_n = 0
            for k2 in idxs:
                j = k2 + W
                if j < len(bars) - 1:
                    tot += 1
                    ups_n += 1 if bars[j + 1]["close"] > bars[j]["close"] \
                        else 0
            if tot >= 5:
                ups.append(ups_n / tot)
                nxts.append(bars[i + 1]["close"] / bars[i]["close"] - 1)
        if len(ups) >= 20:
            ic = _rank_ic(ups, nxts)
            dirhit = sum(1 for u, r in zip(ups, nxts)
                         if (u > 0.5) == (r > 0)) / len(ups)
            out["l1_pattern"] = (ic, dirhit, None, None, None)
        # 买入持有基准
        years = max((_dt.date.fromisoformat(bars[-1]["date"])
                     - _dt.date.fromisoformat(bars[0]["date"])
                     ).days / 365.25, 0.5)
        tot_ret = bars[-1]["close"] / bars[0]["close"] - 1
        bh_ann = (1 + tot_ret) ** (1 / years) - 1 if tot_ret > -1 else -1.0
        peak = mdd = eq = 0.0
        eq = 1.0
        for a, b in zip(closes, closes[1:]):
            eq *= b / a
            peak = max(peak, eq)
            if peak > 0:
                mdd = min(mdd, eq / peak - 1)
        out["bh"] = (bh_ann, mdd)
        return out
    except Exception:
        log.exception("research_one 失败")
        return None


def run_full_a_research(min_bars=400, limit=0, progress=print):
    """全A样本研究：逐股回测7类算法 + 信号IC，跨股票聚合统计。

    返回 {"meta": {...}, "algos": {key: {...}}, "buyhold": {...}}。
    指标口径：
    - IC = 每股 spearman(信号状态T, 次日收益率T+1) 的中位数（跨股聚合）
    - 胜率/年化/回撤 = 每股事件回测（信号日收盘成交+ATR止损）后取中位数
    - 买入持有为同区间基准（中位年化/中位回撤）"""
    with db_conn() as conn:
        codes = [r[0] for r in conn.execute(
            "SELECT code FROM daily_bars GROUP BY code "
            "HAVING COUNT(*) >= ? AND code NOT LIKE 'bj%'",
            (min_bars,)).fetchall()]
    if limit:
        codes = codes[:limit]
    if progress:
        progress(f"研究样本：{len(codes)} 只（≥{min_bars}根日K）")
    algo_stats = {}       # key -> dict of lists
    bh_ann, bh_mdd = [], []
    ALGOS = (("macd", "MACD金叉死叉"), ("kdj", "KDJ金叉死叉"),
             ("rsi", "RSI超买超卖"), ("boll", "布林带回归"),
             ("ma_trend", "MA20/60趋势"), ("composite", "多维评分"),
             ("l1_pattern", "L1形态up_prob"))

    def _acc(key):
        return algo_stats.setdefault(key, {
            "ic": [], "wr": [], "ann": [], "mdd": [], "trades": []})

    # ---- 多进程分块跑全A ----
    from concurrent.futures import ProcessPoolExecutor
    CH = 400
    n_done = [0]
    for ci in range(0, len(codes), CH):
        chunk = codes[ci:ci + CH]
        with db_conn() as conn:
            ph = ",".join("?" for _ in chunk)
            rws = conn.execute(
                f"SELECT code,date,open,high,low,close,vol FROM ("
                f" SELECT *, ROW_NUMBER() OVER (PARTITION BY code "
                f" ORDER BY date DESC) rn FROM daily_bars "
                f" WHERE code IN ({ph})"
                f") WHERE rn<=1000 ORDER BY code, date", chunk).fetchall()
        by = {}
        for c, d, o, h, l, cl, v in rws:
            by.setdefault(c, []).append(
                {"date": d, "open": o, "high": h, "low": l,
                 "close": cl, "vol": v or 0.0})
        bars_list = [b for b in by.values() if len(b) >= min_bars]
        del rws, by
        try:
            with ProcessPoolExecutor(max_workers=6) as ex:
                for res in ex.map(_research_one, bars_list):
                    if not res:
                        continue
                    n_done[0] += 1
                    for key, vals in res.items():
                        if key == "bh":
                            if vals[0] is not None:
                                bh_ann.append(vals[0])
                                bh_mdd.append(vals[1])
                            continue
                        a = _acc(key)
                        ic, wr, ann, mdd, trades = vals
                        if ic is not None:
                            a["ic"].append(ic)
                        if wr is not None:
                            a["wr"].append(wr)
                        if ann is not None:
                            a["ann"].append(ann)
                        if mdd is not None:
                            a["mdd"].append(mdd)
                        if trades is not None:
                            a["trades"].append(trades)
        except Exception:
            log.exception("研究多进程失败，退回单进程")
            for bars in bars_list:
                res = _research_one(bars)
                if not res:
                    continue
                for key, vals in res.items():
                    if key == "bh":
                        if vals[0] is not None:
                            bh_ann.append(vals[0])
                            bh_mdd.append(vals[1])
                    else:
                        a = _acc(key)
                        ic, wr, ann, mdd, trades = vals
                        if ic is not None:
                            a["ic"].append(ic)
                        if wr is not None:
                            a["wr"].append(wr)
                        if ann is not None:
                            a["ann"].append(ann)
                        if mdd is not None:
                            a["mdd"].append(mdd)
                        if trades is not None:
                            a["trades"].append(trades)
        if progress:
            progress(f"研究进度 {min(ci + CH, len(codes))}/{len(codes)} "
                     f"(有效{n_done[0]})")

    def _med(lst):
        if not lst:
            return None
        s = sorted(lst)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    out = {"meta": {"n_codes": n_done[0], "min_bars": min_bars,
                    "ts": time.strftime("%Y-%m-%d %H:%M")}, "algos": {}}
    for key, label in ALGOS:
        a = algo_stats.get(key, {})
        out["algos"][key] = {
            "label": label,
            "n_ic": len(a.get("ic", [])),
            "ic_med": _med(a.get("ic", [])),
            "ic_pos": (sum(1 for x in a.get("ic", []) if x > 0)
                       / len(a["ic"])) if a.get("ic") else None,
            "wr_med": _med(a.get("wr", [])),
            "ann_med": _med(a.get("ann", [])),
            "mdd_med": _med(a.get("mdd", [])),
            "trades_med": _med(a.get("trades", [])),
        }
    for mode in CFG.RISK_PARAMS:
        a = algo_stats.get(f"composite:{mode}", {})
        out["algos"][f"composite:{mode}"] = {
            "label": f"多维评分·{mode}",
            "n_ic": len(a.get("ic", [])), "ic_med": None, "ic_pos": None,
            "wr_med": _med(a.get("wr", [])),
            "ann_med": _med(a.get("ann", [])),
            "mdd_med": _med(a.get("mdd", [])),
            "trades_med": _med(a.get("trades", [])),
        }
    out["buyhold"] = {"ann_med": _med(bh_ann), "mdd_med": _med(bh_mdd),
                      "n": len(bh_ann)}
    return out


def main():
    try:    # GBK 控制台无法编码的字符（如 ⚠️）降级为 ?，防打印崩溃
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    argv = sys.argv[1:]
    do_push = "--push" in argv
    argv = [a for a in argv if a != "--push"]
    do_refresh = "--refresh-cache" in argv
    argv = [a for a in argv if a != "--refresh-cache"]
    do_backfill = "--backfill" in argv
    argv = [a for a in argv if a != "--backfill"]
    do_clean = "--clean" in argv
    argv = [a for a in argv if a != "--clean"]
    do_research = "--research" in argv
    argv = [a for a in argv if a != "--research"]
    do_v4 = "--v4" in argv
    argv = [a for a in argv if a != "--v4"]
    do_tiers = "--tiers" in argv
    argv = [a for a in argv if a != "--tiers"]
    do_tiers_bt = "--tiers-backtest" in argv
    argv = [a for a in argv if a != "--tiers-backtest"]
    do_picks_bt = "--picks-backtest" in argv
    argv = [a for a in argv if a != "--picks-backtest"]
    picks_seg = "full"
    if "--picks-seg" in argv:
        _i = argv.index("--picks-seg")
        if _i + 1 < len(argv) and not argv[_i + 1].startswith("-"):
            picks_seg = argv[_i + 1]
            del argv[_i:_i + 2]
        else:
            del argv[_i]
    tier_filter = []
    while "--tier" in argv:
        _i = argv.index("--tier")
        if _i + 1 < len(argv) and not argv[_i + 1].startswith("-"):
            tier_filter.append(argv[_i + 1])
            del argv[_i:_i + 2]
        else:
            del argv[_i]
    universe = "all"
    if "--universe" in argv:
        _i = argv.index("--universe")
        if _i + 1 < len(argv) and argv[_i + 1] in ("all", "main", "etf",
                                                   "all_etf"):
            universe = argv[_i + 1]
            del argv[_i:_i + 2]
        else:
            del argv[_i]
    do_ai_tier = "--ai-tier" in argv
    argv = [a for a in argv if a != "--ai-tier"]
    do_refresh_etf = "--refresh-etf" in argv
    argv = [a for a in argv if a != "--refresh-etf"]
    v4_limit = 0
    if "--v4-limit" in argv:
        _i = argv.index("--v4-limit")
        try:
            v4_limit = int(argv[_i + 1])
            del argv[_i:_i + 2]
        except (ValueError, IndexError):
            del argv[_i]
    if do_refresh_etf:
        n = refresh_etf_codes(progress=print)
        print(f"ETF 代码表: {n} 只")
        st = backfill_etf_history(progress=print, workers=6)
        print(f"ETF 回填: 待处理{st.get('todo', 0)} 成功{st.get('ok', 0)} "
              f"失败{st.get('fail', 0)} 可用(≥200根){st.get('bar_ok', 0)}")
        if not argv:
            return
    if do_backfill and True:
        backfill_full_market(progress=print)
        if not argv:
            return
    if do_clean and True:
        st = clean_daily_db(fix=True, progress=print)
        print(f"清洗完成: 删除{st['deleted']}根 重拉{st['refetched']}只 "
              f"退市{st['delisted']}只 停牌{st['suspend']}只")
        if not argv:
            return
    if do_research and True:
        import json as _json
        r = run_full_a_research(progress=print)
        print("\n" + "=" * 72)
        print(f"全A研究 (n={r['meta']['n_codes']}只, "
              f"≥{r['meta']['min_bars']}根K)  {r['meta']['ts']}")
        print(f"{'算法':<18}{'IC中位':>8}{'IC>0占比':>9}{'胜率中位':>9}"
              f"{'年化中位':>9}{'回撤中位':>9}{'交易中位':>8}")
        for key, a in r["algos"].items():
            f = lambda v, m=100, d="%": "-" if v is None else f"{v * m:+.2f}{d}"
            print(f"{a['label']:<18}"
                  f"{f(a['ic_med'], 100, ''):>8}"
                  f"{f(a['ic_pos'], 100):>9}"
                  f"{f(a['wr_med']):>9}"
                  f"{f(a['ann_med']):>9}"
                  f"{f(a['mdd_med']):>9}"
                  f"{a['trades_med'] if a['trades_med'] is not None else '-':>8}")
        bh = r["buyhold"]
        print(f"{'买入持有(基准)':<18}{'-':>8}{'-':>9}{'-':>9}"
              f"{bh['ann_med']*100:+.2f}%{bh['mdd_med']*100:+.2f}%{'-':>8}")
        print("注：IC=信号状态与次日收益的spearman相关(跨股中位)；"
              "胜率/年化/回撤为每股事件回测后跨股中位（含样本内成分，"
              "实际选策略请用消融的训练/验证口径）")
        if not argv:
            return
    if do_v4 and True:
        r = run_v4_research(limit=v4_limit, progress=print)
        _v4_print_report(r)
        if not argv:
            return
    if do_tiers and True:
        if do_ai_tier:
            _conf = picks_conf()
            _tier, _why = ai_choose_tier(pref=_conf["risk_pref"])
            print(f"AI 选中档位：{_tier}（{_why}）")
            tier_filter = [_tier]
        print(tier_report_text(tiers=tier_filter or None, universe=universe))
        if not argv:
            return
    if do_tiers_bt and True:
        res = tier_eval(segment="full", tiers=tier_filter or None,
                        progress=print, universe=universe)
        _uni = "全A" if universe == "all" else "主板"
        print("\n" + "=" * 76)
        print(f"v6.1 三档回测（全期 2022-09 ~ 最新 · {_uni}，"
              f"相位平均，含全部费用）")
        for tier, m in res.items():
            b = m.get("bench") or {}
            print(f"[{tier}] {m['range'][0]} ~ {m['range'][1]}  "
                  f"年化 {m['ann']*100:+.1f}%  回撤 {m['mdd']*100:+.1f}%  "
                  f"Sharpe {(m['sharpe'] or 0):+.2f}  交易 {m['trades']}")
            if b.get("ann") is not None:
                print(f"    基准 {m['benchmark']}  年化 {b['ann']*100:+.1f}%  "
                      f"超额 {(m['excess_total'] or 0)*100:+.1f}pp  "
                      f"（相位区间 {m['phase_ann_min']*100:+.1f}% ~ "
                      f"{m['phase_ann_max']*100:+.1f}%）")
        print("=" * 76)
        print("注：历史统计研究，不构成投资建议。")
        if not argv:
            return
    if do_picks_bt and True:
        print(tier_picks_report_text(segment=picks_seg,
                                     tiers=tier_filter or None,
                                     universe=universe))
        if not argv:
            return
    if do_refresh and True:
        print("刷新缓存数据库（全市场代码表/分层）...")
        refresh_all_codes(print)
        print("完成。")
        if not argv:
            return
    if argv:
        code_in = " ".join(argv)
    else:
        try:
            code_in = input("请输入股票代码（如 002241 / 600519）：")
        except EOFError:
            return
    try:
        full = normalize_code(code_in)
    except ValueError as e:
        print(f"错误: {e}")
        sys.exit(1)

    print("\n拉取数据并计算中...")
    res = analyze(full, progress=print)
    q, tp, pred = res["quote"], res["t_pred"], res["pred"]

    print("=" * 68)
    print(f"{q['name']} ({res['full_code']})  快照 {q['time']}")
    if res.get("pre_open"):
        print(f"昨收 {res['prev_close']:.2f} | [未开盘·T日预测锚定昨收] "
              f"| 现价 {q['price']:.2f} "
              f"({(q['price']/res['prev_close']-1)*100:+.2f}%)")
    else:
        print(f"昨收 {res['prev_close']:.2f} | 今开 {q['open']:.2f} "
              f"(缺口 {res['gap_today']:+.2f}%) | 现价 {q['price']:.2f} "
              f"({(q['price']/res['prev_close']-1)*100:+.2f}%)"
              + ("  [含盘中实时bar]" if res["has_live"] else ""))
    idx_txt = (f"{res['idx_chg_today']:+.2f}%"
               if res["idx_chg_today"] is not None else "未知")
    vr_txt = (f"量比 {res['vr_now']:.2f}（{res['cur_regime']}）"
              if res["vr_now"] is not None else "数据不足")
    sec_txt = (f"{res['sector_name']} {res['sector_chg_today']:+.2f}%"
               if res["sector_name"] and res["sector_chg_today"] is not None
               else "未知")
    print(f"大盘(上证) {idx_txt} | 板块 {sec_txt} | 本股量能 {vr_txt}")
    print(f"市场状态: {res.get('phase', '未知')}")

    print("-" * 68)
    print(f"今日(T)预测 [锚定{res.get('anchor', '今开')}]")
    print(f"{'分位':<6}{'收盘':>10}{'最高':>10}{'最低':>10}")
    for p in (10, 25, 50, 75, 90):
        print(f"P{p:<5}{tp['cl'][p]:>10.2f}{tp['hi'][p]:>10.2f}{tp['lo'][p]:>10.2f}")
    print(f"开盘->收盘 上行概率 {tp['up_prob']*100:.0f}%   "
          f"有效样本 {res['src_n']}/{len(res['samples'])}"
          f"（{res['filter_note']}）")
    lv = res.get("levels") or []
    if len(lv) > 1:
        print("分层上行概率: " + " | ".join(
            f"{x['label']} {x['up_prob']*100:.0f}%(n={x['n']})" for x in lv)
             + "  [融合权重 L1 0.6 L2 0.3 L3 0.1]")
    if res.get("pool_note"):
        print(res["pool_note"])
    if res["has_live"] and res["clamped"]:
        print(f"[盘中实时修正] 已实现最高 {res['live_high']:.2f} / "
              f"最低 {res['live_low']:.2f}，已并入预测区间")

    print("-" * 68)
    print("-" * 68)
    print(f"{res.get('next_label', '次日(T+1)')}预测: 开{pred['open']:.2f} 收{pred['close']:.2f} "
          f"高{pred['high']:.2f} 低{pred['low']:.2f}")
    
    # 多日预测
    multi = res.get("multi_pred")
    if multi:
        print("-" * 68)
        print("多日预测趋势（基于相似样本统计分布）")
        print(f"{'周期':<8}{'收盘预测':>10}{'最高预测':>10}{'最低预测':>10}{'上行概率':>10}{'累计涨跌':>10}")
        for mp in multi:
            print(f"{mp['label']:<8}"
                  f"{mp['price_cl']:>10.2f}"
                  f"{mp['price_hi']:>10.2f}"
                  f"{mp['price_lo']:>10.2f}"
                  f"{mp['up_prob']*100:>9.0f}%"
                  f"{mp['cum_cl']*100:>+9.1f}%")

    cp_ = res.get("chips")
    if cp_:
        print("-" * 68)
        print("筹码参考")
        print(f"  平均成本 {cp_['avg_cost']:.2f} | 现价 {cp_['cur']:.2f} "
              f"| 获利盘 {cp_['profit']*100:.0f}%")
        if cp_["p5"] and cp_["p95"]:
            print(f"  90%筹码区间 {cp_['p5']:.2f} ~ {cp_['p95']:.2f}")
        lv_txt = []
        if cp_["sup"]:
            lv_txt.append(f"支撑位 {cp_['sup']:.2f}")
        if cp_["res"]:
            lv_txt.append(f"压力位 {cp_['res']:.2f}")
        if lv_txt:
            print("  " + " | ".join(lv_txt))

    act = res.get("action")
    if act:
        print("-" * 68)
        print("综合评估(买卖点)")
        if act.get("band_note"):
            print("  ◆ " + act["band_note"])
        for lab, sc, note in act["items"]:
            mark = "+" if sc > 0 else ("-" if sc < 0 else "·")
            print(f"  [{mark}] {lab}  {note}")
        print(f"  合计 {act['score']:+d} → {act['verdict']}")

    print("-" * 68)
    print("相似历史参考日期（含 量能/大盘 匹配）")
    print(f"{'T日':<12}{'T+1日':<12}{'次日涨跌':>10}  标记")
    for s in res["samples"]:
        mark = ""
        if s.get("regime") == res.get("cur_regime"):
            mark += "[量]"
        if s.get("idx_chg") is not None and abs(s["idx_chg"]) <= 0.8:
            mark += "[盘]"
        n1_cl = s.get("n1_cl")
        n1_cl_str = f"{n1_cl*100:>+8.2f}%" if n1_cl is not None else "N/A"
        print(f"{s['t_date']:<12}{s.get('n1_date', 'N/A'):<12}"
              f"{n1_cl_str:>10}  {mark}")

    print("-" * 68)
    sigs = res["signals"]
    print(f"近期买卖信号（每日一个，按优先级合并）")
    for i, day, typ, txt in sigs[-12:]:
        tag = "买" if typ == "BUY" else "卖"
        print(f"  {day} [{tag}] {txt}")
    if not sigs:
        print("  近期无")
    
    # 回测统计
    bt = res.get("bt_stats")
    if bt:
        print("-" * 68)
        print("回测统计（样本内全部信号）")
        print(f"  总交易 {bt['trades']} 笔 | 已平仓 {bt['closed']} 笔 | 盈利 {bt['wins']} 笔")
        if bt.get("winrate") is not None:
            print(f"  胜率 {bt['winrate']*100:.1f}% | 区间收益 {bt['total']*100:+.1f}%"
                  f" | 年化收益 {bt['ann']*100:+.1f}%"
                  f" | 最大回撤 {bt['mdd']*100:.1f}%")
        if bt.get("floating") is not None:
            print(f"  未平仓浮盈 {bt['floating']*100:+.1f}%")

    print("=" * 68)
    print(f"作者：{AUTHOR}  邮箱：{AUTHOR_EMAIL}  QQ：{AUTHOR_QQ}")
    print(DISCLAIMER)

    if do_push:
        print("-" * 68)
        push_report(full, res)


def _cleanup():
    """程序退出时清理线程池，避免僵尸线程。"""
    try:
        _SHARED_EX.shutdown(wait=False)
    except Exception:
        pass
    try:
        _BG_EX.shutdown(wait=False)
    except Exception:
        pass


atexit.register(_cleanup)


if __name__ == "__main__":
    main()
