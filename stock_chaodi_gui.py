#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抄底策略选股工具 GUI（通达信 VAR2 抄底体系，参考 stock_gui.py）

公式来源：
    VAR1:=(MA(CLOSE,80)-MA(CLOSE,13)/3);
    VAR2:=MA((CLOSE-VAR1)/VAR1,1);
    买点1:IF(CROSS(VAR2,0) AND LOW/REF(HIGH,1)<1.012,20,0);
    最佳点:IF(COUNT(VAR2>REF(VAR2,1),3)=3 AND COUNT(VAR2<0,10)=10
            AND REF(VAR2,3)=LLV(VAR2,10),60,0);
    买点2:IF(REF(VAR2,2)=LLV(VAR2,20) AND REF(VAR2,2)<0.071
            AND REF(VAR2,2)<REF(VAR2,1)
            AND NOT(REF(LOW,1)>REF(HIGH,2) AND LOW>REF(HIGH,1))
            AND CLOSE>REF(CLOSE,1),20,0);
    MMA:=EMA(VAR2,12)/1.428571;  MMB:=EMA(VAR2,3);
    快到底:IF(LLV(MMB-MMA,12)>0,0,-30);
    底初选股:IF(CROSS(0,LLV(MMB-MMA,12)),10,0);
    DIFF:(EMA(CLOSE,12)-EMA(CLOSE,26))/0.01;  DEA:EMA(DIFF,9);
    MACD:=(DIFF-DEA)/0.5;
    抄底:IF(快到底<0 AND CROSS(MACD,0),30,0);

功能：
    · 个股日K主图 + 买点1/最佳点/买点2/抄底 标记（含盘中实时bar）
    · VAR2/MMA/MMB 副图 + 快到底状态条 + 底初选股标记
    · MACD 副图（通达信四色柱：绿/#FFCC33/黄/品红）
    · 信号明细表 + 历史信号胜率统计 + CSV 导出
    · 全市场后台多线程扫描，双击结果直接看详情
数据层复用 stock_gui.py（stock_cache.db 本地缓存 + 腾讯行情增量）。
仅统计参考，不构成投资建议。
"""

import configparser
import csv
import datetime
import math
import os
import sqlite3
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk, messagebox, filedialog

try:
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
except ImportError:                                     # pragma: no cover
    raise SystemExit(
        "缺少 numpy，请使用 stock_predict/.venv/bin/python3 运行本程序")

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import stock_gui as sg                                  # noqa: E402

DISCLAIMER = "仅统计参考，不构成投资建议"

# ==================== 配色（参考 stock_gui 暗色主题） ====================
BG = "#14181e"
DARK_BG = "#101418"
PANEL_BG = "#171c22"
FIELD_BG = "#1c232b"
GRID_C = "#232b34"
GUIDE_C = "#39434e"
AXIS_TXT = "#8fa0ad"
TITLE_TXT = "#aebccb"
CROSS_C = "#9fb3c8"
FG_MAIN = "#d7dee6"
BTN_BG = "#222a33"
BTN_FG = "#d7dee6"
BTN_HOVER = "#2b3540"
BTN_BORDER = "#333e4a"
UP = "#ff5252"                                          # 涨（红）
DOWN = "#26c281"                                        # 跌（绿）
MA_COLORS = {5: "#ffa94d", 10: "#74c0fc", 13: "#e599f7",
             20: "#69db7c", 80: "#d5a021"}
VAR2_C = "#4dd0e1"
MMA_C = "#ffa94d"
MMB_C = "#74c0fc"
KDD_C = "#26c281"
DIF_C = "#ff5252"
DEA_C = "#ffffff"
MACD_COLORS = {"rise_above": "#ff00ff", "rise_below": "#ffd43b",
               "fall_above": "#26c281", "fall_below": "#ffcc33"}
SIG_COLORS = {"最佳点": "#ffd43b", "买点1": "#ff5252",
              "买点2": "#ff922b", "抄底": "#e64980",
              "底初选": "#4dabf7"}
SIG_ORDER = ("最佳点", "买点1", "买点2", "抄底", "底初选")
SIG_KEY = {"最佳点": "best", "买点1": "buy1", "买点2": "buy2",
           "抄底": "chaodi", "底初选": "di_cx"}

TICK_MS = 60 * 1000                                     # 行情快照刷新
CFG_PATH = os.path.join(_HERE, "stock_chaodi.ini")

FORMULA_TEXT = """通达信公式（信号显示高度已忽略，仅取触发条件）

VAR1:=(MA(CLOSE,80)-MA(CLOSE,13)/3);
VAR2:=MA((CLOSE-VAR1)/VAR1,1);

买点1: CROSS(VAR2,0) 且 LOW/REF(HIGH,1)<1.012
最佳点: 连续3日VAR2抬高 且 最近10日VAR2全为负
        且 3日前的VAR2恰为10日最低
买点2: 2日前VAR2创20日新低 且 <0.071 且昨日回升
        且 非跳空高开形态 且 今日收阳
MMA:=EMA(VAR2,12)/1.428571;  MMB:=EMA(VAR2,3);
快到底: LLV(MMB-MMA,12)>0 时为0，否则-30（绿色底部区）
底初选股: LLV(MMB-MMA,12) 上穿0
抄底: 快到底<0 且 MACD 上穿0（DRAWICON 信号）

