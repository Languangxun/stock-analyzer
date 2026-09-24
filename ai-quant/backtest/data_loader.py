"""历史数据加载：fund_nav.json + market.json。

格式：
fund_nav.json: {symbol: {fund_code, fund_name, etf_code, points: [{date, nav, growth}]}}
market.json:   {symbol: {etf_code, points: [{date, close, change}]}}
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class HistoryDataLoader:
    def __init__(self, base_dir=None):
        self.dir = base_dir or os.path.join(BASE_DIR, "data", "history")

    def load(self, name):
        path = os.path.join(self.dir, name)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def load_fund_navs(self):
        return self.load("fund_nav.json")

    def load_markets(self):
        return self.load("market.json")

    def trading_days(self):
        """合并所有基金净值日与 ETF 行情的共同交易日序列（升序）。"""
        navs = self.load_fund_navs()
        markets = self.load_markets()
        day_sets = []
        for data in (navs, markets):
            days = set()
            for symbol, entry in data.items():
                for p in entry["points"]:
                    days.add(p["date"])
            day_sets.append(days)
        common = set.intersection(*day_sets) if day_sets else set()
        return sorted(common)

    def nav_on(self, symbol, date):
        """指定日 NAV；缺当日则取最近一个 <= date 的净值（场外 T 日未知价时用前值）。"""
        navs = self.load_fund_navs()
        entry = navs.get(symbol)
        if not entry:
            return None
        hit = None
        for p in entry["points"]:
            if p["date"] <= date:
                hit = p
            else:
                break
        return hit

    def market_series(self, symbol, upto_date):
        """ETF 收盘价序列（截至 upto_date）。"""
        markets = self.load_markets()
        entry = markets.get(symbol)
        if not entry:
            return []
        return [
            p for p in entry["points"] if p["date"] <= upto_date
        ]


    def nav_anomaly(self, symbol, trade_date, max_divergence=5.0):
        """净值 vs ETF 背离检测（疑似除权/折算/数据异常）。

        比较 T-1 日净值涨跌与 ETF 涨跌；背离超过 max_divergence 个百分点时返回说明，
        否则 None。决策侧应据此 HOLD，避免把除权跳变误判为行情。
        """
        try:
            d = str(trade_date)
            navs = self.load_fund_navs().get(symbol, {})
            pts = [p for p in navs.get("points", []) if p["date"] < d]
            if len(pts) < 2:
                return None
            nav_chg = (
                (pts[-1]["nav"] - pts[-2]["nav"]) / pts[-2]["nav"] * 100
            )
            series = self.market_series(symbol, d)
            etf = [p for p in series if p.get("date", "") < d]
            if len(etf) < 2:
                return None
            etf_chg = (
                (etf[-1]["close"] - etf[-2]["close"])
                / etf[-2]["close"] * 100
            )
            div = nav_chg - etf_chg
            if abs(div) > max_divergence:
                return (
                    f"{pts[-1]['date']} 净值 {nav_chg:+.2f}% vs "
                    f"ETF {etf_chg:+.2f}%（背离 {div:+.2f}pp，疑似除权/折算/数据异常）"
                )
        except Exception:
            pass
        return None
