# -*- coding: utf-8 -*-
"""插件基类。

所有插件继承 :class:`StockPlugin` 并实现 :meth:`build_panel`。
宿主（stock_gui.py）负责加载、挂载到右侧栏、并在对应时机回调。
"""


class StockPlugin:
    """股票分析器插件基类。

    生命周期（均由宿主调用）：

    - ``__init__(api)``        加载插件时调用一次。
    - ``build_panel(parent)``  需要显示面板时调用；主题切换会销毁重建，可多次。
    - ``on_analysis(result)``  每次分析完成 / 切换股票后调用。
    - ``on_tick(quote)``       行情快照刷新时调用（约每 5 秒）。
    - ``on_theme_changed()``   主题切换、面板重建完成后调用。
    - ``on_close()``           程序退出时调用（保存数据用）。

    注意：``build_panel`` / ``on_analysis`` / ``on_tick`` / ``on_theme_changed``
    都在 Tk 主线程执行；插件内部若另起线程，更新界面必须经
    ``self.api.on_ui_thread(fn)`` 调度回主线程。
    """

    #: 侧边栏下拉框显示名，需唯一
    name = "未命名插件"
    #: 侧边栏排序，数值越小越靠前
    order = 100

    def __init__(self, api):
        self.api = api

    # ------------------------------------------------------------------
    # 需要子类实现
    # ------------------------------------------------------------------
    def build_panel(self, parent):
        """创建并返回插件面板控件（tk 容器）。

        :param parent: 右侧栏内容容器，面板应创建为它的子控件。
        :return: 已创建但**尚未** pack/grid 的控件；宿主负责挂载。
        :raises NotImplementedError: 未实现。
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 可选钩子（默认空实现）
    # ------------------------------------------------------------------
    def on_analysis(self, result):
        """分析完成 / 切换股票。

        :param result: ``analyze()`` 返回的结果字典，字段见接口文档。
        """

    def on_tick(self, quote):
        """行情快照刷新（现价/涨跌等），约每 5 秒一次。"""

    def on_theme_changed(self):
        """主题切换后面板已重建，可在此重新套用颜色。"""

    def on_close(self):
        """宿主退出，保存插件数据。"""
