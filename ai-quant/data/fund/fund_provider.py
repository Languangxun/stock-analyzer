"""场外基金 NAV Provider。

数据源：天天基金 pingzhongdata（JS 脚本内含 Data_netWorthTrend）。
接口化设计：可替换 EastMoney API / 本地历史数据 / 真实基金数据源。
"""
import json
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

import requests


@dataclass
class NavPoint:
    date: str          # YYYY-MM-DD
    nav: float         # 单位净值
    growth: float = 0.0  # 日增长率 %


class FundProvider(ABC):
    @abstractmethod
    def get_history(self, fund_code) -> list:
        """完整历史净值 [NavPoint]。"""

    @abstractmethod
    def get_latest(self, fund_code) -> NavPoint:
        """最新净值。"""


class EastMoneyPingZhongProvider(FundProvider):
    """天天基金 pingzhongdata 脚本解析。"""

    URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"

    def __init__(self, cache_path=None, cache_ttl=6 * 3600, timeout=10,
                 max_retries=2):
        self.cache_path = cache_path  # 本地缓存文件（json）
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self.max_retries = max_retries

    # ---------- 底层拉取 ----------

    def _fetch_script(self, fund_code) -> str:
        last_err = None
        for i in range(self.max_retries + 1):
            try:
                resp = requests.get(
                    self.URL.format(code=fund_code),
                    timeout=self.timeout,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                resp.raise_for_status()
                resp.encoding = "utf-8"
                return resp.text
            except requests.RequestException as e:
                last_err = e
                time.sleep(1 + i)
        raise RuntimeError(f"拉取 {fund_code} 净值失败: {last_err}")

    def _parse(self, script: str, fund_code: str) -> list:
        m = re.search(
            r"Data_netWorthTrend\s*=\s*(\[.*?\]);",
            script, re.S,
        )
        if not m:
            raise ValueError(f"{fund_code} 未找到 Data_netWorthTrend")
        raw = json.loads(m.group(1))
        points = []
        for item in raw:
            ts = item.get("x")
            nav = item.get("y")
            if ts is None or nav is None:
                continue
            points.append(NavPoint(
                date=time.strftime("%Y-%m-%d", time.localtime(ts / 1000)),
                nav=float(nav),
                growth=float(item.get("equityReturn", 0) or 0),
            ))
        return points

    # ---------- 缓存 ----------

    def _load_cache(self, fund_code):
        if not self.cache_path or not os.path.exists(self.cache_path):
            return None
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entry = data.get(fund_code)
            if not entry:
                return None
            if time.time() - entry.get("ts", 0) > self.cache_ttl:
                return None
            return [NavPoint(**p) for p in entry["points"]]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def _save_cache(self, fund_code, points):
        if not self.cache_path:
            return
        data = {}
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, TypeError):
                data = {}
        data[fund_code] = {
            "ts": time.time(),
            "points": [vars(p) for p in points],
        }
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self.cache_path)

    # ---------- 接口 ----------

    def get_history(self, fund_code, use_cache=True) -> list:
        if use_cache:
            cached = self._load_cache(fund_code)
            if cached:
                return cached
        points = self._parse(self._fetch_script(fund_code), fund_code)
        self._save_cache(fund_code, points)
        return points

    def get_latest(self, fund_code) -> NavPoint:
        points = self.get_history(fund_code)
        if not points:
            raise ValueError(f"{fund_code} 无净值数据")
        return points[-1]

    def get_nav_on(self, fund_code, d) -> NavPoint:
        """指定日期净值；无则取最近一个 <= d 的净值点。"""
        if isinstance(d, date):
            d = d.isoformat()
        points = self.get_history(fund_code)
        hit = None
        for p in points:
            if p.date <= d:
                hit = p
            else:
                break
        return hit
