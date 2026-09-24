"""StockAccount 单元测试：费用 / T+1 / 整手 / 已实现收益 / 持久化。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.stock_account import StockAccount


def test_buy_fee_and_cost():
    acc = StockAccount(100000)
    t = acc.buy("sz000725", 10.0, 100, "2026-09-24", "2026-09-25")
    assert t.shares == 100 and t.amount == 1000
    assert abs(t.fee - (5.0 + 0.01)) < 1e-9
    assert abs(acc.cash - (100000 - 1005.01)) < 1e-9
    assert abs(acc.lots["sz000725"][0].cost - 10.0501) < 1e-9


def test_lot_size_enforced():
    acc = StockAccount(100000)
    try:
        acc.buy("sz000725", 10.0, 150, "2026-09-24", "2026-09-25")
        raise AssertionError("非整手应报错")
    except ValueError:
        pass


def test_t_plus_1():
    acc = StockAccount(100000)
    acc.buy("sz000725", 10.0, 100, "2026-09-24", "2026-09-25")
    assert acc.available_shares("sz000725", "2026-09-24") == 0
    assert acc.available_shares("sz000725", "2026-09-25") == 100


def test_sell_realized_pnl():
    acc = StockAccount(100000)
    acc.buy("sz000725", 10.0, 100, "2026-09-24", "2026-09-25")
    t = acc.sell("sz000725", 11.0, 100, "2026-09-25")
    fee = 5.0 + 1100 * 5e-4 + 1100 * 1e-5
    assert abs(t.fee - fee) < 1e-9
    assert abs(t.realized - (1100 - fee - 1005.01)) < 1e-9
    assert acc.total_shares("sz000725") == 0
    assert not acc.lots


def test_sell_before_sellable_rejected():
    acc = StockAccount(100000)
    acc.buy("sz000725", 10.0, 100, "2026-09-24", "2026-09-25")
    try:
        acc.sell("sz000725", 11.0, 100, "2026-09-24")
        raise AssertionError("T+1 前卖出应报错")
    except ValueError:
        pass


def test_max_buy_shares_with_fee():
    acc = StockAccount(1000)
    shares = acc.max_buy_shares(10.0)
    assert shares == 0 or (shares * 10 + acc._buy_fee(shares * 10) <= 1000)


def test_state_roundtrip():
    acc = StockAccount(50000)
    acc.buy("sh600519", 100.0, 100, "2026-09-24", "2026-09-25")
    state = acc.to_state()
    acc2 = StockAccount.from_state(state)
    assert acc2.cash == acc.cash
    assert acc2.total_shares("sh600519") == 100
    assert abs(acc2.lots["sh600519"][0].cost
               - acc.lots["sh600519"][0].cost) < 1e-12
