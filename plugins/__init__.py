# -*- coding: utf-8 -*-
"""插件加载器。

扫描本目录下的 ``*.py``，把其中继承 :class:`StockPlugin` 的类实例化。
导入/实例化失败的插件会被跳过，不影响宿主。
"""

import importlib
import inspect
import os
import pkgutil

from .base import StockPlugin
from .api import PluginAPI

#: 插件目录（放一个 .py 即多一个插件）
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

#: 不作为插件的模块名
_SKIP = {"__init__", "base", "api"}

__all__ = ["StockPlugin", "PluginAPI", "PLUGIN_DIR", "discover", "load_all"]


def discover():
    """发现所有插件类。

    :return: ``[(module, plugin_class), ...]``，导入失败的模块被跳过。
    """
    out = []
    for m in pkgutil.iter_modules([PLUGIN_DIR]):
        if m.name.startswith("_") or m.name in _SKIP:
            continue
        try:
            mod = importlib.import_module("{}.{}".format(__name__, m.name))
        except Exception:
            continue
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if (issubclass(cls, StockPlugin) and cls is not StockPlugin
                    and cls.__module__ == mod.__name__):
                out.append((mod, cls))
    return out


def load_all(api, log=None):
    """实例化所有插件。

    :param api: :class:`PluginAPI` 实例。
    :param log: 可选 logger，用于记录失败。
    :return: 已按 ``order`` 排序的插件实例列表。
    """
    plugins = []
    for _, cls in discover():
        try:
            plugins.append(cls(api))
        except Exception:
            if log is not None:
                log.exception("插件 %s 实例化失败",
                              getattr(cls, "name", cls.__name__))
    plugins.sort(key=lambda p: getattr(p, "order", 100))
    return plugins
