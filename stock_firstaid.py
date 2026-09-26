#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stock_firstaid.py - 数据源急救箱（独立程序，不依赖 stock_gui）

功能：
  1. 体检：逐个探测全部已知数据源候选域，输出哪些活/哪些挂
  2. 自救：把验证通过的最佳 K 线源写入 stock_gui.ini [data] kline_url，
     主程序(GUI/CLI)启动时自动读取覆盖默认源
  3. --ai 求救：内置候选全部挂掉时，调用 DeepSeek 提议候选接口 URL，
     每个提议都必须通过真实数据探测验证后才启用（AI 只提议，代码只信任实测）

安全边界：
  - 只做 http/https GET 探测，绝不执行模型输出的任何代码
  - AI 提议的 URL 必须返回含近期日期/OHLC 数字的真实数据才被采纳
  - 每个源最多向 AI 求救 1 轮、最多验证 5 个提议 URL

用法：
  python stock_firstaid.py            # 体检 + 自救
  python stock_firstaid.py --ai      # 体检 + 自救 + AI求救
  python stock_firstaid.py --check   # 只体检不改配置
"""
import configparser
import json
import os
import re
import sys
import time
import urllib.request

# Windows GBK 控制台兜底：打不出的字符替换而不是崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
INI_PATH = os.path.join(HERE, "stock_gui.ini")

# 内置候选域（按优先级）。探测目标固定用 sz002241（创业板，数据稳定）
TENCENT_CANDS = [
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "http://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
]
EM_KLINE_CANDS = [
    f"https://{h}/api/qt/stock/kline/get" for h in (
        "push2his.eastmoney.com", "92.push2his.eastmoney.com",
        "93.push2his.eastmoney.com", "97.push2his.eastmoney.com")
] + [
    "https://push2delay.eastmoney.com/api/qt/stock/kline/get",
]
EM_LIST_CANDS = [
    f"https://{h}/api/qt/clist/get" for h in (
        "push2delay.eastmoney.com", "push2.eastmoney.com",
        "92.push2delay.eastmoney.com")
] + ["http://push2delay.eastmoney.com/api/qt/clist/get"]
SINA_CANDS = [
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData",
]
N163_CANDS = [
    "http://quotes.money.163.com/service/chddata.html",
]
UT = "fa5fd1943c7b386f172d6893dbfba10b"
HDR = {"User-Agent": "Mozilla/5.0",
       "Referer": "https://quote.eastmoney.com/"}
TEST_CODE = "sz002241"
RECENT = time.strftime("%Y-%m")
URL_RE = re.compile(r"https?://[^\s\"'<>\)\]]+", re.I)


def http_get(url, timeout=10, decode="utf-8", headers=None):
    hdr = dict(HDR)
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(decode, errors="ignore")


def _valid_payload(txt, kind):
    """校验响应里是否包含真实行情数据（近期日期 + 数字）。"""
    if not txt or txt.lstrip().startswith("<"):
        return False
    if RECENT[:7] not in txt and RECENT not in txt:
        return False
    if kind == "json":
        try:
            json.loads(txt.lstrip("\ufeff"))
            return True
        except Exception:
            return False
    return True


def probe(url_builder, validator, timeout=10):
    """构造URL→请求→校验。返回 (ok, detail)。"""
    try:
        url = url_builder()
        if not url:
            return False, "跳过"
        txt = http_get(url, timeout=timeout)
        ok = validator(txt)
        return ok, ("数据正常" if ok else "响应无有效数据")
    except Exception as e:
        return False, f"{e.__class__.__name__}: {str(e)[:60]}"


def v_tencent(base):
    def build():
        return base + f"?param={TEST_CODE},day,,,5,hfq"

    def check(txt):
        try:
            d = json.loads(txt)
            bars = ((d.get("data") or {}).get(TEST_CODE) or {}).get(
                "hfqday") or ((d.get("data") or {}).get(TEST_CODE) or {}
                              ).get("day") or []
            return len(bars) >= 5 and RECENT in str(bars[-1][:1])
        except Exception:
            return False
    return build, check


def v_em_kline(base):
    def build():
        return (base + f"?secid=0.{TEST_CODE[2:]}&fields1=f1,f2,f3"
                f"&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=1"
                f"&beg=0&end=20500101&lmt=5")

    def check(txt):
        try:
            kl = (json.loads(txt).get("data") or {}).get("klines") or []
            return len(kl) >= 5 and RECENT in kl[-1]
        except Exception:
            return False
    return build, check


def v_em_list(base):
    def build():
        return (base + "?pn=1&pz=5&po=1&np=1&fltt=2&invariant=0"
                f"&fields=f12,f14,f3&fs=m:90+t:2&ut={UT}")

    def check(txt):
        try:
            diff = (json.loads(txt).get("data") or {}).get("diff") or {}
            return len(diff) >= 3
        except Exception:
            return False
    return build, check


def v_sina(base):
    def build():
        return (base + f"?symbol={TEST_CODE}&scale=240&ma=no&datalen=5")

    def check(txt):
        try:
            bars = json.loads(re.sub(r'(?<=[{,])(\w+):', r'"\1":', txt))
            return isinstance(bars, list) and len(bars) >= 5 \
                and RECENT in str(bars[-1].get("day", ""))
        except Exception:
            return False
    return build, check


def v_163(base):
    def build():
        return (base + "?code=1" + TEST_CODE[2:]
                + "&start=20260101&end=20500101&fields=TCLOSE")

    def check(txt):
        return (RECENT in txt) and txt.count(",") > 5
    return build, check


SUITES = [
    ("腾讯K线", [(u, *v_tencent(u)) for u in TENCENT_CANDS]),
    ("东财K线", [(u, *v_em_kline(u)) for u in EM_KLINE_CANDS]),
    ("东财列表/板块", [(u, *v_em_list(u)) for u in EM_LIST_CANDS]),
    ("新浪K线", [(u, *v_sina(u)) for u in SINA_CANDS]),
    ("网易163", [(u, *v_163(u)) for u in N163_CANDS]),
]


def checkup():
    """全量体检。返回 {源名: [(url, ok, detail), ...]}"""
    results = {}
    for name, cands in SUITES:
        print(f"\n-- {name} --")
        rows = []
        for url, build, check in cands:
            ok, detail = probe(build, check)
            print(f"  [{'活' if ok else '挂'}] {url}\n        {detail}")
            rows.append((url, ok, detail))
        results[name] = rows
    return results


def _load_ini():
    cp = configparser.ConfigParser()
    cp.read(INI_PATH, encoding="utf-8")
    return cp


def _save_kline_url(url):
    cp = _load_ini()
    if not cp.has_section("data"):
        cp.add_section("data")
    old = cp.get("data", "kline_url", fallback="")
    cp.set("data", "kline_url", url)
    cp.set("data", "kline_url_updated", time.strftime("%Y-%m-%d %H:%M"))
    with open(INI_PATH, "w", encoding="utf-8") as f:
        cp.write(f)
    return old


def ai_discover(api_key, source_name, model=None):
    """向 DeepSeek 求救：要候选接口URL。只返回URL字符串列表，绝不执行。"""
    sys.path.insert(0, HERE)
    from stock_gui import deepseek_chat  # 复用主程序调用封装
    prompt = (
        f"我有一个A股工具，数据源\"{source_name}\"的所有已知接口都失效了。"
        f"请给我最多5个【可以直接HTTP GET】获取A股 {TEST_CODE} 日K线数据的"
        f"候选接口完整URL（免费、无需key），一行一个URL，不要解释，"
        f"不要markdown代码块。只输出URL列表。")
    try:
        txt = deepseek_chat(api_key, prompt, model=model, timeout=60)
    except Exception as e:
        print(f"  AI求救失败: {e}")
        return []
    urls = URL_RE.findall(txt or "")
    out = []
    for u in urls:
        u = u.rstrip(".,;，。；")
        if u not in out:
            out.append(u)
    return out[:5]


def rescue(results, use_ai=False):
    """自救：腾讯K线源选第一个活的写入ini；全挂且 --ai 时向AI求救。"""
    tencent = results.get("腾讯K线", [])
    best = next((u for u, ok, _ in tencent if ok), None)
    if best:
        old = _save_kline_url(best)
        print(f"\n[OK] 已写入 stock_gui.ini [data] kline_url = {best}"
              + (f"（原值 {old or '默认'}）" if old != best else ""))
        return True
    if not use_ai:
        print("\n[!] 腾讯K线全部候选失效，未改配置。可加 --ai 尝试AI求救。")
        return False
    print("\n[AI] 内置候选全挂，启动AI求救...")
    cp = _load_ini()
    key = ""
    try:
        key = cp.get("deepseek", "api_key", fallback="")
    except Exception:
        pass
    if not key:
        print("  ini 无 deepseek api_key，无法求救。先在GUI设置里配好Key。")
        return False
    model = None
    try:
        model = cp.get("deepseek", "model", fallback=None)
    except Exception:
        pass
    cands = ai_discover(key, "腾讯K线", model)
    if not cands:
        print("  AI未给出可用提议。")
        return False
    print(f"  AI提议 {len(cands)} 个URL，逐个实测验证：")
    for u in cands:
        if not u.lower().startswith(("http://", "https://")):
            continue
        ok, detail = probe(lambda: u + f"?param={TEST_CODE},day,,,5,hfq",
                           v_tencent(u)[1])
        print(f"  [{'验证通过' if ok else '验证失败'}] {u}\n        {detail}")
        if ok:
            old = _save_kline_url(u)
            print(f"\n[OK] 已写入 stock_gui.ini [data] kline_url = {u}")
            return True
    print("\n[X] AI提议均未通过验证，未改配置（安全边界生效）。")
    return False


def main():
    args = sys.argv[1:]
    check_only = "--check" in args
    use_ai = "--ai" in args
    print("=" * 56)
    print(" 数据源急救箱  " + time.strftime("%Y-%m-%d %H:%M"))
    print("=" * 56)
    results = checkup()
    alive = {n: sum(1 for _, ok, _ in r if ok) for n, r in results.items()}
    print("\n" + "=" * 56)
    print(" 体检汇总: " + " | ".join(
        f"{n} {c}/{len(results[n])}" for n, c in alive.items()))
    if check_only:
        return
    rescue(results, use_ai)
    print("\n提示：修改后重启 stock_gui.py / stock_predict.py 生效。")


if __name__ == "__main__":
    main()

