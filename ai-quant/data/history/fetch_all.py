"""历史数据采集：场外基金 NAV + 场内 ETF 行情。

输出：
- data/history/fund_nav.json   {symbol: {fund_code, fund_name, etf_code, points: [{date, nav, growth}]}}
- data/history/market.json     {symbol: {etf_code, points: [{date, close, change}]}}

数据源：
- 基金 NAV：天天基金 pingzhongdata（Data_netWorthTrend）
- ETF 行情：东方财富 push2his kline
"""
import json
import os
import time
from datetime import date, timedelta

import requests

from data.fund.fund_mapping import FUND_MAP
from data.fund.fund_provider import EastMoneyPingZhongProvider

HISTORY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))),
    "data", "history",
)


def fetch_fund_navs():
    provider = EastMoneyPingZhongProvider()
    result = {}
    for symbol, info in FUND_MAP.items():
        print(f"[NAV] {symbol} {info.fund_code} ...")
        for attempt in range(3):
            try:
                points = provider.get_history(info.fund_code, use_cache=False)
                result[symbol] = {
                    "fund_code": info.fund_code,
                    "fund_name": info.fund_name,
                    "etf_code": info.etf_code,
                    "points": [
                        {"date": p.date, "nav": p.nav, "growth": p.growth}
                        for p in points
                    ],
                }
                print(f"       {len(points)} points, "
                      f"{points[0].date} -> {points[-1].date}")
                break
            except Exception as e:
                print(f"       attempt {attempt + 1} failed: {e}")
                time.sleep(2)
        time.sleep(0.5)
    return result


def _load_old_market(symbol):
    """读取现有 market.json 中某 symbol 的数据（拉取失败时回退）。"""
    try:
        old = os.path.join(HISTORY_DIR, "market.json")
        if os.path.exists(old):
            with open(old, encoding="utf-8") as f:
                data = json.load(f)
            return data.get(symbol, {}).get("points", [])
    except Exception:
        pass
    return []


def fetch_etf_markets():
    """ETF 日K行情（前复权日线），数据源：新浪历史K线。

    腾讯 ifzq.gtimg.cn 与东财 push2his 在部分网络环境被反爬/拒绝，
    改用新浪 quotes.sina.cn 的 getKLineData（scale=240 即日线）。
    单次最多约 1500 条（约 6 年），足够预测算法使用。

    返回 {symbol: {etf_code, points: [{date, close, change}]}}
    """
    url = ("https://quotes.sina.cn/cn/api/json_v2.php/"
           "CN_MarketDataService.getKLineData")
    result = {}
    for symbol, info in FUND_MAP.items():
        mkt = "sh" if info.etf_market == "sh" else "sz"
        key = f"{mkt}{info.etf_code}"
        print(f"[ETF] {symbol} {info.etf_code} ...")
        rows = []
        # 超时/限流时重试
        for attempt in range(3):
            try:
                resp = requests.get(
                    url,
                    params={"symbol": key, "scale": 240, "ma": "no",
                            "datalen": 1500},
                    timeout=25,
                    headers={"User-Agent": "Mozilla/5.0",
                             "Referer": "https://finance.sina.com.cn/"},
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    rows = data
                    break
            except Exception as e:
                print(f"       attempt {attempt + 1} failed: {e}")
                time.sleep(2 * (attempt + 1))

        points = []
        prev_close = None
        for row in rows:
            try:
                d = row.get("day", "")
                close = float(row.get("close", 0) or 0)
            except (TypeError, ValueError):
                continue
            change = (
                (close - prev_close) / prev_close * 100
                if prev_close else 0.0
            )
            points.append({
                "date": d, "close": close, "change": round(change, 4),
            })
            prev_close = close

        if not points:
            # 回退：保留旧 market.json 里该 symbol 的数据，避免留空
            fallback = _load_old_market(symbol)
            if fallback:
                points = fallback
                print(f"       [fallback] 用旧数据 {len(points)} points "
                      f"({points[-1]['date']})")
        result[symbol] = {
            "etf_code": info.etf_code,
            "points": points,
        }
        if points:
            print(f"       {len(points)} points, "
                  f"{points[0]['date']} -> {points[-1]['date']}")
        time.sleep(0.5)
    return result


def main():
    os.makedirs(HISTORY_DIR, exist_ok=True)

    navs = fetch_fund_navs()
    nav_path = os.path.join(HISTORY_DIR, "fund_nav.json")
    with open(nav_path, "w", encoding="utf-8") as f:
        json.dump(navs, f, ensure_ascii=False, indent=2)
    print(f"saved {nav_path} ({len(navs)} funds)")

    markets = fetch_etf_markets()
    mkt_path = os.path.join(HISTORY_DIR, "market.json")
    with open(mkt_path, "w", encoding="utf-8") as f:
        json.dump(markets, f, ensure_ascii=False, indent=2)
    print(f"saved {mkt_path} ({len(markets)} etfs)")


if __name__ == "__main__":
    main()