DIFF:(EMA(CLOSE,12)-EMA(CLOSE,26))/0.01
DEA:EMA(DIFF,9);  MACD:=(DIFF-DEA)/0.5
柱色: 红出绿/零下橙黄(#FFCC33)/零下转黄/红上品红
"""


# ==================== 策略计算（通达信语义） ====================

def _ma(a, n):
    """MA(X,N)：前 N-1 根无效。"""
    out = np.full(a.shape, np.nan)
    if a.size >= n:
        cs = np.concatenate(([0.0], np.cumsum(a)))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def _ema(a, n):
    """EMA(X,N)：从第一个有效值起递归，遇无效值中断并重置。
    tolist() 后纯 Python 标量运算（比 numpy 标量索引快数倍，扫描热路径）。"""
    out = np.full(a.shape, np.nan)
    k = 2.0 / (n + 1.0)
    e = None
    vals = a.tolist()
    for i, x in enumerate(vals):
        if x != x:                      # NaN
            e = None
            continue
        e = x if e is None else x * k + e * (1 - k)
        out[i] = e
    return out


def _ref(a, k):
    """REF(X,K)：K 根前的值，越界为无效。"""
    out = np.full(a.shape, np.nan)
    if k <= 0:
        out[:] = a
    elif a.size > k:
        out[k:] = a[:-k]
    return out


def _llv(a, n):
    """LLV(X,N)：N 日最低；窗口含无效值时结果无效。"""
    out = np.full(a.shape, np.nan)
    if a.size < n:
        return out
    sw = sliding_window_view(a, n)
    valid = ~np.isnan(sw).any(axis=1)
    mins = np.min(np.where(np.isnan(sw), np.inf, sw), axis=1)
    out[n - 1:] = np.where(valid, mins, np.nan)
    return out


def _count(cond, n):
    """COUNT(COND,N)：N 日内条件成立次数。"""
    c = np.asarray(cond, dtype=float)
    out = np.full(c.shape, np.nan)
    if c.size < n:
        return out
    cs = np.concatenate(([0.0], np.cumsum(c)))
    out[n - 1:] = cs[n:] - cs[:-n]
    return out


def _cross(a, b):
    """CROSS(A,B)：A 上穿 B（前一根在下，当前在上）。"""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.ndim == 0:
        a = np.full(b.shape, float(a))
    if b.ndim == 0:
        b = np.full(a.shape, float(b))
    return (a > b) & (_ref(a, 1) < _ref(b, 1))


def compute_strategy(rows):
    """按通达信公式逐条计算，返回与 rows 等长的 numpy 数组字典。"""
    n = len(rows)
    if n == 0:
        return None
    C = np.array([r["close"] for r in rows], dtype=float)
    H = np.array([r["high"] for r in rows], dtype=float)
    L = np.array([r["low"] for r in rows], dtype=float)
    V = np.array([r["vol"] for r in rows], dtype=float)

    with np.errstate(invalid="ignore", divide="ignore"):
        ma80 = _ma(C, 80)
        ma13 = _ma(C, 13)
        var1 = ma80 - ma13 / 3.0
        var2 = (C - var1) / var1
        v2_prev = _ref(var2, 1)
        # 买点1
        buy1 = _cross(var2, np.zeros(n)) & ((L / _ref(H, 1)) < 1.012)
        # 最佳点
        rise3 = _count(var2 > v2_prev, 3) == 3
        neg10 = _count(var2 < 0, 10) == 10
        best = rise3 & neg10 & (_ref(var2, 3) == _llv(var2, 10))
        # 买点2
        r1v2 = _ref(var2, 1)
        r2v2 = _ref(var2, 2)
        gap_bad = (_ref(L, 1) > _ref(H, 2)) & (L > _ref(H, 1))
        buy2 = ((r2v2 == _llv(var2, 20)) & (r2v2 < 0.071) & (r2v2 < r1v2)
                & (~gap_bad) & (C > _ref(C, 1)))
        # MMA / MMB / 快到底 / 底初选
        mma = _ema(var2, 12) / 1.428571
        mmb = _ema(var2, 3)
        llv12 = _llv(mmb - mma, 12)
        kdd = np.where(np.isnan(llv12), np.nan,
                       np.where(llv12 > 0, 0.0, -30.0))
        bottom = ~np.isnan(llv12) & (llv12 <= 0)
        di_cx = _cross(np.zeros(n), llv12)
        # DIFF / DEA / MACD
        diff = (_ema(C, 12) - _ema(C, 26)) / 0.01
        dea = _ema(diff, 9)
        macd = (diff - dea) / 0.5
        # 抄底
        chaodi = bottom & _cross(macd, np.zeros(n))
        # 副图用布林辅助均线（仅展示）
        ma = {p: _ma(C, p) for p in MA_COLORS}

    return {"close": C, "high": H, "low": L, "vol": V, "ma": ma,
            "var1": var1, "var2": var2, "mma": mma, "mmb": mmb,
            "kdd": kdd, "bottom": bottom, "di_cx": di_cx,
            "diff": diff, "dea": dea, "macd": macd,
            "buy1": buy1, "buy2": buy2, "best": best, "chaodi": chaodi}


def signal_events(strat):
    """全部历史信号 [(bar_index, 名称), ...]，按 bar 升序。"""
    if strat is None:
        return []
    out = []
    for i in range(len(strat["close"])):
        if strat["best"][i]:
            out.append((i, "最佳点"))
        if strat["buy1"][i]:
            out.append((i, "买点1"))
        if strat["buy2"][i]:
            out.append((i, "买点2"))
        if strat["chaodi"][i]:
            out.append((i, "抄底"))
    return out


def forward_pct(closes, i, days):
    """信号后 days 个交易日的收盘涨跌幅（%），不足返回 None。"""
    j = i + days
    if j >= len(closes) or i < 0:
        return None
    return (closes[j] / closes[i] - 1) * 100


def fnum(v, fmt="{:+.3f}", dash="-"):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return dash
    if math.isnan(f) or math.isinf(f):
        return dash
    return fmt.format(f)


# ==================== 绘图工具（参考 stock_gui 约定） ====================

def _geom(cv, n):
    w = max(cv.winfo_width(), 240)
    h = cv.winfo_height()
    if h <= 1:
        try:
            h = int(cv["height"])
        except Exception:
            h = 160
    h = max(h, 100)
    L, R, T, B = 58, 46, 16, 20
    pw, ph = max(w - L - R, 50), max(h - T - B, 50)
    return {"w": w, "h": h, "L": L, "R": R, "T": T, "B": B,
            "pw": pw, "ph": ph, "bw": pw / max(n, 1), "n": n}


def _pad_range(lo, hi, ratio=0.06):
    if not hi > lo:
        hi = lo + 1.0
    pad = (hi - lo) * ratio
    return lo - pad, hi + pad


def _axes(cv, g, lo, hi, fmt="{:.3f}", ngrid=4):
    def ymap(v):
        return g["T"] + (hi - v) / (hi - lo) * g["ph"]
    for k in range(ngrid + 1):
        v = lo + (hi - lo) * k / ngrid
        y = ymap(v)
        cv.create_line(g["L"], y, g["w"] - g["R"], y, fill=GRID_C)
        txt = fmt(v) if callable(fmt) else fmt.format(v)
        cv.create_text(g["L"] - 4, y, text=txt, anchor="e",
                       font=("Consolas", 8), fill=AXIS_TXT)
    return ymap


def _line(cv, xs, arr, off, end, ymap, color, width=1):
    pts, segs = [], []
    for i in range(off, end):
        v = float(arr[i])
        if math.isnan(v):
            if len(pts) >= 2:
                segs.append(list(pts))
            pts = []
            continue
        pts.extend((xs(i), ymap(v)))
    if len(pts) >= 2:
        segs.append(list(pts))
    for s in segs:
        cv.create_line(*s, fill=color, width=width,
                       joinstyle="round", capstyle="round")


def _sig_marker(cv, x, y, lab):
    """K线上的信号小图标（TDX DRAWICON 风格），y 为图标顶部。"""
    col = SIG_COLORS[lab]
    if lab == "最佳点":                       # 金色五角星
        cv.create_text(x, y, text="★", fill=col, anchor="n",
                       font=("DejaVu Sans", 9, "bold"))
    elif lab == "买点1":                      # 红色上三角
        cv.create_polygon(x, y, x - 4.5, y + 8, x + 4.5, y + 8,
                          fill=col, outline="")
    elif lab == "买点2":                      # 橙色菱形
        cv.create_polygon(x, y, x - 4.5, y + 5, x, y + 10,
                          x + 4.5, y + 5, fill=col, outline="")
    else:                                     # 抄底：品红圆点
        cv.create_oval(x - 3.5, y + 1, x + 3.5, y + 8,
                       fill=col, outline="")


# ==================== 个股详情页 ====================

class DetailTab(ttk.Frame):
    TICK = TICK_MS

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.rows = []                 # 已收盘日K（升序）
        self.live = None               # 盘中合成bar
        self.all_rows = []
        self.strat = None
        self.events = []
        self.code = ""
        self.stock_info = {}
        self.quote = None
        self.view_n = 120
        self.view_end = 0
        self.scales = {}
        self._drag_x = None
        self._drag_end = None
        self._drag_job = None
        self._mtn = None
        self._tick_busy = False
        self._resize_job = None
        self._load_seq = 0
        self.ma_on = {n: tk.BooleanVar(value=n in (5, 10, 20, 80))
                      for n in MA_COLORS}
        self._build_ui()
        self._load_cfg()

    # ---------- 界面 ----------
    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=0, minsize=340)
        self.rowconfigure(1, weight=1)

        bar = ttk.Frame(self, padding=(6, 4))
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(bar, text="代码:").pack(side="left")
        self.code_var = tk.StringVar(value="sz000725")
        ent = ttk.Entry(bar, textvariable=self.code_var, width=11)
        ent.pack(side="left", padx=3)
        ent.bind("<Return>", lambda e: self.load_code(self.code_var.get()))
        ttk.Button(bar, text="加载", command=lambda: self.load_code(
            self.code_var.get())).pack(side="left", padx=2)
        ttk.Button(bar, text="更新数据", command=self.reload_force).pack(
            side="left", padx=2)
        ttk.Button(bar, text="策略说明", command=lambda: messagebox.showinfo(
            "策略说明", FORMULA_TEXT)).pack(side="left", padx=2)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y",
                                                   padx=8)
        ttk.Label(bar, text="均线:").pack(side="left")
        for n in sorted(MA_COLORS):
            ttk.Checkbutton(bar, text=f"MA{n}", variable=self.ma_on[n],
                            command=self._rerender).pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y",
                                                   padx=8)
        ttk.Label(bar, text="根数:").pack(side="left")
        self.n_var = tk.StringVar(value=str(self.view_n))
        cb = ttk.Combobox(bar, textvariable=self.n_var, width=5,
                          values=("60", "120", "250", "500"),
                          state="readonly")
        cb.pack(side="left", padx=3)
        cb.bind("<<ComboboxSelected>>", self._n_changed)

        ttk.Button(bar, text="全市场扫描",
                   command=self.app.show_scan).pack(side="right", padx=2)
        ttk.Button(bar, text="导出信号CSV", command=self.export_csv).pack(
            side="right", padx=2)

        charts = tk.Frame(self, bg=BG)
        charts.grid(row=1, column=0, sticky="nsew")
        charts.columnconfigure(0, weight=1)
        charts.rowconfigure(0, weight=5)
        charts.rowconfigure(1, weight=2)
        charts.rowconfigure(2, weight=2)
        self.cv_main = self._mk_canvas(charts, 0, "main", 400)
        self.cv_var2 = self._mk_canvas(charts, 1, "var2", 150)
        self.cv_macd = self._mk_canvas(charts, 2, "macd", 150)

        right = tk.Frame(self, bg=PANEL_BG, width=340)
        right.grid(row=1, column=1, sticky="nsew")
        right.pack_propagate(False)
        self.info = tk.Text(right, height=15, wrap="word", bg=FIELD_BG,
                            fg=FG_MAIN, relief="flat", padx=6, pady=4,
                            font=("Microsoft YaHei", 9),
                            insertbackground=FG_MAIN)
        self.info.pack(fill="x", padx=4, pady=(4, 2))
        for tag, kw in (("h", {"font": ("Microsoft YaHei", 11, "bold"),
                               "foreground": "#ffffff"}),
                        ("h2", {"font": ("Microsoft YaHei", 9, "bold"),
                                "foreground": TITLE_TXT}),
                        ("dim", {"foreground": AXIS_TXT}),
                        ("up", {"foreground": UP}),
                        ("down", {"foreground": DOWN}),
                        ("warn", {"foreground": "#ffd43b"})):
            self.info.tag_configure(tag, **kw)
        for lab in SIG_ORDER:
            self.info.tag_configure("sig_" + lab,
                                    foreground=SIG_COLORS[lab])
        self.info.configure(state="disabled")

        ttk.Label(right, text="信号明细（双击定位）").pack(
            anchor="w", padx=6, pady=(4, 0))
        body = tk.Frame(right, bg=PANEL_BG)
        body.pack(fill="both", expand=True, padx=4, pady=4)
        cols = ("date", "sig", "close", "r5", "r10", "r20")
        heads = ("日期", "信号", "收盘", "后5日", "后10日", "后20日")
        widths = (72, 52, 52, 52, 52, 52)
        self.tree = ttk.Treeview(body, columns=cols, show="headings",
                                 selectmode="browse")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center", stretch=True)
        vsb = ttk.Scrollbar(body, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for lab in SIG_ORDER:
            self.tree.tag_configure("s_" + lab,
                                    foreground=SIG_COLORS[lab])
        self.tree.bind("<Double-Button-1>", self._tree_jump)

        self.hover = ttk.Label(self, text="", anchor="w", padding=(8, 2))
        self.hover.grid(row=2, column=0, columnspan=2, sticky="ew")

    def _mk_canvas(self, parent, row, key, height):
        cv = tk.Canvas(parent, bg=BG, highlightthickness=0, height=height)
        cv.grid(row=row, column=0, sticky="nsew")
        cv.bind("<Motion>", lambda e, k=key: self._on_motion(e, k))
        cv.bind("<Leave>", lambda e, k=key: self._on_leave(e, k))
        cv.bind("<Configure>", self._on_resize)
        cv.bind("<MouseWheel>", self._on_wheel)
        cv.bind("<Button-4>", self._on_wheel)
        cv.bind("<Button-5>", self._on_wheel)
        cv.bind("<ButtonPress-1>", self._drag_start)
        cv.bind("<B1-Motion>", self._drag_move)
        cv.bind("<ButtonRelease-1>", self._drag_end)
        cv.bind("<Double-Button-1>", self._drag_reset)
        return cv

    # ---------- 配置 ----------
    def _load_cfg(self):
        try:
            cp = configparser.ConfigParser()
            cp.read(CFG_PATH, encoding="utf-8")
            self._start_code = cp.get("ui", "last", fallback="sz000725")
            self.view_n = max(30, min(cp.getint("ui", "view_n",
                                                fallback=120), 600))
        except Exception:
            self._start_code, self.view_n = "sz000725", 120
        self.code_var.set(self._start_code)
        self.n_var.set(str(self.view_n))

    def _save_cfg(self):
        try:
            cp = configparser.ConfigParser()
            cp.read(CFG_PATH, encoding="utf-8")
            if not cp.has_section("ui"):
                cp.add_section("ui")
            if self.code:
                cp.set("ui", "last", self.code)
            cp.set("ui", "view_n", str(self.view_n))
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                cp.write(f)
        except Exception:
            pass

    # ---------- 数据加载 ----------
    def load_code(self, text):
        try:
            full = sg.normalize_code(text)
        except ValueError as e:
            messagebox.showwarning("代码有误", str(e))
            return
        self.code = full
        self.code_var.set(full)
        self.live = None
        self.quote = None
        self._load_seq += 1
        seq = self._load_seq
        self.app.set_status(f"正在加载 {full} ...")
        code = full

        def work():
            rows = sg.get_daily(code, min_bars=100)
            try:
                info = sg.get_stock_info(code)
            except Exception:
                info = {}
            return (seq, rows, info or {})

        self.app.run_bg(work, self._load_done)

    def _load_done(self, res, err):
        if err:
            self.app.set_status(f"加载失败: {err}")
            messagebox.showerror("加载失败", str(err))
            return
        seq, rows, info = res
        if seq != self._load_seq:
            return                          # 已被更新的加载请求取代
        if not rows:
            self.app.set_status("无K线数据")
            return
        self.rows = rows
        self.stock_info = info
        self.view_end = 0
        self._recompute()
        self._rerender()
        self._save_cfg()
        self.app.set_status(
            f"{info.get('name', '')} {self.code}  共{len(rows)}根日K  "
            f"最新 {rows[-1]['date']}  收盘 {rows[-1]['close']:.2f}")
        self._tick()

    def reload_force(self):
        if not self.code:
            return
        self.app.set_status("正在联网更新数据（本地够新则直接命中缓存）...")
        code = self.code
        self._load_seq += 1
        seq = self._load_seq

        def work():
            return seq, sg.get_daily(code, min_bars=100)

        self.app.run_bg(work, self._reload_done)

    def _reload_done(self, res, err):
        if err:
            self.app.set_status(f"更新失败: {err}")
            return
        seq, rows = res
        if seq != self._load_seq:
            return
        if not rows:
            return
        self.rows = rows
        self._recompute()
        self._rerender()
        self.app.set_status(f"数据已更新至 {rows[-1]['date']}")
        self._tick()

    def _tick(self):
        self.app.safe_after(self.TICK, self._tick)
        if not self.code or self._tick_busy:
            return
        self._tick_busy = True
        code = self.code
        self.app.run_bg(lambda: (code, sg.fetch_quote(code)),
                        self._quote_done)

    def _quote_done(self, res, err):
        self._tick_busy = False
        if err or not res:
            return
        code, q = res
        if code != self.code or not q:
            return
        self.quote = q
        if q.get("name") and not self.stock_info.get("name"):
            self.stock_info["name"] = q["name"]
        live = None
        if q.get("time", "")[:8] == time.strftime("%Y%m%d") \
                and q.get("price", 0) > 0:
            px = q["price"]
            live = {"date": time.strftime("%Y-%m-%d"),
                    "open": q.get("open") or px,
                    "high": q.get("high") or px,
                    "low": q.get("low") or px,
                    "close": px, "vol": 0.0}
        changed = ((live is None) != (self.live is None)
                   or (live and self.live
                       and live["close"] != self.live["close"]))
        self.live = live
        if changed and self.rows:
            self._recompute()
            self._rerender()
        else:
            self._fill_info()

    # ---------- 重算/回填 ----------
    def _recompute(self):
        old_n = len(self.all_rows)
        at_latest = (old_n == 0) or (self.view_end >= old_n)
        dist = max(0, old_n - self.view_end)
        rows = list(self.rows)
        if self.live and (not rows or self.live["date"] > rows[-1]["date"]):
            rows.append(self.live)
        self.all_rows = rows
        self.strat = compute_strategy(rows)
        self.events = signal_events(self.strat)
        n = len(rows)
        self.view_end = n if at_latest else max(0, n - dist)
        self._fill_info()
        self._fill_tree()

    def _fwd(self, i, days):
        if self.strat is None:
            return None
        return forward_pct(self.strat["close"], i, days)

    def _fill_info(self):
        t = self.info
        t.configure(state="normal")
        t.delete("1.0", "end")
        if not self.all_rows or self.strat is None:
            t.insert("end", "输入股票代码后点【加载】\n", "dim")
            t.configure(state="disabled")
            return
        rows, st = self.all_rows, self.strat
        name = (self.stock_info.get("name")
                or (self.quote or {}).get("name") or self.code)
        t.insert("end", f"{name}  {self.code}\n", "h")
        meta = " · ".join(x for x in (self.stock_info.get("industry"),
                                      self.stock_info.get("tier")) if x)
        if meta:
            t.insert("end", meta + "\n", "dim")
        i = len(rows) - 1
        c = float(st["close"][i])
        pc = float(st["close"][i - 1]) if i > 0 else c
        chg = (c / pc - 1) * 100 if pc else 0.0
        tag = "up" if chg >= 0 else "down"
        live_txt = "  [含盘中实时bar]" if self.live else ""
        t.insert("end", f"{rows[i]['date']}  收 {c:.2f}  ")
        t.insert("end", f"{chg:+.2f}%{live_txt}\n", tag)
        if self.quote:
            q = self.quote
            qt = q.get("time", "")
            t.insert("end", f"现价 {q['price']:.2f}  快照 "
                            f"{qt[8:12] if len(qt) >= 12 else qt}\n", "dim")

        t.insert("end", "\n策略状态\n", "h2")
        t.insert("end", f"VAR2 {fnum(st['var2'][i])}   "
                        f"MMA {fnum(st['mma'][i])}   "
                        f"MMB {fnum(st['mmb'][i])}\n")
        is_bottom = bool(st["bottom"][i])
        t.insert("end", "快到底: " + ("-30  ● 底部区" if is_bottom
                                      else "0  正常") + "\n",
                 "warn" if is_bottom else "dim")
        t.insert("end", f"DIFF {fnum(st['diff'][i])}   "
                        f"DEA {fnum(st['dea'][i])}   "
                        f"MACD {fnum(st['macd'][i])}\n")

        t.insert("end", "\n最近40日信号\n", "h2")
        recent = [(j, lab) for j, lab in self.events if j >= len(rows) - 40]
        if recent:
            for j, lab in reversed(recent[-15:]):
                t.insert("end", f"{rows[j]['date'][5:]}  {lab}  "
                                f"@{float(st['close'][j]):.2f}\n",
                         "sig_" + lab)
        else:
            t.insert("end", "（无）\n", "dim")

        t.insert("end", "\n历史信号统计（全序列）\n", "h2")
        for lab in SIG_ORDER:
            idxs = [j for j, l in self.events if l == lab]
            if not idxs:
                continue
            parts = []
            for d in (5, 10, 20):
                vals = [self._fwd(j, d) for j in idxs]
                vals = [v for v in vals if v is not None]
                if vals:
                    avg = sum(vals) / len(vals)
                    win = sum(1 for v in vals if v > 0) / len(vals) * 100
                    parts.append(f"{d}日 {avg:+.1f}%/{win:.0f}%")
            t.insert("end", f"{lab}  {len(idxs)}次  " + "  ".join(parts)
                            + "\n", "sig_" + lab)
        t.insert("end", "\n" + DISCLAIMER + "\n", "dim")
        t.configure(state="disabled")

    def _fill_tree(self):
        tv = self.tree
        tv.delete(*tv.get_children())
        if self.strat is None:
            return
        st = self.strat
        for j, lab in reversed(self.events):
            vals = (self.all_rows[j]["date"][2:],
                    lab,
                    f"{float(st['close'][j]):.2f}",
                    fnum(self._fwd(j, 5), "{:+.1f}", "-"),
                    fnum(self._fwd(j, 10), "{:+.1f}", "-"),
                    fnum(self._fwd(j, 20), "{:+.1f}", "-"))
            tv.insert("", "end", iid=str(j), values=vals,
                      tags=("s_" + lab,))

    def _tree_jump(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            j = int(sel[0])
        except ValueError:
            return
        _, _, vn = self._view_slice()
        self.view_end = min(len(self.all_rows), j + vn // 2 + 1)
        self._rerender()

    def export_csv(self):
        if not self.events:
            messagebox.showinfo("导出", "当前股票没有信号")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=f"抄底信号_{self.code}_{time.strftime('%Y%m%d')}.csv",
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["代码", "名称", "信号日", "信号", "收盘",
                            "后1日%", "后5日%", "后10日%", "后20日%"])
                name = self.stock_info.get("name", "")
                st = self.strat
                for j, lab in self.events:
                    w.writerow([
                        self.code, name, self.all_rows[j]["date"], lab,
                        f"{float(st['close'][j]):.2f}",
                        fnum(self._fwd(j, 1), "{:.2f}", ""),
                        fnum(self._fwd(j, 5), "{:.2f}", ""),
                        fnum(self._fwd(j, 10), "{:.2f}", ""),
                        fnum(self._fwd(j, 20), "{:.2f}", "")])
        except OSError as e:
            messagebox.showerror("导出失败", str(e))
            return
        self.app.set_status(f"已导出 {path}")
        messagebox.showinfo("导出完成", path)

    # ---------- 视图 ----------
    def _view_slice(self):
        n = len(self.all_rows)
        if n == 0:
            return 0, 0, 1
        vn = max(30, min(self.view_n, n))
        end = max(vn, min(self.view_end, n))
        return end - vn, end, vn

    def _n_changed(self, _event=None):
        try:
            self.view_n = max(30, min(int(self.n_var.get()), 600))
        except ValueError:
            return
        self._rerender()

    def _rerender(self):
        if self.strat is None:
            return
        self._draw_main()
        self._draw_var2()
        self._draw_macd()

    def _on_resize(self, _event):
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(200, self._rerender)

    def _on_wheel(self, event):
        if not self.all_rows:
            return
        if event.state & 0x1:                       # Shift+滚轮 平移
            num = getattr(event, "num", None)
            step = 5 if (event.delta > 0 or num == 4) else -5
            vn = self._view_slice()[2]
            self.view_end = max(vn, min(self.view_end + step,
                                        len(self.all_rows)))
        else:
            num = getattr(event, "num", None)
            if num == 4:
                step = -10
            elif num == 5:
                step = 10
            else:
                step = -10 if event.delta > 0 else 10
            self.view_n = max(30, min(self.view_n + step, 600))
            self.n_var.set(str(self.view_n))
        self._rerender()

    def _drag_start(self, event):
        self._drag_x = event.x
        self._drag_end = self.view_end

    def _drag_move(self, event):
        if self._drag_x is None:
            return
        g = self.scales.get("main")
        if not g or g.get("bw", 0) <= 0:
            return
        bars = int((event.x - self._drag_x) / g["bw"])
        vn = self._view_slice()[2]
        new_end = max(vn, min(self._drag_end - bars,
                              len(self.all_rows)))
        if new_end != self.view_end:
            self.view_end = new_end
            if self._drag_job is None:
                self._drag_job = self.after(60, self._drag_redraw)

    def _drag_redraw(self):
        self._drag_job = None
        self._rerender()

    def _drag_end(self, _event):
        self._drag_x = None
        if self._drag_job:
            self.after_cancel(self._drag_job)
            self._drag_job = None
            self._rerender()

    def _drag_reset(self, _event):
        self.view_end = len(self.all_rows)
        self._rerender()

    # ---------- 主图 ----------
    def _draw_main(self):
        cv = self.cv_main
        cv.delete("all")
        rows, st = self.all_rows, self.strat
        if not rows or st is None:
            return
        off, end, vn = self._view_slice()
        g = _geom(cv, vn)

        def xs(i):
            return g["L"] + g["bw"] * (i - off + 0.5)

        lows = [r["low"] for r in rows[off:end]]
        highs = [r["high"] for r in rows[off:end]]
        for n, var in self.ma_on.items():
            if var.get():
                seg = st["ma"][n][off:end]
                seg = seg[~np.isnan(seg)]
                if seg.size:
                    lows.append(float(seg.min()))
                    highs.append(float(seg.max()))
        lo, hi = _pad_range(min(lows), max(highs))
        ymap = _axes(cv, g, lo, hi, fmt="{:.2f}")

        for n in sorted(MA_COLORS):
            if self.ma_on[n].get():
                _line(cv, xs, st["ma"][n], off, end, ymap, MA_COLORS[n])

        for i in range(off, end):
            b = rows[i]
            up = b["close"] >= b["open"]
            color = UP if up else DOWN
            yo, yc = ymap(b["open"]), ymap(b["close"])
            cv.create_line(xs(i), ymap(b["high"]), xs(i), ymap(b["low"]),
                           fill=color)
            bw = max(g["bw"] * 0.62, 2)
            ty, by = min(yo, yc), max(yo, yc)
            if by - ty < 1:
                by = ty + 1
            cv.create_rectangle(xs(i) - bw / 2, ty, xs(i) + bw / 2, by,
                                fill=color, outline=color)

        sig_at = {}
        for j, lab in self.events:
            if off <= j < end:
                sig_at.setdefault(j, []).append(lab)
        order = {lab: k for k, lab in enumerate(SIG_ORDER)}
        floors = {}                   # 相邻K线的标记下沿，避免信号图标重叠
        for j in sorted(sig_at):
            labs = sorted(sig_at[j], key=lambda x: order.get(x, 99))
            for lab in labs:
                x = xs(j)
                y = ymap(rows[j]["low"]) + 6
                for k in range(j - 1, j + 2):
                    if k in floors:
                        y = max(y, floors[k] + 2)
                _sig_marker(cv, x, y, lab)
                for k in range(j - 1, j + 2):
                    floors[k] = max(floors.get(k, 0.0), y + 11)

        li = end - 1
        ylast = ymap(rows[li]["close"])
        cv.create_line(g["L"], ylast, g["w"] - g["R"], ylast,
                       fill=GUIDE_C, dash=(2, 3))
        col = UP if rows[li]["close"] >= rows[li]["open"] else DOWN
        cv.create_text(g["w"] - g["R"] + 3, ylast,
                       text=f"{rows[li]['close']:.2f}", anchor="w",
                       fill=col, font=("Consolas", 8, "bold"))

        lx = g["L"] + 2
        for n in sorted(MA_COLORS):
            if self.ma_on[n].get():
                tid = cv.create_text(lx, g["T"] + 6, text=f"MA{n}",
                                     fill=MA_COLORS[n], anchor="w",
                                     font=("Consolas", 8, "bold"))
                bb = cv.bbox(tid)
                lx = (bb[2] if bb else lx + 40) + 10
        lx += 10
        for lab in ("最佳点", "买点1", "买点2", "抄底"):
            _sig_marker(cv, lx, g["T"] + 5, lab)
            tid = cv.create_text(lx + 7, g["T"] + 6, text=lab,
                                 anchor="w", fill=SIG_COLORS[lab],
                                 font=("Microsoft YaHei", 8, "bold"))
            bb = cv.bbox(tid)
            lx = (bb[2] if bb else lx + 50) + 12

        step = max(1, vn // 8)
        for i in range(off, end, step):
            cv.create_text(xs(i), g["h"] - 7, text=rows[i]["date"][5:],
                           font=("Consolas", 7), fill=AXIS_TXT)
        self._finish_panel(cv, "main", g, lo, hi, rows[off:end],
                           fmt="{:.2f}")

    # ---------- VAR2 副图 ----------
    def _draw_var2(self):
        cv = self.cv_var2
        cv.delete("all")
        rows, st = self.all_rows, self.strat
        if not rows or st is None:
            return
        off, end, vn = self._view_slice()
        g = _geom(cv, vn)

        def xs(i):
            return g["L"] + g["bw"] * (i - off + 0.5)

        series = (("var2", st["var2"], VAR2_C),
                  ("mma", st["mma"], MMA_C),
                  ("mmb", st["mmb"], MMB_C))
        lows, highs = [0.0], [0.0]
        for _, arr, _ in series:
            seg = arr[off:end]
            seg = seg[~np.isnan(seg)]
            if seg.size:
                lows.append(float(seg.min()))
                highs.append(float(seg.max()))
        lo, hi = _pad_range(min(lows), max(highs))
        ymap = _axes(cv, g, lo, hi, fmt="{:.3f}")

        y0 = ymap(0.0)
        cv.create_line(g["L"], y0, g["w"] - g["R"], y0,
                       fill=GUIDE_C, dash=(4, 3))
        for _, arr, col in series:
            _line(cv, xs, arr, off, end, ymap, col)

        base = g["T"] + g["ph"]
        strip = 14
        cv.create_rectangle(g["L"], base - strip, g["w"] - g["R"], base,
                            fill="#0e1318", outline="")
        cv.create_text(g["L"] + 3, base - strip + 7, text="快到底",
                       anchor="w", fill=KDD_C,
                       font=("Microsoft YaHei", 7))
        pts = []
        for i in range(off, end):
            if bool(st["bottom"][i]):
                pts.extend((xs(i), base - 3))
            else:
                if len(pts) >= 4:
                    cv.create_line(*pts, fill=KDD_C, width=3)
                pts = []
        if len(pts) >= 4:
            cv.create_line(*pts, fill=KDD_C, width=3)
        for i in range(off, end):
            if bool(st["di_cx"][i]):
                cv.create_text(xs(i), base - 8, text="●",
                               fill=SIG_COLORS["底初选"],
                               font=("Arial", 9, "bold"))

        lx = g["L"] + 2
        for txt, col in (("VAR2", VAR2_C), ("MMA", MMA_C), ("MMB", MMB_C)):
            tid = cv.create_text(lx, g["T"] + 6, text=txt, anchor="w",
                                 fill=col, font=("Consolas", 8, "bold"))
            bb = cv.bbox(tid)
            lx = (bb[2] if bb else lx + 40) + 10
        cv.create_text(lx + 4, g["T"] + 6, text="底初选●", anchor="w",
                       fill=SIG_COLORS["底初选"],
                       font=("Microsoft YaHei", 8, "bold"))

        step = max(1, vn // 8)
        for i in range(off, end, step):
            cv.create_text(xs(i), g["h"] - 7, text=rows[i]["date"][5:],
                           font=("Consolas", 7), fill=AXIS_TXT)
        self._finish_panel(cv, "var2", g, lo, hi, rows[off:end],
                           fmt="{:.3f}")

    # ---------- MACD 副图 ----------
    def _draw_macd(self):
        cv = self.cv_macd
        cv.delete("all")
        rows, st = self.all_rows, self.strat
        if not rows or st is None:
            return
        off, end, vn = self._view_slice()
        g = _geom(cv, vn)

        def xs(i):
            return g["L"] + g["bw"] * (i - off + 0.5)

        lows, highs = [0.0], [0.0]
        for arr in (st["diff"], st["dea"], st["macd"]):
            seg = arr[off:end]
            seg = seg[~np.isnan(seg)]
            if seg.size:
                lows.append(float(seg.min()))
                highs.append(float(seg.max()))
        lo, hi = _pad_range(min(lows), max(highs))
        ymap = _axes(cv, g, lo, hi, fmt="{:.2f}")
        y0 = ymap(0.0)
        cv.create_line(g["L"], y0, g["w"] - g["R"], y0,
                       fill=GUIDE_C, dash=(4, 3))

        hist = st["macd"]
        for i in range(off, end):
            v = float(hist[i])
            if math.isnan(v):
                continue
            p = float(hist[i - 1]) if i > 0 else v
            if math.isnan(p):
                p = v
            if v > p and v >= 0:
                col = MACD_COLORS["rise_above"]
            elif v > p:
                col = MACD_COLORS["rise_below"]
            elif v > 0:
                col = MACD_COLORS["fall_above"]
            else:
                col = MACD_COLORS["fall_below"]
            yv = ymap(v)
            bw = max(g["bw"] * 0.6, 1)
            cv.create_rectangle(xs(i) - bw / 2, min(yv, y0),
                                xs(i) + bw / 2, max(yv, y0),
                                fill=col, outline=col)

        _line(cv, xs, st["diff"], off, end, ymap, DIF_C, width=1)
        _line(cv, xs, st["dea"], off, end, ymap, DEA_C, width=1)
        for i in range(off, end):
            if bool(st["chaodi"][i]):
                _sig_marker(cv, xs(i), g["T"] + 4, "抄底")

        lx = g["L"] + 2
        for txt, col in (("DIFF", DIF_C), ("DEA", DEA_C)):
            tid = cv.create_text(lx, g["T"] + 6, text=txt, anchor="w",
                                 fill=col, font=("Consolas", 8, "bold"))
            bb = cv.bbox(tid)
            lx = (bb[2] if bb else lx + 40) + 10
        cv.create_text(lx + 6, g["T"] + 6, text="MACD四色柱", anchor="w",
                       fill=TITLE_TXT, font=("Microsoft YaHei", 8))

        step = max(1, vn // 8)
        for i in range(off, end, step):
            cv.create_text(xs(i), g["h"] - 7, text=rows[i]["date"][5:],
                           font=("Consolas", 7), fill=AXIS_TXT)
        self._finish_panel(cv, "macd", g, lo, hi, rows[off:end],
                           fmt="{:.2f}")

    # ---------- 十字光标 ----------
    def _finish_panel(self, cv, key, g, lo, hi, dates, fmt=None):
        g["key"], g["lo_v"], g["hi_v"] = key, lo, hi
        if fmt is None:
            g["fmt"] = lambda v: f"{v:.3f}"
        elif callable(fmt):
            g["fmt"] = fmt
        else:
            g["fmt"] = lambda v, f=fmt: f.format(v)
        self.scales[key] = g
        g["vid"] = cv.create_line(0, 0, 0, 0, state="hidden",
                                  fill=CROSS_C, dash=(4, 3))
        g["hid"] = cv.create_line(0, 0, 0, 0, state="hidden",
                                  fill=CROSS_C, dash=(4, 3))
        g["pid"] = cv.create_text(0, 0, text="", state="hidden",
                                  fill="#ffffff",
                                  font=("Consolas", 9, "bold"))
        g["pbg"] = cv.create_rectangle(0, 0, 0, 0, state="hidden",
                                       fill="#1971c2", outline="")
        g["did"] = cv.create_text(0, 0, text="", state="hidden",
                                  fill="#ffffff",
                                  font=("Consolas", 9, "bold"))
        g["dbgd"] = cv.create_rectangle(0, 0, 0, 0, state="hidden",
                                        fill="#333c46", outline="")
        cv.tag_lower(g["dbgd"], g["did"])
        cv.tag_raise(g["pid"])
        g["_shown"] = False

    def _on_motion(self, event, key):
        if self._drag_x is not None:
            return
        pg = self.scales.get(key)
        if not pg or not self.all_rows:
            return
        cv = {"main": self.cv_main, "var2": self.cv_var2,
              "macd": self.cv_macd}[key]
        xv = max(min(event.x, pg["w"] - pg["R"]), pg["L"])
        y = max(min(event.y, pg["T"] + pg["ph"]), pg["T"])
        cv.coords(pg["vid"], xv, pg["T"] + 2, xv, pg["h"] - pg["B"])
        cv.coords(pg["hid"], pg["L"], y, pg["w"] - pg["R"], y)
        val = pg["hi_v"] - (y - pg["T"]) / pg["ph"] * (
            pg["hi_v"] - pg["lo_v"])
        txt = pg["fmt"](val)
        px = pg["w"] - pg["R"] + 30
        cv.coords(pg["pid"], px, y)
        cv.itemconfigure(pg["pid"], text=txt)
        cv.coords(pg["pbg"], px - 27, y - 9, px + 29, y + 9)
        if not pg["_shown"]:
            for it in ("vid", "hid", "pid", "pbg", "did", "dbgd"):
                cv.itemconfigure(pg[it], state="normal")
            cv.tag_raise(pg["pbg"])
            cv.tag_raise(pg["pid"])
            pg["_shown"] = True

        idx = int((event.x - pg["L"]) / pg["bw"])
        idx = max(0, min(pg["n"] - 1, idx))
        off, end, _ = self._view_slice()
        abs_i = off + idx
        if not (off <= abs_i < end):
            return
        sig = (key, abs_i)
        if self._mtn == sig:
            return
        self._mtn = sig
        cx = pg["L"] + pg["bw"] * (idx + 0.5)
        dl = self.all_rows[abs_i]["date"]
        cv.coords(pg["did"], cx, pg["h"] - pg["B"] // 2 + 2)
        cv.itemconfigure(pg["did"], text=dl)
        w_bg = len(dl) * 7 + 10
        cv.coords(pg["dbgd"], cx - w_bg / 2, pg["h"] - pg["B"] // 2 - 5,
                  cx + w_bg / 2, pg["h"] - pg["B"] // 2 + 11)
        self._hover_text(abs_i)

    def _hover_text(self, i):
        rows, st = self.all_rows, self.strat
        if not (0 <= i < len(rows)) or st is None:
            return
        b = rows[i]
        pc = float(st["close"][i - 1]) if i > 0 else float(st["close"][i])
        chg = (float(st["close"][i]) / pc - 1) * 100 if pc else 0.0
        labs = "+".join(lab for j, lab in self.events if j == i)
        parts = [f"{b['date']}  开{b['open']:.2f} 高{b['high']:.2f} "
                 f"低{b['low']:.2f} 收{b['close']:.2f} ({chg:+.2f}%)",
                 f"VAR2 {fnum(st['var2'][i])}",
                 f"MMA {fnum(st['mma'][i])}",
                 f"MMB {fnum(st['mmb'][i])}",
                 ("快到底 -30" if bool(st["bottom"][i]) else "快到底 0"),
                 f"MACD {fnum(st['macd'][i])}"]
        if labs:
            parts.append("◆" + labs)
        self.hover.config(text="   ".join(parts))

    def _on_leave(self, _event, _key=None):
        self.hover.config(text="")
        self._mtn = None
        for key, cv in (("main", self.cv_main), ("var2", self.cv_var2),
                        ("macd", self.cv_macd)):
            pg = self.scales.get(key)
            if not pg:
                continue
            for it in ("vid", "hid", "pid", "pbg", "did", "dbgd"):
                cv.itemconfigure(pg[it], state="hidden")
            pg["_shown"] = False


# ==================== 全市场扫描页 ====================

class ScanTab(ttk.Frame):
    WORKERS = 8
    MIN_BARS = 150
    TAIL = 320

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self._busy = False
        self._stop = threading.Event()
        self._results = []
        self._sort_col = None
        self._sort_desc = True
        self._build_ui()

    def _build_ui(self):
        ctl = ttk.Frame(self, padding=(8, 6))
        ctl.pack(fill="x")
        ttk.Label(ctl, text="信号:").pack(side="left")
        self.sig_vars = {}
        for lab in SIG_ORDER:
            v = tk.BooleanVar(value=lab != "底初选")
            self.sig_vars[lab] = v
            ttk.Checkbutton(ctl, text=lab, variable=v).pack(
                side="left", padx=2)
        ttk.Separator(ctl, orient="vertical").pack(side="left", fill="y",
                                                   padx=8)
        ttk.Label(ctl, text="回看(交易日):").pack(side="left")
        self.look_var = tk.StringVar(value="10")
        ttk.Spinbox(ctl, from_=1, to=120, textvariable=self.look_var,
                    width=5).pack(side="left", padx=3)
        self.st_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="排除ST/退市",
                        variable=self.st_var).pack(side="left", padx=2)
        self.etf_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="排除ETF/指数",
                        variable=self.etf_var).pack(side="left", padx=2)
        self.stale_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="排除长期停牌",
                        variable=self.stale_var).pack(side="left", padx=2)
        ttk.Separator(ctl, orient="vertical").pack(side="left", fill="y",
                                                   padx=8)
        self.btn_scan = ttk.Button(ctl, text="开始扫描",
                                   command=self.start_scan)
        self.btn_scan.pack(side="left", padx=2)
        self.btn_stop = ttk.Button(ctl, text="停止", state="disabled",
                                   command=self.stop_scan)
        self.btn_stop.pack(side="left", padx=2)

        prog = ttk.Frame(self, padding=(8, 0))
        prog.pack(fill="x")
        self.pb = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.pb.pack(side="left", fill="x", expand=True)
        self.prog_var = tk.StringVar(value="待扫描（基于本地缓存已收盘日K）")
        ttk.Label(prog, textvariable=self.prog_var, width=34).pack(
            side="left", padx=6)
        self.sum_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.sum_var).pack(side="left", padx=6)

        body = ttk.Frame(self, padding=(8, 4))
        body.pack(fill="both", expand=True)
        cols = ("code", "name", "date", "signals", "close", "pct",
                "since", "bottom")
        heads = ("代码", "名称", "信号日", "信号", "收盘", "当日%",
                 "信号至今%", "快到底")
        widths = (84, 130, 90, 150, 70, 70, 82, 70)
        self.tree = ttk.Treeview(body, columns=cols, show="headings",
                                 selectmode="browse")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(
                c, text=h, command=lambda cc=c: self._sort_by(cc))
            self.tree.column(c, width=w, anchor="center")
        vsb = ttk.Scrollbar(body, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("hit", foreground="#ffd43b")
        self.tree.bind("<Double-Button-1>", self._open)
        ttk.Label(self, text="双击结果行 → 打开个股详情 | "
                             + DISCLAIMER, padding=(8, 2)).pack(fill="x")

    # ---------- 扫描 ----------
    def start_scan(self):
        if self._busy:
            return
        labels = [lab for lab, v in self.sig_vars.items() if v.get()]
        if not labels:
            messagebox.showinfo("提示", "请至少选择一个信号")
            return
        try:
            look = max(1, min(int(self.look_var.get()), 120))
        except ValueError:
            look = 10
        opts = {"labels": labels, "lookback": look,
                "exclude_st": self.st_var.get(),
                "exclude_etf": self.etf_var.get(),
                "exclude_stale": self.stale_var.get()}
        self._busy = True
        self._stop = threading.Event()
        self.btn_scan.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.tree.delete(*self.tree.get_children())
        self._results = []
        self.sum_var.set("")
        self.pb["value"] = 0
        self.prog_var.set("准备扫描...")
        self.app.set_status("全市场扫描中（后台多线程，可随时停止）...")
        self.app.run_bg(lambda: self._scan(opts, self._stop),
                        self._scan_done)

    def stop_scan(self):
        self._stop.set()
        self.prog_var.set("正在停止...")

    @staticmethod
    def _is_etf_or_index(code):
        if code[2:4] in ("51", "56", "58", "15", "16", "18"):
            return True
        return code.startswith(("sh000", "sz399", "bj899"))

    def _scan(self, opts, stop):
        t0 = time.time()
        with sg.db_conn() as con:
            names = {r[0]: r[1] or "" for r in
                     con.execute("SELECT code,name FROM stocks")}
            codes = [r[0] for r in con.execute(
                "SELECT code FROM daily_bars GROUP BY code "
                "HAVING COUNT(*)>=?", (self.MIN_BARS,))]
            market_last = con.execute(
                "SELECT MAX(date) FROM daily_bars").fetchone()[0] or ""
        stale_limit = ""
        if opts["exclude_stale"] and market_last:
            stale_limit = (datetime.date.fromisoformat(market_last)
                           - datetime.timedelta(days=7)).isoformat()

        total = [len(codes)]
        tls = threading.local()

        def conn_of():
            con = getattr(tls, "con", None)
            if con is None:
                con = sqlite3.connect(sg.DB_PATH, timeout=30)
                con.execute("PRAGMA query_only=ON")
                tls.con = con
            return con

        def work(code):
            if stop.is_set():
                return None
            name = names.get(code, "")
            if opts["exclude_st"] and ("ST" in name or "退" in name):
                return None
            if opts["exclude_etf"] and self._is_etf_or_index(code):
                return None
            try:
                cur = conn_of().execute(
                    "SELECT date,open,high,low,close,vol FROM daily_bars "
                    "WHERE code=? ORDER BY date DESC LIMIT ?",
                    (code, self.TAIL))
                rows = [{"date": r[0], "open": r[1], "high": r[2],
                         "low": r[3], "close": r[4], "vol": r[5] or 0.0}
                        for r in cur.fetchall()]
            except sqlite3.Error:
                return None
            rows.reverse()
            if len(rows) < self.MIN_BARS:
                return None
            if stale_limit and rows[-1]["date"] < stale_limit:
                return None
            st = compute_strategy(rows)
            if st is None:
                return None
            n = len(rows)
            found = None
            for i in range(max(0, n - opts["lookback"]), n):
                labs = [lab for lab in opts["labels"]
                        if bool(st[SIG_KEY[lab]][i])]
                if labs:
                    found = (i, labs)
            if found is None:
                return None
            i, labs = found
            c = float(st["close"][i])
            c_prev = float(st["close"][i - 1]) if i > 0 else c
            c_last = float(st["close"][n - 1])
            return {"code": code, "name": name, "date": rows[i]["date"],
                    "signals": labs, "close": c,
                    "pct": (c / c_prev - 1) * 100 if c_prev else 0.0,
                    "since": (c_last / c - 1) * 100 if c else 0.0,
                    "bottom": bool(st["bottom"][n - 1])}

        results = []
        with ThreadPoolExecutor(max_workers=self.WORKERS) as ex:
            it = ex.map(work, codes, chunksize=8)
            for done, res in enumerate(it, 1):
                if stop.is_set():
                    break
                if res:
                    results.append(res)
                if done % 50 == 0 or done == total[0]:
                    pct = done * 100.0 / total[0]
                    self.app.safe_after(0, lambda d=done, p=pct: (
                        self.pb.configure(value=p),
                        self.prog_var.set(f"扫描中 {d}/{total[0]} ...")))
        return results, time.time() - t0, total[0]

    def _scan_done(self, res, err):
        self._busy = False
        self.btn_scan.config(state="normal")
        self.btn_stop.config(state="disabled")
        if err:
            self.prog_var.set("扫描失败")
            self.app.set_status(f"扫描失败: {err}")
            messagebox.showerror("扫描失败", str(err))
            return
        results, secs, total = res
        results.sort(key=lambda r: (r["date"], r["code"]), reverse=True)
        self._results = results
        self._sort_col = None
        self._fill_tree()
        counts = {}
        for r in results:
            for lab in r["signals"]:
                counts[lab] = counts.get(lab, 0) + 1
        cnt_txt = "  ".join(f"{lab}{counts.get(lab, 0)}"
                            for lab in SIG_ORDER if lab in counts)
        self.pb["value"] = 100
        self.prog_var.set(f"命中 {len(results)} 只 / 扫描 {total} 只，"
                          f"用时 {secs:.1f}s")
        self.sum_var.set(cnt_txt)
        self.app.set_status(f"扫描完成：命中 {len(results)} 只（{cnt_txt}）")

    def _fill_tree(self):
        tv = self.tree
        tv.delete(*tv.get_children())
        for r in self._results:
            tags = ("hit",) if len(r["signals"]) > 1 else ()
            tv.insert("", "end", iid=r["code"], tags=tags, values=(
                r["code"], r["name"], r["date"],
                "+".join(r["signals"]), f"{r['close']:.2f}",
                f"{r['pct']:+.2f}", f"{r['since']:+.2f}",
                "●" if r["bottom"] else ""))

    KEY_OF = {"code": "code", "name": "name", "date": "date",
              "signals": lambda r: "+".join(r["signals"]),
              "close": "close", "pct": "pct", "since": "since",
              "bottom": "bottom"}

    def _sort_by(self, col):
        if not self._results:
            return
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, True
        key = self.KEY_OF.get(col, col)
        if callable(key):
            self._results.sort(key=key, reverse=self._sort_desc)
        else:
            self._results.sort(key=lambda r: r.get(key), reverse=self._sort_desc)
        self._fill_tree()

    def _open(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.app.open_detail(row)


# ==================== 应用 ====================

class App:
    def __init__(self, root):
        self.root = root
        root.title("抄底策略选股工具 · 通达信VAR2体系")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1440, max(1100, sw - 60))}x"
                      f"{min(920, max(700, sh - 100))}+30+30")
        root.minsize(1000, 640)
        root.configure(bg=DARK_BG)
        self._style()
        self._ex = ThreadPoolExecutor(max_workers=4,
                                      thread_name_prefix="chaodi")
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.detail = DetailTab(self.nb, self)
        self.scan = ScanTab(self.nb, self)
        self.nb.add(self.detail, text="  个股详情  ")
        self.nb.add(self.scan, text="  全市场扫描  ")

        bottom = ttk.Frame(root, padding=(8, 2))
        bottom.pack(fill="x")
        self.status_var = tk.StringVar(value=f"就绪 · {DISCLAIMER}")
        ttk.Label(bottom, textvariable=self.status_var, anchor="w").pack(
            fill="x")

        root.protocol("WM_DELETE_WINDOW", self._close)
        if self.detail._start_code:
            self.detail.after(300, lambda: self.detail.load_code(
                self.detail._start_code))

    def _style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=DARK_BG, foreground=FG_MAIN,
                        fieldbackground=FIELD_BG, bordercolor="#2a3340",
                        lightcolor=PANEL_BG, darkcolor="#12171d",
                        troughcolor=DARK_BG)
        style.configure("TFrame", background=DARK_BG)
        style.configure("TLabelframe", background=DARK_BG,
                        bordercolor="#2a3340")
        style.configure("TLabelframe.Label", background=DARK_BG,
                        foreground=TITLE_TXT)
        style.configure("TLabel", background=DARK_BG, foreground=FG_MAIN)
        style.configure("TButton", background=BTN_BG, foreground=BTN_FG,
                        bordercolor=BTN_BORDER)
        style.map("TButton", background=[("active", BTN_HOVER)])
        style.configure("TEntry", fieldbackground=FIELD_BG,
                        foreground=FG_MAIN)
        style.configure("TCombobox", fieldbackground=FIELD_BG,
                        foreground=FG_MAIN, background=BTN_BG,
                        arrowcolor=FG_MAIN)
        style.map("TCombobox",
                  fieldbackground=[("readonly", FIELD_BG)],
                  foreground=[("readonly", FG_MAIN)])
        style.configure("TSpinbox", fieldbackground=FIELD_BG,
                        foreground=FG_MAIN, background=BTN_BG,
                        arrowcolor=FG_MAIN)
        style.configure("TScrollbar", background=BTN_BG,
                        troughcolor=DARK_BG)
        style.configure("TNotebook", background=DARK_BG,
                        bordercolor=BTN_BORDER, tabmargins=[4, 4, 4, 0])
        style.configure("TNotebook.Tab", background=BTN_BG,
                        foreground=FG_MAIN, bordercolor=BTN_BORDER,
                        padding=[12, 6])
        style.map("TNotebook.Tab",
                  background=[("selected", BTN_HOVER),
                              ("active", BTN_HOVER)],
                  foreground=[("selected", "#ffffff")])
        style.configure("Treeview", background=FIELD_BG,
                        fieldbackground=FIELD_BG, foreground=FG_MAIN,
                        bordercolor=BTN_BORDER)
        style.configure("Treeview.Heading", background=BTN_BG,
                        foreground=FG_MAIN, bordercolor=BTN_BORDER)
        style.map("Treeview", background=[("selected", "#2b3540")],
                  foreground=[("selected", "#ffffff")])
        style.configure("TProgressbar", background="#4dabf7",
                        troughcolor=FIELD_BG, bordercolor=BTN_BORDER)
        for sub in ("TRadiobutton", "TCheckbutton"):
            style.configure(sub, background=DARK_BG, foreground=FG_MAIN,
                            focuscolor=DARK_BG)
            style.map(sub,
                      background=[("active", DARK_BG),
                                  ("selected", DARK_BG),
                                  ("disabled", DARK_BG)],
                      foreground=[("active", FG_MAIN),
                                  ("selected", FG_MAIN),
                                  ("disabled", AXIS_TXT)])

    # ---------- 线程/调度 ----------
    def run_bg(self, fn, done):
        try:
            fut = self._ex.submit(fn)
        except RuntimeError:
            return

        def _cb(f):
            try:
                r = f.result()
                err = None
            except Exception as e:
                r, err = None, e
            self.safe_after(0, lambda: done(r, err))

        fut.add_done_callback(_cb)

    def safe_after(self, ms, fn):
        try:
            if not self.root.winfo_exists():
                return
            self.root.after(ms, fn)
        except (tk.TclError, RuntimeError):
            pass

    def set_status(self, msg):
        self.safe_after(0, lambda: self.status_var.set(msg))

    def show_scan(self):
        self.nb.select(self.scan)

    def open_detail(self, code):
        self.nb.select(self.detail)
        self.detail.load_code(code)

    def _close(self):
        try:
            self.detail._save_cfg()
        except Exception:
            pass
        self._ex.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
