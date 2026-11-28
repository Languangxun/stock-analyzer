#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sm701 云 API 接入（StockModel /platform，v7.5.21）

云端 sm701 概率预测服务（多市场，官方文档：<base>/docs）：

    Base URL   https://quant.tail6a6cc8.ts.net/platform
    POST /v1/predict   {"codes": ["sh600519", ...]} → results[].prob_up
    GET  /v1/balance   余额/限流（免费）
    GET  /health       探活（免费，无需鉴权）

**用途**：本地没有 sm70x 模型/数据时的兜底——用云端「最新数据时点」的概率
出 sm701 买卖点（历史段仍需本地模型；云端不发布历史概率序列，因此云模式
只补最新一日，不参与 A 股消融回测）。

配置优先级（高 → 低）：
    环境变量 SM701_API_KEY / SM701_API_URL（推荐，避免明文落盘）
    ini [predict] sm701_cloud_key / sm701_cloud_url
    默认 https://quant.tail6a6cc8.ts.net/platform（路径为空时自动补 /platform）

Key 形如 sk-sm-…（控制台创建，仅完整显示一次）；`sm701_cloud=0` 可显式停用。
计费 ¥0.10/次（成功才计费，失败不计费），普通用户 2 秒/次；本模块内置
**节流 + 结果缓存**（默认 300s，ini `[predict] sm701_cloud_ttl` 可调），
避免界面刷新重复扣费。

标的编码（与 quant 一致，见 /platform/docs §标的编码）：
    加密小时 BTCUSDT · 加密日线 crypto:BTC/USDT · A股 sh600519 ·
    港股 hk:00700 · 美股 us:AAPL · 期货 fut:RB0 · 场外基金 007301

