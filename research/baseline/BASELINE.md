# Baseline（v6.1.8 bata 改激进破甲版 + P0/P1）

## 回测耗时基线（实测，commit 8e0d91c/247815a/本次提交）

| 脚本 | v6.1.7 baseline | v6.1.8 P1 后 | 提速 |
|---|---|---|---|
| `backtest_v61.py`（4 口径全期） | **127.9s** | **74s** | -42% |
| `stock_backtest_export.py`（6898 只 × 4 档） | **260s** | **204s** | -22% |
| `backtest_strategy_ablation.py`（7181 只 × 4 档） | **509s** | **373s** | -27% |

## v61 全期数字漂移（与 v6.1.7 bata 改激进破甲版对比）
- 组合层 v61：完全一致（浮点 <0.01pp）—— 组合引擎 `tier_sim_phase` 不设止损，
  bata 改 MDD 止损 + P0 年化口径修复只影响单股 `_bt_events`/`_bt_simulate`，不影响组合层
- perstock bata 总收益中位：+53.33% → +63.22%（+9.89pp，bata 改 MDD 止损 + 新选型叠加）
- perstock bata 验证段中位：-1.47% → -1.87%（-0.40pp，在容差内）
- 消融训练/验证中位漂移 1-2pp（bata 候选入选 pool + 数据微变）

## 环境
8 核 / 14GB 内存（Linux 5.x，Python 3.14.4），库内 ~1272 万根 / 7624 只（2026-09-29）

## 优化点
1. **v61 全期**：父进程预热面板+特征后 fork 继承（避免每进程重复加载 1.6G 库争 IO）；
   修复初版每进程独立加载 1.6G 库导致 175.9s 反比串行慢的退化
2. **perstock**：`map+chunksize=20` 批量提交；4 档共用 ATR(14) 预计算 +
   composite 算法 4 档共用 `_composite_precompute`（ATR/pre 不依赖 rp/sigs）
3. **消融**：`map+chunksize` 批量提交；workers 默认 8 → 16（os.cpu_count 上限）

## 验证
- `python3 -m py_compile stock_gui.py stock_predict.py backtests/*.py` → OK
- `python3 test_settings.py` → GUI 冒烟通过（窗口 964×994，无 errors）
- `python3 build_cli.py` → 重生成 `stock_predict.py` (464 KB)
- `python3 backtests/v61_dashboard.py` → 仪表盘刷新 OK（24 MB / 27592 只 perstock）

## 提交
- v6.1.8 bata 改激进破甲版 + P0 + P1（v61 全期并行）→ 247815a
- v6.1.8 P1 perstock + 消融并行 + 跨档指标复用 → 本次提交
