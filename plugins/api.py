# -*- coding: utf-8 -*-
"""插件宿主 API。

插件通过 ``self.api`` 使用本对象读取行情/结果、调度主线程、读写私有数据。
"""

import os


class PluginAPI:
    """宿主暴露给插件的只读/工具接口。"""

    def __init__(self, app, plugin_dir):
        self._app = app
        self._plugin_dir = plugin_dir

    # ------------------------------------------------------------------
    # 行情 / 分析结果
    # ------------------------------------------------------------------
    @property
    def current_code(self):
        """当前股票代码（如 ``sz002491``）；无分析结果时为 ``None``。"""
        return (self._app.res or {}).get("full_code")

    @property
    def current_name(self):
        """当前股票名称；未知时为 ``None``。"""
        return ((self._app.res or {}).get("quote") or {}).get("name")

    @property
    def current_result(self):
        """当前完整分析结果字典（``analyze()`` 返回），无则 ``None``。"""
        return self._app.res

    @property
    def current_view(self):
        """当前图表视图（``slice_view`` 返回），初始可能为 ``None``。"""
        return getattr(self._app, "view", None)

    # ------------------------------------------------------------------
    # 线程 / 状态栏
    # ------------------------------------------------------------------
    def on_ui_thread(self, fn):
        """把 ``fn`` 调度到 Tk 主线程执行（后台线程更新界面必须走这里）。"""
        self._app._safe_after(0, fn)

    def set_status(self, text):
        """在状态栏显示一条文本。"""
        self.on_ui_thread(lambda: self._app.progress_var.set(text))

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------
    def colors(self):
        """当前主题颜色字典（键：DARK_BG / PANEL_BG / FG_MAIN / UP / DOWN 等）。"""
        import stock_gui as sg
        keys = ("UP", "DOWN", "PRED_C", "TPRED_C", "BG", "GRID_C", "GUIDE_C",
                "AXIS_TXT", "TITLE_TXT", "CROSS_C", "DARK_BG", "PANEL_BG",
                "FIELD_BG", "FG_MAIN", "BTN_BG", "BTN_FG", "BTN_HOVER",
                "BTN_BORDER")
        return {k: getattr(sg, k) for k in keys if hasattr(sg, k)}

    def current_theme(self):
        """当前主题名：``dark`` / ``light`` / ``contrast``。"""
        return self._app.settings.get("theme", "dark")

    # ------------------------------------------------------------------
    # 私有数据 / 日志
    # ------------------------------------------------------------------
    def storage_path(self, filename):
        """返回插件私有数据文件路径（自动创建 ``plugins/_data`` 目录）。"""
        d = os.path.join(self._plugin_dir, "_data")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, filename)

    @property
    def log(self):
        """宿主 logger（``logging.Logger``），插件用它记录日志。"""
        import stock_gui as sg
        return sg.log
