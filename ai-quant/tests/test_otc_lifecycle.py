"""场外 ETF 联接 C 类生命周期测试（决策/风控/账户/日历/守恒）。

覆盖提示词要求的 15 项核心场景。
"""
from datetime import datetime, date

import pytest

from agent.decision import Decision, create_decision
from agent.decision_parser import parse_decision
from risk.manager import RiskManager
from trading.account import FundAccount
from trading.calendar import TradingCalendar
from trading.executor import Executor


# ---------------- 1. Decision ----------------

def test_decision_target_position_core():
    d = create_decision("BUY", "半导体", 40, 0.8, "看好", "波动")
    assert d.target_position == 40
    assert d.action == "BUY"


def test_decision_position_change_compat():
    d = create_decision("BUY", "半导体", 40, 0.8, "看好", "波动",
                        current_position=20)
    assert d.position_change == 20  # 兼容属性


# ---------------- 2. DecisionParser ----------------

def test_parse_decision_target_position():
    d = parse_decision({
        "action": "BUY",
        "target": "半导体",
        "target_position": 30,
        "confidence": 0.7,
        "reason": "趋势改善",
        "risk": "波动",
    })
    assert d.target_position == 30
    assert d.action == "BUY"


def test_parse_decision_bad_action_falls_back_hold():
    d = parse_decision({"action": "ATTACK", "target_position": 50})
    assert d.action == "HOLD"


# ---------------- 3. RiskManager ----------------

@pytest.fixture
def risk():
    return RiskManager()


def _dec(action, target):
    return create_decision(action, "半导体", target, 0.7, "r", "k",
                           current_position=10)


def test_risk_illegal_action(risk):
    r = risk.check(_dec("FLY", 30), 10)
    assert not r.allowed


def test_risk_negative_target(risk):
    r = risk.check(_dec("SELL", -5), 10)
    assert not r.allowed


def test_risk_over_max_position(risk):
    r = risk.check(_dec("BUY", 41), 10)
    assert not r.allowed


def test_risk_buy_must_raise(risk):
    r = risk.check(_dec("BUY", 5), 10)
    assert not r.allowed  # BUY 但目标低于当前


def test_risk_sell_must_lower(risk):
    r = risk.check(_dec("SELL", 30), 10)
    assert not r.allowed  # SELL 但目标高于当前


def test_risk_single_change_over_20_rejected(risk):
    r = risk.check(_dec("BUY", 35), 10)  # 单次 +25
    assert not r.allowed


def test_risk_liquidation_exempt(risk):
    r = risk.check(_dec("SELL", 0), 30)  # 清仓 30->0 超过20，豁免
    assert r.allowed
    assert r.target_position == 0


def test_risk_hold_keeps_current(risk):
    d = create_decision("HOLD", "半导体", 99, 0.5, "", "")
    r = risk.check(d, 15)
    assert r.allowed
    assert r.target_position == 15  # 保持当前确认仓位，不理会 target 数字


# ---------------- 4. 最低申购 ----------------

def _navs(v=1.0):
    return {"半导体": v}


def test_subscribe_99_rejected():
    acc = FundAccount(100000)
    with pytest.raises(ValueError):
        acc.subscribe("半导体", "007301", 99, "2026-08-13", "2026-08-14",
                      nav_est=1.0)


def test_subscribe_100_allowed():
    acc = FundAccount(100000)
    acc.subscribe("半导体", "007301", 100, "2026-08-13", "2026-08-14",
                  nav_est=1.0)
    assert acc.cash == 100000 - 100


# ---------------- 5. 申购 T 日：现金减少、份额 pending ----------------

def test_subscribe_t_day_pending():
    acc = FundAccount(100000)
    o = acc.subscribe("半导体", "007301", 20000, "2026-08-13", "2026-08-14",
                      nav_est=4.1155)
    assert acc.cash == 80000
    assert len(acc.pending) == 1
    assert o.status == "pending"
    assert acc.total_shares("半导体") == 0  # 份额未入持仓


# ---------------- 6. 申购 T+1 确认入仓 + FundLot ----------------

def test_subscribe_t1_confirm():
    acc = FundAccount(100000)
    acc.subscribe("半导体", "007301", 20000, "2026-08-13", "2026-08-14",
                  nav_est=4.0)
    confirmed = acc.confirm_orders("2026-08-14", {"半导体": 4.1155})
    assert len(confirmed) == 1
    assert acc.total_shares("半导体") == pytest.approx(20000 / 4.1155)
    lots = acc.lots["半导体"]
    assert len(lots) == 1
    assert lots[0].confirm_date == "2026-08-14"  # 确认日，非申请日
    assert lots[0].trade_date == "2026-08-13"


# ---------------- 7. 赎回冻结 ----------------

def test_redeem_freezes_shares():
    acc = FundAccount(100000, min_holding_days=0)
    acc.subscribe("半导体", "007301", 10000, "2026-08-13", "2026-08-14", nav_est=1.0)
    acc.confirm_orders("2026-08-14", {"半导体": 1.0})
    cash_before = acc.cash
    o = acc.redeem("半导体", "007301", 5000, "2026-08-14", "2026-08-17",
                   nav_est=1.0)
    assert o.status == "pending"
    assert acc.frozen_shares("半导体") == 5000
    assert acc.total_shares("半导体") == 10000  # 份额还在（冻结）
    assert acc.cash == cash_before  # 现金未到账


