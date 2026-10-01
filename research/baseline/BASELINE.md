# Baseline（v6.1.8 高风险档 + P0/P1 + P0 二轮修复）

> **历史快照**：本页记录 v6.1.8 时点的高风险档（当时为「激进破甲版」+500 日 MDD 止损）与回测耗时基线；
> v6.1.9 已将该档重做为板块轮动策略并删除 MDD 止损机制，正文中的 MDD 口径描述仅作历史对照。

## 回测耗时基线（实测）

| 脚本 | v6.1.7 baseline | v6.1.8 后 | 提速 |
|---|---|---|---|
| `backtest_v61.py`（4 口径全期） | **127.9s** | **74s** | -42% |
| `stock_backtest_export.py`（6898 只 × 4 档） | **260s** | **203s** | -22% |
| `backtest_strategy_ablation.py`（7181 只 × 4 档） | **509s** | **373s** | -27% |

## 数字漂移（与 v6.1.7 高风险档对比）
- **组合层 v61**：完全一致（0pp 漂移）—— 组合引擎 `tier_sim_phase` 不设止损
- **perstock 高风险 修复前**：总收益中位 +63.22%（与激进完全相等，**P0 bug**——500 日 MDD 形同虚设）
- **perstock 高风险 修复后**（本次提交）：总收益 +37.24%（-25.98pp），胜率 56%（+6pp），
  盈亏比 1.48（-0.73），验证段 -2.62%（样本外仍接近 0）—— 呈现「高胜率·低赔率」真实画像
- **perstock 其他档**：保守/稳健/激进**完全不受影响**
- **消融**：训练/验证漂移 1-2pp（高风险候选入选 pool + 数据微变）

## P0 二轮修复（2026-09-30）
1. **P0-12 高风险档止损参数实际未生效（v6.1.7 高风险档起即存在）**：
   - `tier_picks_from_ablation` / `stock_backtest_export` 的 高风险档直接取
     选型候选 `pk.get("params")`，若选型选中 `mode='激进'` 候选则 高风险档
     实际跑激进 ATR 止损（ATR2.5x），500 日 MDD 形同虚设
   - 修复：高风险档强制 `RISK_PARAMS["高风险"]`，选型只决定 algo/信号源
2. **P0-3 生产端 高风险 参考止损与回测口径差 1 根**：
   - 改 `_mdd_stop_dist(C[k], d+2, _ddw)` 与回测引擎执行日一致
3. **过拟合相关 docstring 误导修复**：
   - `_ablation_recent` / `pick_ablation_consistent` 旧 docstring 误称
     「近窗 = 验证段」，加 ⚠ 防回归注释（v6.1.5 热修⑥ 已修实现，本轮只同步文档）

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
- `python3 build_cli.py` → 重生成 `stock_predict.py` (465 KB)
- `python3 backtests/v61_dashboard.py` → 仪表盘刷新 OK（24 MB / 27592 只 perstock）

## 提交链
- `247815a` v6.1.8: 高风险档按500日历史最大回撤止损 + 四口径并行回测 P1(75.6s)/年化口径 P0
- `b1d9ea3` v6.1.8 P1: perstock + 消融多进程并行 + 跨档指标复用
- 本次提交：v6.1.8 P0 二轮修复（高风险 止损强制 高风险 参数 + 生产端 MDD 口径对齐 + docstring 防回归）