安全：Key 只在请求头出现，日志/异常文案均不打印 Key（状态里只留掩码）。
"""
import configparser
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INI_PATH = os.path.join(ROOT, "stock_gui.ini")

DEFAULT_URL = "https://quant.tail6a6cc8.ts.net/platform"
ENV_KEY = "SM701_API_KEY"
ENV_URL = "SM701_API_URL"
UA = "stock-analyzer/sm701-cloud"

_LOCK = threading.RLock()
_STATE = {
    "last_call": 0.0,        # 上次 /v1/predict 时间（节流）
    "min_interval": 2.0,     # 普通用户 2 秒/次
    "cache": {},             # {code: (ts, rec)}
    "ini_mtime": None,
    "ini": {},
    "last_error": "",
}


# ---------------------------------------------------------------- 配置读取

def _ini_values():
    """读 ini [predict] 云相关键（按 mtime 缓存，保存后 reset_cache 即刷新）。"""
    try:
        mt = os.path.getmtime(INI_PATH)
    except OSError:
        return {}
    with _LOCK:
        if _STATE.get("ini_mtime") == mt:
            return dict(_STATE.get("ini") or {})
    vals = {}
    try:
        cp = configparser.ConfigParser()
        cp.read(INI_PATH, encoding="utf-8")
        if cp.has_section("predict"):
            for k in ("sm701_cloud", "sm701_cloud_key", "sm701_cloud_url",
                      "sm701_cloud_ttl"):
                vals[k] = (cp.get("predict", k, fallback="") or "").strip()
    except Exception:
        vals = {}
    with _LOCK:
        _STATE["ini_mtime"] = mt
        _STATE["ini"] = dict(vals)
    return vals


def api_key():
    """云 API Key：环境变量 > ini；都没有返回空串。"""
    v = (os.environ.get(ENV_KEY) or "").strip()
    if v:
        return v
    return _ini_values().get("sm701_cloud_key", "")


def key_source():
    """Key 来源：env / ini / 空。"""
    if (os.environ.get(ENV_KEY) or "").strip():
        return "env"
    return "ini" if _ini_values().get("sm701_cloud_key") else ""


def api_url():
    """云 API 地址（空路径自动补 /platform；非法 scheme 返回空串）。"""
    v = ((os.environ.get(ENV_URL) or "").strip()
         or _ini_values().get("sm701_cloud_url", "")
         or DEFAULT_URL).strip().rstrip("/")
    return _norm_url(v)


def _norm_url(v):
    """规范化 Base URL：补 scheme、去尾斜杠、空路径补 /platform。"""
    v = (v or "").strip().rstrip("/")
    if not v:
        return ""
    if not v.startswith(("http://", "https://")):
        v = "https://" + v
    try:
        parts = urllib.parse.urlsplit(v)
    except Exception:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    if parts.path in ("", "/"):
        v = v + "/platform"
    return v


def flag_on():
    """云 API 兜底开关（ini `sm701_cloud`，默认开；与是否填 Key 无关）。"""
    flag = (_ini_values().get("sm701_cloud") or "1").lower()
    return flag not in ("0", "off", "none", "false", "no")


def enabled():
    """云 API 是否启用：有 Key 且未被 `sm701_cloud=0` 显式停用。"""
    return flag_on() and bool(api_key())


def reset_cache():
    """清空节流/缓存/ini 快照（设置保存后调用，立即生效）。"""
    with _LOCK:
        _STATE["last_call"] = 0.0
        _STATE["min_interval"] = 2.0
        _STATE["cache"] = {}
        _STATE["ini_mtime"] = None
        _STATE["ini"] = {}
        _STATE["last_error"] = ""


def mask_key(key=None):
    """Key 掩码（状态/日志只显示这个）。"""
    k = api_key() if key is None else (key or "")
    if not k:
        return ""
    if len(k) <= 10:
        return "****"
    return k[:6] + "****" + k[-4:]


def _ttl():
    try:
        v = float(_ini_values().get("sm701_cloud_ttl") or 300)
    except (TypeError, ValueError):
        v = 300.0
    return max(0.0, min(v, 86400.0))


# ---------------------------------------------------------------- 编码映射

def code_for(full, hourly=False):
    """本项目代码 → 云 API 代码；不覆盖（如北交所）返回 None。

    hourly=True 时加密用裸 Ticker（sm701-h24 小时级：BTCUSDT）；
    默认日线口径（crypto:BASE/QUOTE），与本工程日K一致。
    """
    full = (full or "").strip().lower()
    if not full:
        return None
    if full.startswith("crypto"):
        body = full[6:].upper()
        base, quote = body, "USDT"
        for q in ("USDT", "USD", "BTC", "ETH"):
            if body.endswith(q) and len(body) > len(q):
                base, quote = body[:-len(q)], q
                break
        return (base + quote) if hourly else "crypto:%s/%s" % (base, quote)
    if full.startswith(("sh", "sz")):
        return full
    if full.startswith("bj"):          # 北交所：quant 库不覆盖
        return None
    if full.startswith("hk"):
        return "hk:" + full[2:].zfill(5)
    if full.startswith("us"):
        return "us:" + full[2:].upper()
    if full.startswith("fu"):
        return "fut:" + full[2:].upper()
    return None


# ---------------------------------------------------------------- HTTP

def _host_open_url(req, url, timeout):
    """复用宿主 `_open_url`（tailnet 强制直连/代理旁路）；无宿主时直连。"""
    fn = None
    try:
        from . import sm701_ext as _ext          # 包内导入
    except Exception:
        try:
            import sm701_ext as _ext             # 兜底（同目录脚本）
        except Exception:
            _ext = None
    if _ext is not None:
        fn = (getattr(_ext, "_HOST", None) or {}).get("_open_url")
    if callable(fn):
        return fn(req, url, timeout)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _call(method, path, body=None, timeout=25, key=None, url=None):
    base = _norm_url(url) or api_url()
    k = api_key() if key is None else (key or "").strip()
    if not base:
        raise RuntimeError("sm701 云 API 地址无效")
    if not k:
        raise RuntimeError("sm701 云 API Key 未配置")
    data = None
    headers = {"Authorization": "Bearer " + k, "User-Agent": UA,
               "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    raw = _host_open_url(req, req.full_url, timeout)
    txt = raw.decode("utf-8", "replace")
    try:
        return json.loads(txt) if txt.strip() else {}
    except ValueError:
        raise RuntimeError("sm701 云 API 返回非 JSON（%s）: %s"
                           % (path, txt[:120]))


def _friendly(exc):
    """HTTPError → 中文可读异常（不打印 Key）。"""
    if not isinstance(exc, urllib.error.HTTPError):
        return exc
    code = exc.code
    typ = msg = ""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
        err = body.get("error") or {}
        typ = str(err.get("type") or "")
        msg = str(err.get("message") or "")
    except Exception:
        pass
    hint = {
        401: "Key 无效/已吊销，请在控制台重新创建",
        402: "余额不足，请到控制台充值后重试",
        400: "参数或标的编码错误",
        404: "接口路径不存在（检查云 API 地址）",
        429: "触发限流（普通用户 2 秒/次）",
        503: "云端模型/数据暂不可用（不计费）",
    }.get(code, "")
    parts = ["sm701 云 HTTP %d" % code]
    if typ:
        parts.append(typ)
    if msg:
        parts.append(msg)
    if hint:
        parts.append(hint)
    return RuntimeError(" · ".join(parts))


def _retry_after(exc):
    try:
        ra = exc.headers.get("Retry-After") if exc.headers else None
        if ra:
            return float(ra)
    except Exception:
        pass
    return 2.0


def _throttle():
    """普通用户 2 秒/次的最小间隔（管理员返回 0 时不等待）。"""
    with _LOCK:
        wait = _STATE["last_call"] + _STATE["min_interval"] - time.time()
    if wait > 0:
        time.sleep(min(wait, 5.0))
    with _LOCK:
        _STATE["last_call"] = time.time()


# ---------------------------------------------------------------- 对外 API

def predict(codes, timeout=25, use_cache=True, key=None, url=None):
    """批量概率预测（≤50 个）。返回 {code: {prob, ts, market, model, threshold}}。

    结果按标的缓存（默认 300s），命中缓存不扣费；429 自动按 Retry-After
    重试一次；其它错误抛中文异常（不含 Key）。
    """
    codes = list(dict.fromkeys(
        [str(c).strip() for c in (codes or []) if str(c).strip()]))
    if not codes:
        return {}
    if len(codes) > 50:
        raise RuntimeError("sm701 云 API 单次最多 50 个标的")
    now = time.time()
    ttl = _ttl()
    out, need = {}, []
    with _LOCK:
        cache = _STATE["cache"]
        for c in codes:
            hit = cache.get(c)
            if use_cache and hit and now - hit[0] < ttl:
                out[c] = dict(hit[1])
            else:
                need.append(c)
    if not need:
        return out

    payload = None
    for attempt in range(2):
        _throttle()
        try:
            payload = _call("POST", "/v1/predict", {"codes": need},
                            timeout=timeout, key=key, url=url)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(min(5.0, max(0.5, _retry_after(e))))
                continue
            err = _friendly(e)
            with _LOCK:
                _STATE["last_error"] = str(err)
            raise err from None
        except Exception as e:
            with _LOCK:
                _STATE["last_error"] = str(e)
            if attempt == 0:
                time.sleep(0.8)
                continue
            raise
    if payload is None:
        raise RuntimeError(_STATE.get("last_error") or "sm701 云预测失败")

    got = {}
    for r in (payload.get("results") or []):
        code = str(r.get("code") or "").strip()
        try:
            p = float(r.get("prob_up"))
        except (TypeError, ValueError):
            continue
        if code and 0.0 <= p <= 1.0:
            got[code] = {"prob": p,
                         "ts": str(r.get("ts") or ""),
                         "market": str(r.get("market") or ""),
                         "model": str(r.get("model") or ""),
                         "threshold": r.get("threshold")}
    norm = {}
    for c in need:
        rec = (got.get(c) or got.get(c.upper()) or got.get(c.lower())
               or next((v for k, v in got.items()
                        if k.upper() == c.upper()), None))
        if rec is not None:
            norm[c] = rec
    with _LOCK:
        for c, rec in norm.items():
            _STATE["cache"][c] = (now, dict(rec))
    out.update(norm)
    return out


def prob_for(full, date=None, hourly=False, timeout=25):
    """单只标的的最新概率。

    返回 {prob, ts, market, model, threshold} 或 None（未配置/不覆盖/云数据
    比 `date` 还旧）。`date` 为本地最新K线日期（YYYY-MM-DD）。
    """
    code = code_for(full, hourly=hourly)
    if not code:
        return None
    rec = predict([code], timeout=timeout).get(code)
    if not rec:
        return None
    ts = (rec.get("ts") or "")[:10]
    if date and ts and ts < date:
        return None              # 云端数据更旧：不用于该bar（避免回填错位）
    return rec


def balance(key=None, url=None, timeout=15):
    """GET /v1/balance（免费）：余额与限流。"""
    return _call("GET", "/v1/balance", None, timeout=timeout,
                 key=key, url=url)


def health(url=None, timeout=10):
    """GET /health（免费、无需鉴权）。"""
    base = _norm_url(url) or api_url()
    req = urllib.request.Request(
        base.rstrip("/") + "/health", headers={"User-Agent": UA})
    raw = _host_open_url(req, req.full_url, timeout)
    return json.loads(raw.decode("utf-8", "replace"))


def status():
    """本地状态快照（不联网）：供设置页/关于页展示。"""
    k = api_key()
    return {"configured": bool(k), "ready": enabled(),
            "url": api_url(), "key": mask_key(k), "source": key_source(),
            "last_error": _STATE.get("last_error", "")}