# ---------------- 8. 赎回确认现金到账 ----------------

def test_redeem_confirm_cash_arrives():
    acc = FundAccount(100000, min_holding_days=0)
    acc.subscribe("半导体", "007301", 10000, "2026-08-13", "2026-08-14", nav_est=1.0)
    acc.confirm_orders("2026-08-14", {"半导体": 1.0})
    acc.redeem("半导体", "007301", 5000, "2026-08-14", "2026-08-17", nav_est=1.0)
    confirmed = acc.confirm_orders("2026-08-17", {"半导体": 1.2})
    assert len(confirmed) == 1
    assert acc.total_shares("半导体") == 5000
    assert acc.frozen_shares("半导体") == 0
    assert acc.cash == pytest.approx(90000 + 5000 * 1.2)


# ---------------- 9. 超额赎回拒绝 ----------------

def test_redeem_over_available_rejected():
    acc = FundAccount(100000, min_holding_days=0)
    acc.subscribe("半导体", "007301", 10000, "2026-08-13", "2026-08-14", nav_est=1.0)
    acc.confirm_orders("2026-08-14", {"半导体": 1.0})
    with pytest.raises(ValueError):
        acc.redeem("半导体", "007301", 10001, "2026-08-14", "2026-08-17")


# ---------------- 10. 清仓允许 ----------------

def test_liquidation_full_available_shares():
    acc = FundAccount(100000, min_holding_days=0)
    acc.subscribe("半导体", "007301", 30000, "2026-08-13", "2026-08-14", nav_est=1.0)
    acc.confirm_orders("2026-08-14", {"半导体": 1.0})
    d = create_decision("SELL", "半导体", 0, 0.9, "清仓", "风险", current_position=30)
    ex = Executor(acc)
    r = ex.risk.check(d, 30)
    assert r.allowed  # 30 -> 0 超过 20%，但清仓豁免
    trade = ex.execute(d, {"半导体": 1.0}, "2026-08-14")
    assert trade.side == "REDEEM"
    assert trade.shares == pytest.approx(30000)


# ---------------- 11-13. 交易日历 ----------------

@pytest.fixture
def cal():
    return TradingCalendar()


def test_trade_date_before_1500(cal):
    dt = datetime(2026, 8, 13, 14, 30)  # 周四
    assert cal.trade_date_of(dt) == date(2026, 8, 13)


def test_trade_date_after_1500(cal):
    dt = datetime(2026, 8, 13, 15, 30)
    assert cal.trade_date_of(dt) == date(2026, 8, 14)  # 周五


def test_weekend_rollover(cal):
    # 周五 15:00 后 -> 跳过周六日 -> 周一
    dt = datetime(2026, 8, 14, 16, 0)
    assert cal.trade_date_of(dt) == date(2026, 8, 17)
    # 周六下单 -> 周一
    assert cal.trade_date_of(datetime(2026, 8, 15, 10, 0)) == date(2026, 8, 17)


def test_confirm_date_skips_weekend(cal):
    d = cal.next_trading_day(date(2026, 8, 14))  # 周五的下一交易日
    assert d == date(2026, 8, 17)


# ---------------- 14. 总资产守恒 ----------------

def test_total_asset_constant_during_subscribe():
    acc = FundAccount(100000)
    acc.subscribe("半导体", "007301", 20000, "2026-08-13", "2026-08-14",
                  nav_est=4.0)
    total = acc.total_asset({"半导体": 4.0})
    assert total == pytest.approx(100000)  # 不是 80000


def test_total_asset_constant_during_redeem():
    acc = FundAccount(100000, min_holding_days=0)
    acc.subscribe("半导体", "007301", 20000, "2026-08-13", "2026-08-14", nav_est=1.0)
    acc.confirm_orders("2026-08-14", {"半导体": 1.0})
    acc.redeem("半导体", "007301", 5000, "2026-08-14", "2026-08-17", nav_est=1.0)
    total = acc.total_asset({"半导体": 1.0})
    assert total == pytest.approx(100000)


# ---------------- 15. 不残留旧场内逻辑 ----------------

def test_no_legacy_exchange_methods():
    acc = FundAccount(100000)
    assert not hasattr(acc, "buy")
    assert not hasattr(acc, "sell")
    assert not hasattr(acc, "calculate_fee")


def test_7day_rule_configurable():
    # 默认 7 天：确认日当天不可赎
    acc = FundAccount(100000)
    acc.subscribe("半导体", "007301", 10000, "2026-08-13", "2026-08-14", nav_est=1.0)
    acc.confirm_orders("2026-08-14", {"半导体": 1.0})
    assert acc.available_shares("半导体", "2026-08-14") == 0  # 持有 0 天
    assert acc.available_shares("半导体", "2026-08-21") == 10000  # 满 7 天
    # 可配置关闭
    acc2 = FundAccount(100000, min_holding_days=0)
    acc2.subscribe("半导体", "007301", 10000, "2026-08-13", "2026-08-14", nav_est=1.0)
    acc2.confirm_orders("2026-08-14", {"半导体": 1.0})
    assert acc2.available_shares("半导体", "2026-08-14") == 10000
