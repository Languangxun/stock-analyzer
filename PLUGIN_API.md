# 插件接口文档 · 右侧栏插件

> 版本 1.0　适用于 `stock_gui.py`（含 `build_cli.py` 生成的 CLI 不加载插件）
> 目标：在右侧栏注册一个自己的面板（例如「交易记录」），与「预测参考」并列切换。

---

## 一、总览

- 右侧栏顶部有一个下拉选择器：**预测参考** + 已加载的插件。
- 每在 `plugins/` 目录放一个 `.py`，就多一个插件；重启程序后自动出现。
- 插件面板由宿主挂载进右侧栏内容区；主题切换会销毁并**重建**面板。
- 插件加载/回调失败只会记日志，不影响主程序。
- 小屏（`compact`，宽≤700 或 高≤500）不显示右侧栏，插件面板也不会挂载。

目录结构：

```
stock_predict/
├─ stock_gui.py            # 唯一算法源（GUI）
├─ PLUGIN_API.md           # 本文档
└─ plugins/
   ├─ __init__.py          # 加载器（勿改）
   ├─ base.py              # StockPlugin 基类（勿改）
   ├─ api.py               # PluginAPI 宿主接口（勿改）
   ├─ example_plugin.py    # 示例插件（可删）
   └─ your_plugin.py       # ← 你的插件放这里
```

---

## 二、快速开始

在 `plugins/` 下新建 `trade_log.py`：

```python
# -*- coding: utf-8 -*-
import tkinter as tk
from .base import StockPlugin


class TradeLogPlugin(StockPlugin):

    name = "交易记录"     # 侧边栏显示名（唯一）
    order = 10            # 越小越靠前

    def build_panel(self, parent):
        """创建面板；返回未 pack 的容器，宿主负责挂载。"""
        c = self.api.colors()
        self.frame = tk.Frame(parent, bg=c["DARK_BG"])
        self.label = tk.Label(self.frame, text="等待分析…",
                              bg=c["DARK_BG"], fg=c["FG_MAIN"], justify="left")
        self.label.pack(fill="both", expand=True, padx=6, pady=6)
        return self.frame

    def on_analysis(self, result):
        """分析完成/切换股票。"""
        if not result:
            return
        q = result.get("quote") or {}
        txt = "{} {}\n现价 {}".format(result.get("full_code"),
                                     q.get("name"), q.get("price"))
        self.api.on_ui_thread(lambda: self.label.config(text=txt))

    def on_theme_changed(self):
        c = self.api.colors()
        self.frame.config(bg=c["DARK_BG"])
        self.label.config(bg=c["DARK_BG"], fg=c["FG_MAIN"])
```

保存 → 重启 `stock_gui.py` → 右侧栏下拉里出现「交易记录」。

---

## 三、插件基类 `StockPlugin`

```python
from plugins.base import StockPlugin
```

| 属性 | 类型 | 说明 |
|---|---|---|
| `name` | `str` | 侧边栏显示名，需唯一；重名会被跳过。 |
| `order` | `int` | 侧边栏排序，越小越靠前（默认 100）。 |

生命周期（宿主自动调用，均在 **Tk 主线程**）：

| 方法 | 时机 | 说明 |
|---|---|---|
| `__init__(self, api)` | 加载插件 | 保存 `self.api = api`；不要在这里创建 Tk 控件。 |
| `build_panel(self, parent)` | 需要显示面板 | **必须实现**。创建并返回容器控件，宿主挂载；主题切换会再次调用，需可重复构建。 |
| `on_analysis(self, result)` | 分析完成/切换股票 | `result` 见第五节；可能较频繁（自动刷新/秒级 tick 后重分析）。 |
| `on_tick(self, quote)` | 行情快照刷新（约 5 秒） | `quote` 见第六节；仅当有分析结果时触发。 |
| `on_theme_changed(self)` | 主题切换、面板重建后 | 用 `self.api.colors()` 重新套色。 |
| `on_close(self)` | 程序退出 | 保存插件数据。 |

---

## 四、宿主接口 `self.api`（`PluginAPI`）

### 4.1 行情 / 结果（只读属性）

| 成员 | 返回 | 说明 |
|---|---|---|
| `current_code` | `str \| None` | 当前股票代码，如 `sz002491`。 |
| `current_name` | `str \| None` | 当前股票名称。 |
| `current_result` | `dict \| None` | 完整分析结果（第五节）。 |
| `current_view` | `dict \| None` | 图表视图（可含可见区间）。 |

### 4.2 线程 / 状态栏

| 方法 | 说明 |
|---|---|
| `on_ui_thread(fn)` | 把 `fn` 调度到 Tk 主线程；**后台线程更新界面必须用它**。 |
| `set_status(text)` | 在底部状态栏显示一条文本。 |

> 插件内部可以任意开后台线程做 IO/计算，但**任何 Tk 控件操作都要经 `on_ui_thread`**。

### 4.3 主题

| 方法 | 返回 | 说明 |
|---|---|---|
| `colors()` | `dict` | 当前主题颜色，键见下表。 |
| `current_theme()` | `str` | `dark` / `light` / `contrast`。 |

