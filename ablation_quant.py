#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ablation_quant.py - quant 侧「每日消融数据」接入（v7.4.2）

下载 https://quant.tail6a6cc8.ts.net/downloads 的每日消融产物，并转换成
本仓库 GUI 直接可用的 `research/ablation_gui_cache.json`（结构同
`run_ablation()` 返回值：三档 mode_candidates / recommend / candidates）。

quant 侧文件（下载中心 →「每日消融数据」）：
  public/ablation/latest.csv   每只股票 保守/稳健/激进 三档选中的策略 + 验证指标
  public/ablation/latest.json  批次汇总（发布时间/池子大小/各档 Top 策略）
  public/ablation/latest.md    可读报告

CSV 列：code,name,{档}_策略,{档}_验证年化,{档}_验证胜率,{档}_验证回撤。
策略名形如「KDJ金叉/死叉·稳健」——按最后一个「·」拆成 算法 + 风险档，
再用下方 LABEL_TO_ALGO 映射到本引擎的 algo key；风险参数取当前 ini
（GUI 调用时传 `ablation_risk_params()`，CLI 默认懒加载 stock_gui）。

产物：
  research/ablation_quant/latest.csv / latest.json   （原始留档，git 忽略）
  research/ablation_gui_cache.json                   （GUI 消融缓存，git 忽略）

用法：
    python ablation_quant.py                 # 下载 + 转换
    python ablation_quant.py --offline       # 只用已下载的 latest.csv 重新转换
    python ablation_quant.py --file x.csv --no-download
    python ablation_quant.py --base https://quant.tail6a6cc8.ts.net

注意：quant CSV 只有**验证集**指标（年化/胜率/回撤），没有训练集明细；
导入的候选 `train={}`，GUI 三档窗会显示训练期 "-"，推荐档按当前风险偏好
（`--prefer` / ini risk_mode）或验证年化最高档确定。历史统计研究，不构成
投资建议。
"""
import argparse
import csv
import datetime as _dt
import json
import os
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.join(HERE, "research")
RAW_DIR = os.path.join(RESEARCH, "ablation_quant")
CACHE_PATH = os.path.join(RESEARCH, "ablation_gui_cache.json")
DEFAULT_BASE = "https://quant.tail6a6cc8.ts.net"
LATEST_CSV = "public/ablation/latest.csv"
LATEST_JSON = "public/ablation/latest.json"
MODES = ("保守", "稳健", "激进")

# 与 stock_gui.ALGO_LABEL 保持同一套中文名（反查用）
LABEL_TO_ALGO = {
    "L1形态上行概率": "l1_pattern",
    "L2同行业+行业ETF": "l2_ind",
    "MACD金叉/死叉": "macd",
    "KDJ金叉/死叉": "kdj",
    "RSI超买超卖": "rsi",
    "布林带回归": "boll",
    "MA20/60趋势": "ma_trend",
    "筹码峰支撑": "chip_peak",
    "板块轮动": "sector_rot",
    "量比放量": "vol_ratio",
    "LGBM预测": "lgbm",
    "sm701上涨概率": "sm701",
    "异动·全市场相似": "analog",
    "多维评分": "composite",
    "策略工坊·外部策略": "studio",
}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _download(url, dest, timeout=120, retries=2):
    """带重试下载到 dest（先写 .part 再原子替换）。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    last = None
    for attempt in range(retries + 1):
        tmp = dest + ".part"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "stock-analyzer/ablation-sync"})
            with urllib.request.urlopen(req, timeout=timeout) as r, \
                    open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp, dest)
            return dest
        except Exception as exc:        # noqa: BLE001 - 网络异常统一重试
            last = exc
            try:
                os.remove(tmp)
            except OSError:
                pass
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"下载失败 {url}: {last}")


def _fetch_url(base, rel, timeout=120):
    return (base.rstrip("/") + "/download?file=" +
            urllib.parse.quote(rel, safe="/"))


def fetch(base=DEFAULT_BASE, outdir=RAW_DIR, timeout=120):
    """下载 latest.csv/latest.json 到 outdir，返回 (csv_path, json_path)。"""
    csv_path = os.path.join(outdir, "latest.csv")
    json_path = os.path.join(outdir, "latest.json")
    log(f"下载消融明细: {_fetch_url(base, LATEST_CSV)}")
    _download(_fetch_url(base, LATEST_CSV), csv_path, timeout=timeout)
    try:
        log(f"下载批次汇总: {_fetch_url(base, LATEST_JSON)}")
        _download(_fetch_url(base, LATEST_JSON), json_path, timeout=timeout)
    except Exception as exc:            # 汇总失败不影响明细导入
        log(f"批次汇总下载失败（仅用 CSV）: {exc}")
        json_path = ""
    return csv_path, json_path


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or abs(x) > 1e12:         # NaN/inf
        return None
    return x


def _parse_ts(meta, csv_path):
    """批次发布时间 → epoch；缺失用文件 mtime。"""
    for key in ("published_at", "ts"):
        s = str((meta or {}).get(key) or "").strip()
        if not s:
            continue
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return _dt.datetime.strptime(s, fmt).timestamp()
            except ValueError:
                continue
    try:
        return os.path.getmtime(csv_path)
    except OSError:
        return time.time()


def _split_label(label):
    """'KDJ金叉/死叉·稳健' → ('kdj', '稳健')；未知返回 (None, None)。"""
    label = (label or "").strip()
    base, sep, mode = label.rpartition("·")
    if not sep or mode not in MODES:
        return None, None
    return LABEL_TO_ALGO.get(base), mode


