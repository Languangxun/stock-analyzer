# AGENTS.md

## 项目约定

- **每次改动都要写进 `README.md`**：在顶部当前版本摘要追加条目（日期 + 一句话 + 关键行为）；
  涉及数据层/算法层/目录结构时同步更新 `ARCHITECTURE.md` 正文小节，并在第七节「变更索引」补一行。
- `stock_gui.py` 是**唯一算法源**；改完后必须运行 `python build_cli.py` 重新生成
  `stock_predict.py`（生成物勿手改）。
- 数据源改动优先复用现有多源 / 熔断 / 代理机制（`_open_url`、`_SRC_CB`、`_fetch_remote_rows`、
  `fetch_batch_quotes`），不要新增单源调用；新增国内行情域名需补进 `_DOMESTIC_SUFFIX`。

## 提交前检查（本仓库无 lint 配置）

- `python3 -m py_compile stock_gui.py stock_predict.py`
- `python3 test_settings.py`（GUI 冒烟，需图形环境）
- 涉及数据源：`python3 stock_firstaid.py --check`
