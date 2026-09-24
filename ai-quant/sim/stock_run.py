"""股票常驻模拟盘：CLI 选股 + deepseek-v4.1-flash 组合决策 + qwen3-embedding 记忆。

每日流程（14:50 cron，`python -m sim.run`）：
1. 载入账户状态（sim/state/stock_account.json）
2. CLI 缓存扫描候选（多维评分 + 空头趋势闸门 + ST/退市过滤）
3. 候选/持仓实时行情（腾讯快照）
4. 构建上下文（候选/持仓/账户/经验/相似记忆）
5. deepseek-flash 决策 → orders
6. StockExecutor 执行（100股整手 / T+1 / 费用 / 涨跌停 / 持仓上限）
7. 保存状态 + 总结/复盘 + RAG 入库 + U 盘快照备份 + 日志轮转

用法：
  .venv/bin/python -m sim.run                     # 默认股票模式
  .venv/bin/python -m sim.run --dry-run --no-rag
  .venv/bin/python -m sim.run --top 30 --scan 300
"""
import argparse
import glob
import json
import os
import shutil
import time
from datetime import datetime

from trading.calendar import TradingCalendar
from trading.stock_account import StockAccount
from trading.stock_executor import StockExecutor
from data.stock import cli_bridge as bridge
from memory import otc_memory
from scripts.usb_backup import backup as usb_backup

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(BASE_DIR, "sim", "state", "stock_account.json")
LOG_PATH = os.path.join(BASE_DIR, "logs", "sim.log")
INBOX_DIR = os.path.join(BASE_DIR, "memory", "inbox")