def _strategy(algo, mode, label, ann, win, mdd, params):
    return {
        "algo": algo,
        "mode": mode,
        "params": dict((params or {}).get(mode) or {}),
        "label": label,
        "train": {},                    # quant 只发布验证集指标
        "val": {"ann": ann, "winrate": win, "mdd": mdd,
                "trades": 0, "pf": None},
        "bull": None, "bear": None,
        "source": "quant",
    }


def build_cache(csv_path, meta=None, params=None, prefer_mode=None):
    """CSV → GUI 消融缓存 dict；返回 (cache, skipped)。"""
    if params is None:
        try:
            import stock_gui as sg
            params = sg.ablation_risk_params()
        except Exception:
            params = {}
    rows = {}
    skipped = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        fields = set(rd.fieldnames or ())
        need = {"code"} | {f"{t}_策略" for t in MODES}
        if not need <= fields:
            raise RuntimeError(f"CSV 缺少列: {sorted(need - fields)}")
        for row in rd:
            code = (row.get("code") or "").strip()
            if not code:
                continue
            cands = {}
            for tier in MODES:
                label = (row.get(f"{tier}_策略") or "").strip()
                if not label:
                    continue
                algo, mode = _split_label(label)
                if not algo:
                    skipped.append((code, tier, label))
                    continue
                if mode != tier:
                    # 保守档可选中「稳健参数」候选（引擎同口径），保留原 mode
                    pass
                cands[tier] = _strategy(
                    algo, mode, label,
                    _num(row.get(f"{tier}_验证年化")),
                    _num(row.get(f"{tier}_验证胜率")),
                    _num(row.get(f"{tier}_验证回撤")), params)
            if not cands:
                continue
            # 推荐档：优先当前风险偏好，否则验证年化最高（缺失按稳健）
            if prefer_mode in cands:
                rec = prefer_mode
            else:
                scored = [(t, (c.get("val") or {}).get("ann"))
                          for t, c in cands.items()]
                scored = [(t, v) for t, v in scored if v is not None]
                rec = max(scored, key=lambda x: x[1])[0] if scored else (
                    "稳健" if "稳健" in cands else next(iter(cands)))
            rows[code] = {
                "mode_candidates": cands,
                "recommend": rec,
                "pick_notes": {
                    t: ("quant 验证年化 %+.1f%% / 胜率 %.1f%%" % (
                        (c["val"]["ann"] or 0) * 100,
                        (c["val"]["winrate"] or 0) * 100))
                    for t, c in cands.items()},
                "candidates": list(cands.values()),
                # 三档窗需要的基础字段（quant 未发布训练集/波动率，占位）
                "bars": 1000, "train_n": 750, "val_n": 250,
                "vol_ann": None, "high_vol": False,
                "source": "quant",
                "ts": _parse_ts(meta, csv_path),
            }
    return {"ts": _parse_ts(meta, csv_path), "source": "quant",
            "codes": rows}, skipped


def write_cache(cache, out_path=CACHE_PATH):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out_path)
    return out_path


def sync(base=DEFAULT_BASE, outdir=RAW_DIR, out_path=CACHE_PATH,
         params=None, prefer_mode=None, offline=False, csv_file=None,
         timeout=120):
    """下载（或复用本地 CSV）→ 转换 → 写 GUI 缓存。返回摘要 dict。"""
    if csv_file:
        csv_path, json_path = csv_file, ""
    elif offline:
        csv_path = os.path.join(outdir, "latest.csv")
        json_path = os.path.join(outdir, "latest.json")
        if not os.path.isfile(csv_path):
            raise RuntimeError(f"离线模式缺少 {csv_path}，先联网跑一次")
    else:
        csv_path, json_path = fetch(base, outdir, timeout=timeout)
    meta = {}
    if json_path and os.path.isfile(json_path):
        try:
            with open(json_path, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as exc:        # noqa: BLE001
            log(f"汇总 JSON 解析失败（忽略）: {exc}")
    cache, skipped = build_cache(csv_path, meta, params, prefer_mode)
    n = len(cache.get("codes") or {})
    if not n:
        raise RuntimeError("CSV 未解析出任何可用消融记录")
    write_cache(cache, out_path)
    return {"ok": True, "codes": n, "skipped": len(skipped),
            "csv": csv_path, "json": json_path or "",
            "cache": out_path, "ts": cache.get("ts"),
            "source": cache.get("source")}


def main():
    ap = argparse.ArgumentParser(description="quant 每日消融数据接入")
    ap.add_argument("--base", default=DEFAULT_BASE, help="quant 站点根地址")
    ap.add_argument("--offline", action="store_true",
                    help="不下载，复用 research/ablation_quant/latest.csv")
    ap.add_argument("--file", default="", help="直接指定本地 CSV")
    ap.add_argument("--out", default=CACHE_PATH, help="输出缓存 JSON")
    ap.add_argument("--prefer", default="", choices=("",) + MODES,
                    help="推荐档（默认取验证年化最高档）")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()
    r = sync(base=args.base, out_path=args.out,
             prefer_mode=args.prefer or None,
             offline=args.offline, csv_file=args.file or None,
             timeout=args.timeout)
    log(f"完成：{r['codes']} 只股票，跳过 {r['skipped']}，"
        f"批次 {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['ts']))}")
    log(f"GUI 缓存: {r['cache']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