颜色键：`UP`、`DOWN`、`PRED_C`、`TPRED_C`、`BG`、`GRID_C`、`GUIDE_C`、
`AXIS_TXT`、`TITLE_TXT`、`CROSS_C`、`DARK_BG`、`PANEL_BG`、`FIELD_BG`、
`FG_MAIN`、`BTN_BG`、`BTN_FG`、`BTN_HOVER`、`BTN_BORDER`。

### 4.4 数据存储 / 日志

| 方法 | 说明 |
|---|---|
| `storage_path(filename)` | 返回 `plugins/_data/filename` 绝对路径，目录自动创建。交易记录等持久化数据建议放这里。 |
| `log` | 宿主 `logging.Logger`，用于记录日志。 |

### 4.5 插件 → 宿主：账户上下文（可选约定）

宿主会在「每日荐股」等信号展示前，探测插件是否实现 `account_context()`；
实现则读取账户快照做联动（可买手数 / 已持仓标注），未实现或未填本金时
**自动降级为普通分析**。返回格式：

```python
{
    "available": True,          # False = 未填本金，宿主不联动
    "capital": 100000.0,        # 总资产（本金，元）
    "cash": 42000.0,            # 可用现金（本金 + 卖出 - 买入 - 费用）
    "positions": {              # 当前持仓
        "sz300750": {"name": "宁德时代", "volume": 100, "avg_cost": 210.5},
    },
    "source": "交易记录插件",
}
```

约定：
- 宿主仅在 `available=True` 时联动；异常/缺字段按不可用处理；
- 「买不起」（100 股一手金额 > 可用现金）的标的仍正常展示分析，
  仅不做账户联动（用户明示口径）；
- 该方法应为纯读取，不在其中修改数据。

---

## 五、分析结果 `result`（`on_analysis` 参数）

字典，主要字段：

| 键 | 类型 | 说明 |
|---|---|---|
| `quote` | `dict` | 实时行情快照，见第六节。 |
| `full_code` | `str` | 标准化代码（如 `sz002491`）。 |
| `prev_close` | `float` | 昨收。 |
| `action` | `str` | 多维评估结论（如 `BUY`/`SELL`/`HOLD`/观望等）。 |
| `signals` | `list` | 历史买卖点 `(index, date, "BUY"/"SELL", reason)`。 |
| `pred` | `dict` | 预测区间（`hi`/`lo` 各含 `P10..P90`）。 |
| `t_pred` | `dict` | T 日盘中预测区间。 |
| `t5_pred` | `dict \| None` | T+5 预测。 |
| `multi_pred` | `list` | T+1..T+10 多日预测累计曲线。 |
| `levels` | `list` | 三级样本池（L1/L2/L3）及 `up_prob`。 |
| `sector_name` | `str` | 所属行业。 |
| `sector_chg_today` | `float` | 行业当日涨跌（%）。 |
| `idx_chg_today` | `float` | 大盘当日涨跌（%）。 |
| `disp_rows` | `list[dict]` | 展示用日K：`date/open/high/low/close/vol`。 |
| `has_live` | `bool` | 是否含盘中实时 bar。 |
| `live_high` / `live_low` | `float \| None` | 盘中最高/最低。 |
| `gap_today` | `float` | 今日跳空（%）。 |
| `strategy` / `sel_algo` | `dict` / `str` | 当前所选策略。 |
| `quick` | `bool` | 是否为快速首屏（样本池可能还在后台补齐）。 |

> 结果字段随版本演进，插件请用 `.get()` 取值并做好缺省。
> `quick=True` 时会先给一份基础结果，随后 `_apply_progressive` 更新预测但**不会**再触发 `on_analysis`；如需在样本池补齐后刷新，可在面板上加“刷新”按钮读 `current_result`。

---

## 六、行情快照 `quote`（`on_tick` 参数）

```python
{"name": "通鼎互联", "price": 4.56, "prev_close": 4.50,
 "open": 4.52, "high": 4.60, "low": 4.48, "time": "20260912143000"}
```

| 键 | 类型 | 说明 |
|---|---|---|
| `name` | `str` | 名称。 |
| `price` | `float` | 现价。 |
| `prev_close` | `float` | 昨收。 |
| `open` / `high` / `low` | `float` | 今开 / 最高 / 最低。 |
| `time` | `str` | 快照时间戳（`YYYYMMDDHHMMSS`）。 |

---

## 七、注意事项

1. **不要修改** `plugins/base.py`、`plugins/api.py`、`plugins/__init__.py`；升级可能覆盖。
2. `build_panel` 会在主题切换时被重新调用。请把界面状态放在插件实例属性或 `storage_path` 文件里，不要依赖已被销毁的控件。
3. 插件是 **GUI 专属**：`build_cli.py` 生成的 `stock_predict.py` 不包含插件系统。
4. 小屏模式没有右侧栏，插件不会显示；如需小屏支持可自行在 `on_analysis` 里用 `Toplevel` 弹窗。
5. 单个插件抛异常只会被日志记录，不影响其它插件与主程序；但仍请自行 try/except 关键路径。
6. 数据持久化目录 `plugins/_data/` 为插件私有，建议 gitignore。
7. `on_analysis` 可能被自动刷新（默认 15 分钟）和秒级 tick 后的重分析触发，注意幂等与频率。

---

*本接口为内部扩展点，字段可能随版本调整；以 `stock_gui.py` 实际返回为准。*
