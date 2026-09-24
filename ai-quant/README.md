# ai-quant · OrangePi 量化系统

Pi（`orangepizero2w`）上的 AI 量化研究与模拟交易系统：**股票组合模拟盘**
（CLI 选股 + LLM 决策 + A股账本 + 全缓存回测）、旧版场外 ETF 联接 C 类基金
模式（etf-c，保留可切换）、每日复盘与 U 盘快照备份，并接收本机
stock-analyzer 通过 `--push` 推送的分析报告。

## 目录

| 路径 | 用途 |
|---|---|
| `main.py` | 入口：读取 `config/system.yaml` |
| `agent/` | 决策链路（stock_decision 股票组合 / ensemble 基金多模型） |
| `models/` | 模型客户端（deepseek / embedding / prompt / stock_prompt） |
| `config/` | 运行配置（system / model / risk / **stock**） |
| `trading/` | 账户与执行（**stock_account/stock_executor** + 基金 account/executor） |
| `sim/` | 模拟盘：`run.py` 双模式调度，`stock_run.py` / `fund_run.py` |
| `backtest/` | 回测：`stock_engine.py` 股票组合全缓存回测 + 基金引擎 |
| `data/` | 行情、基金映射、特征、历史缓存；`data/stock/cli_bridge.py` 桥接 CLI |
| `memory/` | 记忆与收件箱（`inbox/` 推送落点，`daily/` 每日总结/复盘） |
| `scripts/` | 运维脚本；`scripts/cli/` 为独立单文件 CLI（生成物，勿手改） |
| `archive/` | 历史备份归档（`*.bak*`、旧快照，不参与运行） |

## 股票模拟盘

- **实盘（每交易日 14:50，cron `python -m sim.run`）**：
  CLI 缓存选股 → 腾讯实时行情 → `deepseek-v4.1-flash`（`deepseek-flash`）
  组合决策 → `StockExecutor` 按 A股规则成交 → 状态/复盘/RAG 记忆/U 盘快照
  ```bash
  .venv/bin/python -m sim.run --dry-run --no-rag   # 试跑
  .venv/bin/python -m sim.run --mode fund          # 旧版 etf-c 基金模式
  ```
- **全缓存回测**：
  ```bash
  .venv/bin/python scripts/stock_backtest.py                  # 全量（约4860只）
  .venv/bin/python scripts/stock_backtest.py --limit 300      # 抽样
  .venv/bin/python scripts/stock_backtest.py --mode 激进 --exec-px open
  ```
  信号 T 日收盘生成、T+1 成交；ATR(14) 跟踪止损；100股整手/T+1/佣金
  万2.5+印花税0.05%；基准=等权全市场 + 上证指数。结果落
  `backtest/results/`（JSON + markdown 报告）。
  - 实测（2020-02~2026-09）：市值前 300 年化 **+16.1%**、Sharpe **1.01**
    （超额上证 +63.9pp）；全市场 +1.0% —— **信号边际集中在大市值**，
    实盘候选默认按市值前 120 扫描与此一致。
- **A股规则**：整手买入、T+1 可卖、涨跌停不成交、最多 5 只、单票 ≤20%。

## CLI（预测/选股引擎）

```bash
cd scripts/cli && python3 stock_predict.py 000725
```

- 缓存：`scripts/cli/stock_cache.db`（全市场日K，约 813 万根）
- AI：优先 `DEEPSEEK_API_KEY` / `stock_gui.ini [deepseek]`；缺省回退主目录
  opencode-go 授权（`~/.local/share/opencode/auth.json`）
- 支持 `--tiers` / `--research` / `--push` 等（见文件头注释）

## 模型与记忆

| 用途 | 配置 | 说明 |
|---|---|---|
| 决策 | `config/model.yaml` → `deepseek-flash` | DeepSeek-V4.1-Flash，`api.deepseek.com`，Key 在 `.env` |
| 记忆 | Ollama `qwen3-embedding:0.6b` | 相似历史检索（RAG），失败自动降级 |

## 备份

```bash
.venv/bin/python scripts/usb_backup.py --label stock --keep 14
```

时间戳快照 + `manifest.json`（sha256）+ 复制校验 + 保留最近 14 份，
挂载点自动探测（`/media/usb` 等）。

## 约定

- 密钥只放 `.env` 与主目录授权文件，不入库（`.env` 已在 .gitignore）
- 历史备份不散落：统一移入 `archive/`；大文件（缓存、生成物）不入 git
