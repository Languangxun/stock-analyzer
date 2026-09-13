# -*- coding: utf-8 -*-
"""无头实测：设置窗口能否打开"""
import traceback
import tkinter as tk

import stock_gui as sg

errors = []
root = tk.Tk()
root.withdraw()
try:
    app = sg.App(root)
except Exception:
    traceback.print_exc()
    errors.append("App init")
try:
    app.open_settings()
    root.update()
    tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    print("Toplevel数量:", len(tops))
    if tops:
        w = tops[-1]
        print("设置窗口:", w.title(), "size:", w.winfo_width(), "x",
              w.winfo_height())
        # 检查按钮是否在可见区
        root.update_idletasks()
        print("窗口高:", w.winfo_reqheight(), "屏幕高:", w.winfo_screenheight())
        w.destroy()
except Exception:
    traceback.print_exc()
    errors.append("open_settings")
root.destroy()
print("errors:", errors)
