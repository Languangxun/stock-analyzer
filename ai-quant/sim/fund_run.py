"""场外 ETF 联接 C 类基金模拟盘（etf-c 旧版，保留模式）。

由 sim/run.py --mode fund 调度；也可直接：
  python -m sim.fund_run [--capital 100000] [--state path] [--ollama]
                         [--dry-run] [--no-update] [--no-rag]
"""
import argparse
import glob
import json
import os
import shutil
import time
import math
from datetime import datetime

from trading.calendar import TradingCalendar
from trading.account import FundAccount
from trading.executor import Executor
from agent.ensemble import EnsembleDecision, DeepSeekVoter, OllamaVoter
from data.fund.fund_mapping import FUND_MAP
from data.fund.fund_provider import EastMoneyPingZhongProvider
from data.market.tencent_provider import TencentProvider
from memory import otc_memory
from scripts.usb_backup import backup as usb_backup

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(BASE_DIR, "sim", "state", "account.json")
LOG_PATH = os.path.join(BASE_DIR, "logs", "sim.log")
INBOX_DIR = os.path.join(BASE_DIR, "memory", "inbox")


# ---------- 账户状态 ----------

def load_account(path, capital):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return FundAccount.from_state(json.load(f))
    return FundAccount(capital)


def save_account(path, account):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(account.to_state(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------- 数据 ----------



def load_inbox_signals():
    """读取 stock_predict CLI 推送的外部个股信号，消费后归档。"""
    out = []
    if not os.path.isdir(INBOX_DIR):
        return out
    for fnm in sorted(glob.glob(os.path.join(INBOX_DIR, "*.json"))):
        try:
            with open(fnm, encoding="utf-8") as f:
                out.append(json.load(f))
            arch = os.path.join(INBOX_DIR, "archive")
            os.makedirs(arch, exist_ok=True)
            shutil.move(fnm, os.path.join(arch, os.path.basename(fnm)))
        except Exception as e:
            print("[warn] 收件箱 %s 读取失败: %s" % (fnm, e))
    return out


def fetch_latest():
    """最新数据：{symbol: {etf_price, etf_change, nav, nav_date}}。"""
    result = {}
    tencent = TencentProvider()
    nav_provider = EastMoneyPingZhongProvider()

    for symbol, info in FUND_MAP.items():
        entry = {}
        try:
            md = tencent.get(symbol, info.etf_code, info.etf_market)
            entry["etf_price"] = md.price
            entry["etf_change"] = md.change_percent
        except Exception as e:
            print(f"[warn] {symbol} 行情失败: {e}")
        try:
            latest = nav_provider.get_latest(info.fund_code)
            entry["nav"] = latest.nav
            entry["nav_date"] = latest.date
        except Exception as e:
            print(f"[warn] {symbol} 净值失败: {e}")
        if entry.get("nav"):
            result[symbol] = entry
    return result


def update_history():
    """刷新本地历史数据（NAV + ETF 行情），失败不阻断主流程。"""
    try:
        from data.history.fetch_all import main as fetch_main
        fetch_main()
        print("历史数据已更新")
    except Exception as e:
        print(f"[warn] 历史数据更新失败: {e}")


# ---------- RAG ----------

def build_query_text(context):
    """市场快照转检索文本。"""
    parts = []
    for m in context.get("market", []):
        parts.append(
            f"{m['symbol']}涨跌{m.get('change_1d', 0)}%趋势{m.get('trend', 'side')}"
        )
    acc = context.get("account", {})
    positions = acc.get("positions") or []
    pos_str = "无持仓" if not positions else " ".join(
        f"{p['symbol']}{p.get('pct', 0)}%" for p in positions
    )
    parts.append(f"现金{acc.get('cash', 0)} 持仓:{pos_str}")
    return " ".join(parts)


# ---------- 报告 ----------

def write_daily_summary(data, decision, trade, account, date_str=None):
    """当日总结（json）。文件名用交易日，避免跨午夜运行标错日期。"""
    today = date_str or data.get("date") or datetime.now().strftime("%Y-%m-%d")
    folder = os.path.join(BASE_DIR, "memory", "daily")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{today}.json")
    summary = {
        "time": datetime.now().isoformat(),
        "date": data.get("date", today),
        "decision": (
            {
                "action": decision.action,
                "target": decision.target,
                "target_position": decision.target_position,
                "confidence": decision.confidence,
                "reason": decision.reason,
            } if decision else None
        ),
        "trade": trade.to_dict() if hasattr(trade, "to_dict") else str(trade),
        "account": account.snapshot(data["navs"]),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return path


def write_review(data, decision, trade, account, votes, similar, date_str=None):
    """人类可读复盘报告（markdown）。文件名用交易日，避免跨午夜运行标错日期。"""
    today = date_str or data.get("date") or datetime.now().strftime("%Y-%m-%d")
    folder = os.path.join(BASE_DIR, "memory", "daily")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"复盘-{today}.md")
    snap = account.snapshot(data["navs"])

    lines = [f"# 模拟盘复盘 {today}", ""]
    lines.append(f"- 交易日 T：{data['date']}")
    lines.append(f"- 总资产：{snap['total_asset']} 元")
    lines.append(f"- 现金：{snap['cash']} 元")
    lines.append(f"- 已实现收益：{snap['realized_pnl']} 元")
    lines.append(f"- 未实现收益：{snap['unrealized_pnl']} 元")
    lines.append("")
    if decision:
        lines.append("## 决策")
        lines.append(f"- 动作：{decision.action} {decision.target}")
        lines.append(f"- 目标仓位：{decision.target_position}%")
        lines.append(f"- 置信度：{decision.confidence}")
        lines.append(f"- 理由：{decision.reason}")
        lines.append("")
    if votes:
        lines.append("## 模型投票")
        for v in votes:
            if v["ok"]:
                d = v["decision"]
                lines.append(
                    f"- {v['voter']}: {d.action} {d.target} "
                    f"{d.target_position}% (conf {d.confidence})"
                )
            else:
                lines.append(f"- {v['voter']}: 失败 {v.get('error', '')[:60]}")
        lines.append("")
    lines.append("## 执行")
    if hasattr(trade, "to_dict"):
        t = trade.to_dict()
        lines.append(
            f"- {t['side']} {t['symbol']} 金额{t['amount']} 份额{t['shares']} "
            f"NAV {t['nav']}"
        )
    else:
        lines.append(f"- {trade}")
    lines.append("")
    lines.append("## 持仓")
    if snap["positions"]:
        for s, p in snap["positions"].items():
            lines.append(f"- {s}: {p['shares']} 份（{p['pct']}%）冻结{p['frozen']}")
    else:
        lines.append("- 空仓")
    lines.append("")
    if similar:
        lines.append("## 相似历史")
        for s in similar[:3]:
            lines.append(f"- ({s['score']:.2f}) {s['text'][:80]}")
    lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ---------- 备份与维护 ----------


def rotate_logs(max_mb=2, keep=10):
    """日志轮转：sim.log 超 max_mb 归档，只保留最近 keep 份。"""
    try:
        if not os.path.exists(LOG_PATH):
            return
        size = os.path.getsize(LOG_PATH)
        if size < max_mb * 1024 * 1024:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        os.replace(LOG_PATH, f"{LOG_PATH}.{stamp}")
        logs = sorted(
            [f for f in os.listdir(os.path.dirname(LOG_PATH))
             if f.startswith("sim.log.")],
            reverse=True,
        )
        for old in logs[keep:]:
            os.remove(os.path.join(os.path.dirname(LOG_PATH), old))
    except OSError as e:
        print(f"[maintain] 日志轮转失败: {e}")


# ---------- 主流程 ----------

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, default=100000)
    parser.add_argument("--state", default=STATE_PATH)
    parser.add_argument("--ollama", action="store_true",
                        help="启用本地 Ollama 投票（慢，~2-5 分钟）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只决策不执行")
    parser.add_argument("--no-update", action="store_true",
                        help="跳过历史数据刷新")
    parser.add_argument("--no-rag", action="store_true",
                        help="跳过相似历史检索")
    args = parser.parse_args(argv)

    calendar = TradingCalendar()
    now = datetime.now()
    trade_date = calendar.trade_date_of(now)

    print(f"=== 常驻模拟盘 {now:%Y-%m-%d %H:%M} ===")
    print(f"交易日 T = {trade_date}")

    if not args.no_update:
        update_history()

    account = load_account(args.state, args.capital)
    executor = Executor(account, calendar=calendar)

    # 数据
    data = fetch_latest()
    navs = {s: v["nav"] for s, v in data.items()}
    if not navs:
        print("无可用净值数据，退出")
        return
    print(f"数据: {len(navs)} 只基金")

    # 确认到期订单
    confirmed = account.confirm_orders(str(trade_date), navs)
    if confirmed:
        print(f"确认订单: {len(confirmed)} 笔")

    # 构建上下文（含历史技术指标）
    from backtest.data_loader import HistoryDataLoader
    from data.features.technical import TechnicalFeature
    hist_loader = HistoryDataLoader()
    feats_engine = TechnicalFeature()

    market_list = []
    for s, v in data.items():
        entry = {
            "symbol": s,
            "etf_code": FUND_MAP[s].etf_code,
            "close": v.get("etf_price", 0),
            "change_1d": v.get("etf_change", 0),
            "prev_nav": v.get("nav"),
            "prev_nav_date": v.get("nav_date"),
            "position_pct": round(account.position_pct(s, navs), 2),
        }
        # 历史序列只加载一次（除息校正 + 技术指标共用）
        try:
            series = hist_loader.market_series(s, str(trade_date))
        except Exception:
            series = []
        # 场内实时除息校正：实时价 vs 复权昨收 = 真实涨跌；与原始涨跌幅差 >1.5pp = 除息/折算
        try:
            qfq_before = [
                p for p in series
                if p.get("date", "") < str(trade_date)
            ]
            base = qfq_before[-1]["close"] if qfq_before else 0
            raw_change = v.get("etf_change") or 0
            if base and v.get("etf_price"):
                adj = (v["etf_price"] - base) / base * 100
                entry["change_1d"] = round(adj, 2)
                if abs(adj - raw_change) > 1.5:
                    entry["ex_dividend"] = (
                        f"场内ETF除息/折算检测：实时涨跌 {raw_change:+.2f}% "
                        f"vs 复权后 {adj:+.2f}%（除息跳空，勿当大跌）"
                    )
        except Exception:
            pass
        try:
            if series:
                feats = feats_engine.calculate_prices(
                    [p["close"] for p in series]
                )
                entry["ma5"] = round(feats["ma5"], 4)
                entry["ma20"] = round(feats["ma20"], 4)
                entry["trend"] = feats["trend"]
                entry["volatility"] = round(feats["volatility"], 4)
        except Exception:
            pass
        entry.setdefault("ma5", 0)
        entry.setdefault("ma20", 0)
        entry.setdefault("trend", "side")
        entry.setdefault("volatility", 0)
        try:
            entry["anomaly"] = hist_loader.nav_anomaly(s, str(trade_date))
        except Exception:
            entry["anomaly"] = None
        if entry.get("ex_dividend"):
            entry["anomaly"] = (
                entry["anomaly"] + "；" + entry["ex_dividend"]
                if entry.get("anomaly") else entry["ex_dividend"]
            )
        market_list.append(entry)

    context = {
        "date": str(trade_date),
        "market": market_list,
        "account": {
            "cash": round(account.cash, 2),
            "total_asset": round(account.total_asset(navs), 2),
            "positions": [
                {
                    "symbol": s,
                    "shares": round(account.total_shares(s), 4),
                    "nav": navs.get(s, 0),
                    "pct": round(account.position_pct(s, navs), 2),
                    "frozen": round(account.frozen_shares(s), 4),
                }
                for s in FUND_MAP
                if account.total_shares(s) > 0
            ],
            "pending": len([o for o in account.pending
                            if o.status == "pending"]),
        },
    }

    # RAG 相似历史
    similar = []
    if not args.no_rag:
        print("检索相似历史...")
        similar = otc_memory.search(
            build_query_text(context), limit=3
        )
        if similar:
            print(f"  找到 {len(similar)} 条相似记忆")
    context["similar_history"] = similar
    context["lessons"] = otc_memory.load_lessons(5)
    context["stock_signals"] = load_inbox_signals()
    if context["stock_signals"]:
        print("  外部信号 %d 条已并入决策上下文"
              % len(context["stock_signals"]))

    # 多模型决策
    voters = [DeepSeekVoter()]
    if args.ollama:
        ollama_url = os.environ.get(
            "OLLAMA_BASE_URL", "http://localhost:11434"
        )
        ollama_model = os.environ.get("OLLAMA_MODEL", "qwen3.5:4b")
        voters.append(OllamaVoter(
            model=ollama_model, base_url=ollama_url
        ))
    ensemble = EnsembleDecision(voters)

    def position_of(symbol):
        """按行业查当前仓位 %（供决策判断 BUY/SELL 用）。"""
        try:
            return account.position_pct(symbol, navs)
        except Exception:
            return 0.0

    print("多模型决策中...")
    decision, votes = ensemble.decide(context, position_of)
    print(f"决策: {decision.action} {decision.target} "
          f"target={decision.target_position} conf={decision.confidence}")
    for v in votes:
        if v["ok"]:
            d = v["decision"]
            print(f"  [{v['voter']}] {d.action} {d.target} "
                  f"pos={d.target_position} conf={d.confidence}")
        else:
            print(f"  [{v['voter']}] FAILED: {v.get('error', '')[:80]}")

    # 执行（除权/数据异常守卫）
    trade = "HOLD"
    if not args.dry_run:
        anomaly_note = None
        if decision and decision.action != "HOLD":
            for m in context.get("market", []):
                if (m.get("symbol") == decision.target
                        and m.get("anomaly")):
                    anomaly_note = m["anomaly"]
                    break
        if anomaly_note:
            trade = f"BLOCKED (数据异常: {anomaly_note})"
            print(f"执行: {trade}")
        else:
            trade = executor.execute(decision, navs, str(trade_date))
            print(f"执行: {trade}")

    # 保存 + 复盘 + 记忆 + 备份 + 维护
    save_account(args.state, account)
    summary_path = write_daily_summary(
        {"date": str(trade_date), "navs": navs}, decision, trade, account,
        date_str=str(trade_date),
    )
    review_path = write_review(
        {"date": str(trade_date), "navs": navs},
        decision, trade, account, votes, similar,
        date_str=str(trade_date),
    )
    print(f"状态已保存: {args.state}")
    print(f"总结已保存: {summary_path}")
    print(f"复盘已保存: {review_path}")

    # RAG 记忆入库
    trade_str = (
        trade.to_dict().__str__() if hasattr(trade, "to_dict") else str(trade)
    )
    otc_memory.add_memory(
        str(trade_date),
        build_query_text(context),
        (
            f"{decision.action} {decision.target} "
            f"{decision.target_position}%"
            if decision else "HOLD"
        ),
        trade_str[:200],
    )

    usb_backup(label="fund", progress=print)
    rotate_logs()


if __name__ == "__main__":
    main()