def _load_cfg():
    import yaml
    path = os.path.join(BASE_DIR, "config", "stock.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["stock"]


# ---------- 账户状态 ----------

def load_account(path, capital, cfg):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return StockAccount.from_state(json.load(f))
    return StockAccount(
        capital,
        commission_rate=cfg["fees"]["commission_rate"],
        min_commission=cfg["fees"]["min_commission"],
        stamp_tax_rate=cfg["fees"]["stamp_tax_rate"],
        transfer_fee_rate=cfg["fees"]["transfer_fee_rate"],
    )


def save_account(path, account):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(account.to_state(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------- 行情 ----------

def load_inbox_signals():
    """读取 stock_predict CLI --push 推送的个股报告，消费后归档。"""
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
            print(f"[warn] 收件箱 {fnm} 读取失败: {e}")
    return out


def live_prices(codes):
    """实时行情：{code: quote}；失败回退缓存最新收盘。"""
    quotes = {}
    for code in codes:
        try:
            q = bridge.quote(code)
            if q.get("price"):
                quotes[code] = q
                continue
        except Exception as e:
            print(f"[warn] {code} 行情失败: {e}")
        rows = bridge.db_rows(code, tail=1)
        if rows:
            q = {"name": "", "price": rows[-1]["close"],
                 "prev_close": rows[-1]["close"], "stale": True}
            quotes[code] = q
    return quotes


# ---------- 上下文 / 报告 ----------

def build_query_text(context):
    parts = []
    for c in context.get("candidates", [])[:12]:
        parts.append(f"{c['code']}{c.get('chg', 0)}%评分{c.get('score', 0)}")
    acc = context.get("account", {})
    pos = acc.get("positions") or []
    pos_str = "无持仓" if not pos else " ".join(
        f"{p['code']}{p.get('pnl_pct', 0)}%" for p in pos)
    parts.append(f"现金{acc.get('cash', 0)} 持仓:{pos_str}")
    return " ".join(parts)


def write_daily_summary(date_str, context, result, trades, account, prices):
    folder = os.path.join(BASE_DIR, "memory", "daily")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"stock-{date_str}.json")
    summary = {
        "time": datetime.now().isoformat(),
        "date": date_str,
        "market_view": (result or {}).get("market_view"),
        "orders": (result or {}).get("orders"),
        "trades": [t.to_dict() if hasattr(t, "to_dict") else str(t)
                   for t in trades],
        "account": account.snapshot(prices),
        "candidates": [
            {k: c[k] for k in ("code", "name", "score", "chg", "price")
             if k in c} for c in context.get("candidates", [])[:20]
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return path


def write_review(date_str, context, result, trades, account, prices,
                 similar):
    folder = os.path.join(BASE_DIR, "memory", "daily")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"股票复盘-{date_str}.md")
    snap = account.snapshot(prices)
    lines = [f"# 股票模拟盘复盘 {date_str}", ""]
    lines.append(f"- 总资产：{snap['total_asset']} 元")
    lines.append(f"- 现金：{snap['cash']} 元")
    lines.append(f"- 已实现收益：{snap['realized_pnl']} 元")
    lines.append(f"- 未实现收益：{snap['unrealized_pnl']} 元")
    lines.append("")
    if result:
        lines.append("## 模型观点")
        lines.append(f"- {result.get('market_view', '')}")
        lines.append("")
        if result.get("orders"):
            lines.append("## 模型指令")
            for o in result["orders"]:
                lines.append(
                    f"- {o.get('action')} {o.get('code')} "
                    f"仓位{o.get('position')}% {o.get('reason', '')}")
            lines.append("")
    lines.append("## 执行")
    if trades:
        for t in trades:
            if hasattr(t, "to_dict"):
                d = t.to_dict()
                lines.append(
                    f"- {d['side']} {d['code']} {d['shares']}股 "
                    f"@{d['price']} 费用{d['fee']}")
            else:
                lines.append(f"- {t}")
    else:
        lines.append("- 无成交")
    lines.append("")
    lines.append("## 持仓")
    if snap["positions"]:
        for code, p in snap["positions"].items():
            lines.append(
                f"- {code}: {p['shares']}股 成本{p['cost']} 现价{p['price']} "
                f"({p['pnl_pct']:+.2f}%, 仓位{p['pct']}%)")
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


def rotate_logs(max_mb=2, keep=10):
    try:
        if not os.path.exists(LOG_PATH):
            return
        if os.path.getsize(LOG_PATH) < max_mb * 1024 * 1024:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        os.replace(LOG_PATH, f"{LOG_PATH}.{stamp}")
        logs = sorted(
            [f for f in os.listdir(os.path.dirname(LOG_PATH))
             if f.startswith("sim.log.")], reverse=True)
        for old in logs[keep:]:
            os.remove(os.path.join(os.path.dirname(LOG_PATH), old))
    except OSError as e:
        print(f"[maintain] 日志轮转失败: {e}")


# ---------- 主流程 ----------

def main(argv=None):
    cfg = _load_cfg()
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, default=cfg["capital"])
    parser.add_argument("--state", default=STATE_PATH)
    parser.add_argument("--top", type=int, default=30, help="候选数量")
    parser.add_argument("--scan", type=int, default=cfg["universe"]["max_scan"],
                        help="扫描股票数上限（按市值降序）")
    parser.add_argument("--mode-risk", default=cfg.get("risk_mode", "稳健"),
                        choices=["保守", "稳健", "激进"])
    parser.add_argument("--dry-run", action="store_true", help="只决策不执行")
    parser.add_argument("--no-rag", action="store_true", help="跳过相似历史检索")
    parser.add_argument("--no-backup", action="store_true", help="跳过 U 盘备份")
    args = parser.parse_args(argv)

    calendar = TradingCalendar()
    now = datetime.now()
    trade_date = calendar.trade_date_of(now)
    next_date = str(calendar.next_trading_day(trade_date))
    print(f"=== 股票模拟盘 {now:%Y-%m-%d %H:%M} ===")
    print(f"交易日 T = {trade_date}（T+1 可卖日 {next_date}）")

    account = load_account(args.state, args.capital, cfg)
    executor = StockExecutor(
        account, calendar=calendar,
        max_positions=cfg["max_positions"],
        max_position_pct=cfg["max_position_pct"],
        min_order_amount=cfg["min_order_amount"],
    )

    # 候选 + 行情
    print(f"扫描候选（前 {args.scan} 只，取 Top{args.top}）...")
    t0 = time.time()
    cands = bridge.candidates(
        top_n=args.top, max_scan=args.scan,
        min_bars=cfg["universe"]["min_bars"],
        min_price=cfg["universe"]["min_price"],
        recent_days=cfg["universe"]["recent_days"],
        risk_mode=args.mode_risk,
    )
    print(f"候选 {len(cands)} 只（{time.time() - t0:.0f}s）")
    codes = [c["code"] for c in cands] + account.position_codes()
    quotes = live_prices(list(dict.fromkeys(codes)))
    prices = {c: q["price"] for c, q in quotes.items() if q.get("price")}
    prev_closes = {c: q.get("prev_close") for c, q in quotes.items()}
    for c in cands:
        q = quotes.get(c["code"]) or {}
        c["price"] = q.get("price") or c.get("close")
        if q.get("price") and q.get("prev_close"):
            c["chg"] = round((q["price"] / q["prev_close"] - 1) * 100, 2)
    # 持仓行情兜底
    for code in account.position_codes():
        if code not in prices:
            rows = bridge.db_rows(code, tail=1)
            if rows:
                prices[code] = rows[-1]["close"]
                prev_closes[code] = rows[-1]["close"]

    positions = []
    for code in account.position_codes():
        shares = account.total_shares(code)
        cost = (sum(l.cost * l.shares for l in account.lots[code])
                / max(1, shares))
        px = prices.get(code, 0)
        positions.append({
            "code": code, "shares": shares, "cost": round(cost, 4),
            "price": px,
            "pnl_pct": round((px / cost - 1) * 100, 2) if cost else 0.0,
            "pct": round(account.position_pct(code, prices), 2),
            "sellable": account.available_shares(code, str(trade_date)),
        })

    context = {
        "date": str(trade_date),
        "mode": "stock",
        "candidates": cands,
        "positions": positions,
        "account": {
            "cash": round(account.cash, 2),
            "total_asset": round(account.total_asset(prices), 2),
            "position_count": len(positions),
            "max_positions": cfg["max_positions"],
            "max_position_pct": cfg["max_position_pct"],
            "t_plus_1_note": f"今日买入 {next_date} 起可卖",
        },
        "lessons": otc_memory.load_lessons(5),
        "stock_signals": load_inbox_signals(),
    }

    similar = []
    if not args.no_rag:
        print("检索相似历史（qwen3-embedding）...")
        similar = otc_memory.search(build_query_text(context), limit=3)
        if similar:
            print(f"  找到 {len(similar)} 条相似记忆")
    context["similar_history"] = similar

    # LLM 决策
    from agent.stock_decision import StockDecisionMaker
    print("deepseek-v4.1-flash 决策中...")
    maker = StockDecisionMaker()
    result, err = maker.decide(context)
    if err:
        print(f"决策失败: {err}")
        result = {"market_view": f"模型失败: {err[:80]}", "orders": []}
    else:
        print(f"市场观点: {result.get('market_view', '')}")
        for o in result.get("orders", []):
            print(f"  指令: {o.get('action')} {o.get('code')} "
                  f"pos={o.get('position')} conf={o.get('confidence')} "
                  f"{o.get('reason', '')[:40]}")

    # 执行
    trades = []
    if not args.dry_run:
        for o in (result or {}).get("orders", []):
            t = executor.execute_order(o, prices, str(trade_date),
                                       prev_closes)
            print(f"  执行 {o.get('code')}: {t if not hasattr(t, 'to_dict') else t.to_dict()}")
            if hasattr(t, "to_dict"):
                trades.append(t)
    else:
        print("[dry-run] 不执行")

    # 保存 + 复盘 + 记忆 + 备份 + 维护
    save_account(args.state, account)
    summary_path = write_daily_summary(
        str(trade_date), context, result, trades, account, prices)
    review_path = write_review(
        str(trade_date), context, result, trades, account, prices, similar)
    print(f"状态已保存: {args.state}")
    print(f"总结已保存: {summary_path}")
    print(f"复盘已保存: {review_path}")

    otc_memory.add_memory(
        str(trade_date), build_query_text(context),
        (f"股票 {len(trades)} 笔: " + ", ".join(
            f"{t.side}{t.code}" for t in trades)) if trades else "股票 HOLD",
        json.dumps(account.snapshot(prices), ensure_ascii=False)[:200],
    )

    if not args.no_backup:
        usb_backup(label="stock", progress=print)
    rotate_logs()


if __name__ == "__main__":
    main()
