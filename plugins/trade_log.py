# -*- coding: utf-8 -*-
"""交易记录插件（v6.6.5 重构）。

右侧栏交易记录管理面板，支持：
- 实盘 / 模拟 双账本独立记账（旧记录自动归入实盘）
- 日期只能为交易日（非交易日自动对齐到上一交易日，宿主交易日历）
- 真实性校验：整手（买入 100 股倍数）、T+1（当日买入不可当日卖）、
  卖出不可超持仓（按日期重放）、买入不可超可用现金
- 真实费用：佣金（含最低 5 元）+ 印花税（仅卖出）+ 过户费（仅沪市）
  + 可选滑点（计入费用）
- 持仓实时显示现价/市值/浮动盈亏/今仓（T+1）标记
- 联动开关：可选是否把账户资金提供给荐股/信号联动（联动固定使用实盘）
- 账户净值收益曲线（逐交易日：现金 + 持仓按当日收盘市值）
- AI 分析（基于当前账本持仓与记录）
"""

import json
import os
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
from datetime import datetime

from .base import StockPlugin

ACCOUNTS = (("live", "实盘"), ("sim", "模拟"))
DEFAULT_CAPITAL_SIM = 1000000.0     # 模拟盘默认本金 100 万


class TradeLogPlugin(StockPlugin):
    """交易记录插件。"""

    name = "交易记录"
    order = 10  # 排在预测参考之后

    def __init__(self, api):
        super().__init__(api)
        self._records = []          # 全部记录（含 account 字段）
        self._settings = {
            "commission": 0.00025,      # 佣金费率（万2.5）
            "min_commission": 5.0,      # 最低佣金（元）
            "stamp_tax": 0.001,         # 印花税（千1，仅卖出）
            "transfer_fee": 0.00001,    # 过户费（十万1，仅沪市）
            "slippage": 0.0,            # 滑点费率（双边，计入费用）
            "capital": 0.0,             # （旧字段，迁移到 capital_live）
            "capital_live": 0.0,        # 实盘本金（0=未填写）
            "capital_sim": DEFAULT_CAPITAL_SIM,
            "link_analysis": True,      # 是否参与荐股/信号联动
            "view_account": "live",     # 面板当前显示的账本
        }
        self._ai_msgs = []
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        self._last_price = {}       # code -> 最新价（on_tick/on_analysis 缓存）
        self._load_data()

    # ------------------------------------------------------------------
    # 存取
    # ------------------------------------------------------------------
    def _load_data(self):
        try:
            path = self.api.storage_path("trade_records.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    self._records = json.load(f)
            # v6.6.5 迁移：无 account 字段的旧记录归入实盘
            for r in self._records:
                if not r.get("account"):
                    r["account"] = "live"
        except Exception:
            self._records = []
        try:
            path = self.api.storage_path("trade_settings.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    self._settings.update(saved)
            # v6.6.5 迁移：旧 capital → capital_live
            if not self._settings.get("capital_live"):
                self._settings["capital_live"] = float(
                    self._settings.get("capital", 0.0) or 0.0)
            if self._settings.get("capital_sim") is None:
                self._settings["capital_sim"] = DEFAULT_CAPITAL_SIM
        except Exception:
            pass

    def _save_data(self):
        try:
            path = self.api.storage_path("trade_records.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._records, f, ensure_ascii=False, indent=2)
        except Exception:
            self.api.log.exception("保存交易记录失败")
        try:
            path = self.api.storage_path("trade_settings.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)
        except Exception:
            self.api.log.exception("保存设置失败")

    # ------------------------------------------------------------------
    # 账本基础
    # ------------------------------------------------------------------
    def _view_acc(self):
        return self._settings.get("view_account", "live")

    def _capital(self, acc):
        key = "capital_live" if acc == "live" else "capital_sim"
        return float(self._settings.get(key, 0.0) or 0.0)

    def _recs(self, acc=None):
        """某账本的记录（缺省=当前视图）；按日期稳定排序供重放。"""
        acc = acc or self._view_acc()
        recs = [r for r in self._records
                if r.get("account", "live") == acc]
        return sorted(recs, key=lambda r: (r.get("date", ""), r.get("code", "")))

    def _calc_fee(self, price, volume, is_sell=False, code=""):
        """真实费用：佣金(含最低) + 印花税(仅卖出) + 过户费(仅沪市) + 滑点。"""
        amount = price * volume
        commission = max(amount * self._settings["commission"],
                         self._settings["min_commission"])
        stamp = amount * self._settings["stamp_tax"] if is_sell else 0.0
        # 过户费仅沪市（sh 开头）收取；深市 2015 年起已免
        transfer = (amount * self._settings["transfer_fee"]
                    if str(code).startswith("sh") else 0.0)
        slip = amount * float(self._settings.get("slippage", 0.0) or 0.0)
        return round(commission + stamp + transfer + slip, 2)

    def _replay(self, recs, capital):
        """按日期顺序重放账本。返回 (最终现金, 持仓, 错误列表)。

        校验规则（真实 A 股口径）：
        - 卖出不可超持仓（含 T+1：当日买入部分不可当日卖出）
        - 买入金额+费用不可超可用现金
        - 买入须整手（100 股倍数）；卖出允许零股（清仓真实存在）
        同日多笔按 先卖后买 处理；资金不足/超卖即记错误（阻止入库）。"""
        cash = capital
        hold = {}       # code -> {"vol": 总持仓, "today": 今日买入}
        cur_date = None
        errs = []
        for rec in recs:
            code = rec.get("code", "")
            vol = int(rec.get("volume", 0) or 0)
            price = float(rec.get("price", 0) or 0)
            fee = float(rec.get("fee", 0) or 0)
            d = rec.get("date", "")
            if d != cur_date:
                cur_date = d
                for h in hold.values():
                    h["today"] = 0
            if rec.get("type") == "SELL":
                h = hold.get(code)
                avail = (h["vol"] - h["today"]) if h else 0
                if vol > avail:
                    errs.append(f"{d} {code} 卖出{vol}股超过可卖持仓{avail}"
                                f"（含T+1当日买入不可卖）")
                    continue
                h["vol"] -= vol
                cash += price * vol - fee
            else:
                if vol % 100:
                    errs.append(f"{d} {code} 买入{vol}股不是100股整数倍")
                    continue
                need = price * vol + fee
                if capital > 0 and need > cash + 1e-6:
                    errs.append(f"{d} {code} 买入需{need:,.0f}元 "
                                f"超过可用现金{cash:,.0f}元")
                    continue
                cash -= need
                h = hold.setdefault(code, {"vol": 0, "today": 0})
                h["vol"] += vol
                h["today"] += vol
        positions = {c: h["vol"] for c, h in hold.items() if h["vol"] > 0}
        return cash, positions, errs

    def _positions_detail(self, acc=None):
        """持仓明细：{code: {name, volume, avg_cost, today_vol}}。"""
        recs = self._recs(acc)
        capital = self._capital(acc or self._view_acc())
        hold = {}
        cash = capital
        cur_date = None
        for rec in recs:
            code = rec.get("code", "")
            vol = int(rec.get("volume", 0) or 0)
            price = float(rec.get("price", 0) or 0)
            fee = float(rec.get("fee", 0) or 0)
            d = rec.get("date", "")
            if d != cur_date:
                cur_date = d
                for h in hold.values():
                    h["today"] = 0
            if rec.get("type") == "SELL":
                h = hold.get(code)
                if h and h["vol"] >= vol:
                    h["vol"] -= vol
                    cash += price * vol - fee
            else:
                cash -= price * vol + fee
                h = hold.setdefault(code, {
                    "name": rec.get("name", ""), "vol": 0, "today": 0,
                    "cost": 0.0})
                h["name"] = rec.get("name", "") or h["name"]
                h["vol"] += vol
                h["today"] += vol
                h["cost"] += price * vol + fee
        out = {}
        for c, h in hold.items():
            if h["vol"] > 0:
                out[c] = {"name": h.get("name", ""),
                          "volume": h["vol"],
                          "avg_cost": h["cost"] / h["vol"],
                          "today_vol": h.get("today", 0)}
        return cash, out

    # ------------------------------------------------------------------
    # 账户上下文（宿主联动；link_analysis 关闭时 available=False 自动降级）
    # ------------------------------------------------------------------
    def account_context(self):
        """账户快照给宿主结合信号使用（联动固定使用**实盘**账本）。"""
        if not self._settings.get("link_analysis", True):
            return {"available": False,
                    "source": "交易记录插件（联动已关闭）"}
        acc = "live"
        capital = self._capital(acc)
        cash, positions = self._positions_detail(acc)
        mv = 0.0
        pos_out = {}
        for code, p in positions.items():
            px = self._last_price.get(code) or 0.0
            pos_out[code] = {
                "name": p["name"], "volume": int(p["volume"]),
                "avg_cost": round(p["avg_cost"], 4),
                "last_price": px or None,
                "market_value": round(px * p["volume"], 2) if px else None,
                "today_buy": int(p.get("today_vol", 0)),
            }
            if px:
                mv += px * p["volume"]
        return {
            "available": capital > 0,
            "account": acc,
            "capital": capital,
            "cash": round(cash, 2),
            "market_value": round(mv, 2) if mv else None,
            "positions": pos_out,
            "source": "交易记录插件（实盘）",
        }

    # ------------------------------------------------------------------
    # 面板
    # ------------------------------------------------------------------
    def build_panel(self, parent):
        c = self.api.colors()
        self._frame = tk.Frame(parent, bg=c["DARK_BG"])

        # 顶部按钮栏
        btn_frame = tk.Frame(self._frame, bg=c["DARK_BG"])
        btn_frame.pack(fill="x", padx=6, pady=(6, 2))

        self._btn_add = tk.Button(
            btn_frame, text="添加记录", command=self._show_add_dialog,
            bg=c["BTN_BG"], fg=c["BTN_FG"],
            activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei", 9))
        self._btn_add.pack(side="left", padx=(0, 4))

        self._btn_delete = tk.Button(
            btn_frame, text="删除选中", command=self._delete_selected,
            bg=c["BTN_BG"], fg=c["BTN_FG"],
            activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei", 9))
        self._btn_delete.pack(side="left", padx=(0, 4))

        self._btn_settings = tk.Button(
            btn_frame, text="设置", command=self._show_settings,
            bg=c["BTN_BG"], fg=c["BTN_FG"],
            activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei", 9))
        self._btn_settings.pack(side="left", padx=(0, 4))

        self._btn_capital = tk.Button(
            btn_frame, text="本金", command=self._set_capital,
            bg=c["BTN_BG"], fg=c["BTN_FG"],
            activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei", 9))
        self._btn_capital.pack(side="left", padx=(0, 4))

        self._btn_equity = tk.Button(
            btn_frame, text="收益曲线", command=self._show_equity_dialog,
            bg=c["BTN_BG"], fg=c["BTN_FG"],
            activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei", 9))
        self._btn_equity.pack(side="left", padx=(0, 4))

        self._btn_ai = tk.Button(
            btn_frame, text="AI分析", command=self._show_ai_dialog,
            bg=c["BTN_BG"], fg=c["BTN_FG"],
            activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei", 9))
        self._btn_ai.pack(side="right")

        # 账本切换（实盘/模拟独立记账）
        acc_frame = tk.Frame(self._frame, bg=c["DARK_BG"])
        acc_frame.pack(fill="x", padx=6, pady=(0, 2))
        tk.Label(acc_frame, text="账本：", bg=c["DARK_BG"], fg=c["AXIS_TXT"],
                 font=("Microsoft YaHei", 9)).pack(side="left")
        self._acc_var = tk.StringVar(value=self._view_acc())
        self._acc_btns = {}
        for key, lab in ACCOUNTS:
            b = tk.Button(
                acc_frame, text=lab, command=lambda k=key: self._switch_acc(k),
                bg=c["BTN_BG"], fg=c["BTN_FG"],
                activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
                relief="flat", cursor="hand2",
                font=("Microsoft YaHei", 9))
            b.pack(side="left", padx=(0, 4))
            self._acc_btns[key] = b
        tk.Label(acc_frame, text="（两本账独立记账、独立本金；联动固定使用实盘）",
                 bg=c["DARK_BG"], fg=c["AXIS_TXT"],
                 font=("Microsoft YaHei", 8)).pack(side="left", padx=(8, 0))

        # 持仓信息
        self._pos_label = tk.Label(
            self._frame, text="持仓：无", justify="left", anchor="nw",
            bg=c["DARK_BG"], fg=c["FG_MAIN"],
            font=("Microsoft YaHei", 9))
        self._pos_label.pack(fill="x", padx=6, pady=2)

        # 本金/现金/市值标签
        self._cap_label = tk.Label(
            self._frame, text="", justify="left", anchor="nw",
            bg=c["DARK_BG"], fg=c["FG_MAIN"],
            font=("Microsoft YaHei", 9))
        self._cap_label.pack(fill="x", padx=6, pady=2)

        # 交易记录列表
        list_frame = tk.Frame(self._frame, bg=c["DARK_BG"])
        list_frame.pack(fill="both", expand=True, padx=6, pady=2)

        columns = ("date", "code", "name", "type", "price", "volume", "fee")
        self._tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", height=8)

        self._tree.heading("date", text="日期")
        self._tree.heading("code", text="代码")
        self._tree.heading("name", text="名称")
        self._tree.heading("type", text="类型")
        self._tree.heading("price", text="价格")
        self._tree.heading("volume", text="数量")
        self._tree.heading("fee", text="费用")

        self._tree.column("date", width=70)
        self._tree.column("code", width=65)
        self._tree.column("name", width=60)
        self._tree.column("type", width=35)
        self._tree.column("price", width=50)
        self._tree.column("volume", width=45)
        self._tree.column("fee", width=45)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical",
                                  command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)

        self._tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 底部汇总
        self._summary_label = tk.Label(
            self._frame, text="", justify="left", anchor="nw",
            bg=c["DARK_BG"], fg=c["FG_MAIN"],
            font=("Microsoft YaHei", 9))
        self._summary_label.pack(fill="x", padx=6, pady=(2, 6))

        self._refresh_list()
        return self._frame

    def _switch_acc(self, key):
        self._settings["view_account"] = key
        self._save_data()
        self._acc_var.set(key)
        self._refresh_list()

    def _refresh_list(self):
        """刷新交易记录列表。"""
        for item in self._tree.get_children():
            self._tree.delete(item)

        acc = self._view_acc()
        for rec in reversed(self._recs(acc)):
            self._tree.insert("", "end", values=(
                rec.get("date", ""),
                rec.get("code", ""),
                rec.get("name", ""),
                "买" if rec.get("type") == "BUY" else "卖",
                rec.get("price", ""),
                rec.get("volume", ""),
                rec.get("fee", ""),
            ))

        self._highlight_acc_btn()
        self._update_position_label()
        self._update_summary()
        self._update_capital_label()

    def _highlight_acc_btn(self):
        """当前账本按钮高亮。"""
        c = self.api.colors()
        acc = self._view_acc()
        for key, b in self._acc_btns.items():
            on = key == acc
            try:
                b.config(bg=c["SEL_BG"] if on else c["BTN_BG"],
                         fg=c["FG_MAIN"] if on else c["BTN_FG"])
            except Exception:
                pass

    def _update_position_label(self):
        """持仓显示：现价/市值/浮动盈亏/今仓(T+1)。"""
        acc = self._view_acc()
        _, positions = self._positions_detail(acc)
        if not positions:
            text = "持仓：无"
        else:
            items = []
            total_mv = total_cost = 0.0
            for code, p in sorted(positions.items()):
                px = self._last_price.get(code) or 0.0
                avg = p["avg_cost"]
                tag = " [今仓T+1]" if p.get("today_vol") else ""
                if px:
                    mv = px * p["volume"]
                    pnl = (px - avg) * p["volume"]
                    pct = (px / avg - 1) * 100 if avg else 0.0
                    items.append(f"{p['name']}({code[-6:]}) {p['volume']}股 "
                                 f"成本{avg:.2f} 现价{px:.2f} "
                                 f"{'+' if pnl >= 0 else ''}{pnl:.0f}元"
                                 f"({pct:+.1f}%){tag}")
                    total_mv += mv
                    total_cost += avg * p["volume"]
                else:
                    items.append(f"{p['name']}({code[-6:]}) {p['volume']}股 "
                                 f"成本{avg:.2f}{tag}")
            if total_cost:
                tot_pct = (total_mv / total_cost - 1) * 100
                items.append(f"— 合计市值{total_mv:,.0f} "
                             f"浮盈{'+' if total_mv >= total_cost else ''}"
                             f"{total_mv - total_cost:,.0f}({tot_pct:+.1f}%)")
            text = "持仓：" + " | ".join(items)
        try:
            self._pos_label.config(text=text)
        except Exception:
            pass

    def _update_summary(self):
        acc = self._view_acc()
        recs = self._recs(acc)
        total_buy = sum(r.get("volume", 0) * r.get("price", 0)
                        for r in recs if r.get("type") == "BUY")
        total_sell = sum(r.get("volume", 0) * r.get("price", 0)
                         for r in recs if r.get("type") == "SELL")
        total_fee = sum(r.get("fee", 0) for r in recs)
        text = (f"累计买入：{total_buy:.0f}元 | 累计卖出：{total_sell:.0f}元 | "
                f"累计费用：{total_fee:.2f}元 | 记录 {len(recs)} 笔")
        try:
            self._summary_label.config(text=text)
        except Exception:
            pass

    def _update_capital_label(self):
        """本金/现金/市值/总资产显示（当前账本）。"""
        acc = self._view_acc()
        name = dict(ACCOUNTS).get(acc, acc)
        capital = self._capital(acc)
        cash, positions = self._positions_detail(acc)
        try:
            if capital <= 0:
                self._cap_label.config(
                    text=f"{name}本金：未设置（点“本金”填写）")
                return
            mv = 0.0
            for code, p in positions.items():
                px = self._last_price.get(code)
                if px:
                    mv += px * p["volume"]
            total = cash + mv
            pnl = total - capital
            pct = pnl / capital * 100 if capital else 0.0
            self._cap_label.config(
                text=f"{name} | 本金 ¥{capital:,.0f} | 现金 ¥{cash:,.0f} | "
                     f"持仓市值 ¥{mv:,.0f} | 总资产 ¥{total:,.0f} "
                     f"({pnl:+,.0f}, {pct:+.2f}%)")
        except Exception:
            pass

    def _set_capital(self):
        """设置当前账本本金。"""
        acc = self._view_acc()
        name = dict(ACCOUNTS).get(acc, acc)
        cur = self._capital(acc)
        val = simpledialog.askfloat(
            f"{name}本金",
            f"输入{name}账本总资产（元）\n"
            f"（0 或留空 = 该账本不参与校验/曲线；"
            f"{'实盘且开启联动时荐股按此资金算手数' if acc == 'live' else '模拟盘'}）",
            initialvalue=cur, minvalue=0.0, parent=self._frame)
        if val is None:
            return
        key = "capital_live" if acc == "live" else "capital_sim"
        self._settings[key] = float(val)
        self._save_data()
        self._update_capital_label()

    # ------------------------------------------------------------------
    # 添加记录（交易日对齐 + 真实性校验）
    # ------------------------------------------------------------------
    def _show_add_dialog(self):
        c = self.api.colors()
        win = tk.Toplevel(self._frame)
        win.title(f"添加交易记录（{dict(ACCOUNTS).get(self._view_acc())}）")
        win.configure(bg=c["DARK_BG"])
        win.transient(self._frame)
        win.grab_set()

        result = self.api.current_result
        default_code = self.api.current_code or ""
        default_name = self.api.current_name or ""
        default_price = ""
        if result:
            q = result.get("quote") or {}
            default_price = str(q.get("price", ""))

        form = tk.Frame(win, bg=c["DARK_BG"], padx=16, pady=12)
        form.pack(fill="both", expand=True)

        def _row(r, label):
            tk.Label(form, text=label, bg=c["DARK_BG"], fg=c["FG_MAIN"],
                     font=("Microsoft YaHei", 9)).grid(
                         row=r, column=0, sticky="e", pady=4)

        _row(0, "日期：")
        date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        tk.Entry(form, textvariable=date_var, width=20,
                 font=("Microsoft YaHei", 9)).grid(row=0, column=1, pady=4)
        date_note = tk.Label(form, text="", bg=c["DARK_BG"], fg=c["AXIS_TXT"],
                             font=("Microsoft YaHei", 8))
        date_note.grid(row=0, column=2, sticky="w", padx=(6, 0))

        def align_date(*_a):
            d = self.api.align_trading_day(date_var.get().strip())
            if d and d != date_var.get().strip():
                date_var.set(d)
                date_note.config(text="已对齐到交易日")
            else:
                date_note.config(text="交易日" if d else "")

        date_var.trace_add("write", align_date)
        align_date()

        _row(1, "代码：")
        code_var = tk.StringVar(value=default_code)
        tk.Entry(form, textvariable=code_var, width=20,
                 font=("Microsoft YaHei", 9)).grid(row=1, column=1, pady=4)

        _row(2, "名称：")
        name_var = tk.StringVar(value=default_name)
        tk.Entry(form, textvariable=name_var, width=20,
                 font=("Microsoft YaHei", 9)).grid(row=2, column=1, pady=4)

        _row(3, "类型：")
        type_var = tk.StringVar(value="BUY")
        type_frame = tk.Frame(form, bg=c["DARK_BG"])
        type_frame.grid(row=3, column=1, sticky="w", pady=4)
        tk.Radiobutton(type_frame, text="买入", variable=type_var, value="BUY",
                       bg=c["DARK_BG"], fg=c["FG_MAIN"],
                       activebackground=c["DARK_BG"],
                       activeforeground=c["FG_MAIN"],
                       selectcolor=c["FIELD_BG"],
                       font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Radiobutton(type_frame, text="卖出", variable=type_var, value="SELL",
                       bg=c["DARK_BG"], fg=c["FG_MAIN"],
                       activebackground=c["DARK_BG"],
                       activeforeground=c["FG_MAIN"],
                       selectcolor=c["FIELD_BG"],
                       font=("Microsoft YaHei", 9)).pack(side="left")

        _row(4, "价格：")
        price_var = tk.StringVar(value=default_price)
        tk.Entry(form, textvariable=price_var, width=20,
                 font=("Microsoft YaHei", 9)).grid(row=4, column=1, pady=4)

        _row(5, "数量：")
        vol_var = tk.StringVar()
        tk.Entry(form, textvariable=vol_var, width=20,
                 font=("Microsoft YaHei", 9)).grid(row=5, column=1, pady=4)
        tk.Label(form, text="买入须整手(100股倍数)\n卖出可零股",
                 bg=c["DARK_BG"], fg=c["AXIS_TXT"],
                 font=("Microsoft YaHei", 8), justify="left").grid(
                     row=5, column=2, sticky="w", padx=(6, 0))

        _row(6, "费用：")
        fee_var = tk.StringVar(value="自动计算")
        tk.Label(form, textvariable=fee_var, bg=c["DARK_BG"],
                 fg=c["FG_MAIN"], font=("Microsoft YaHei", 9)).grid(
                     row=6, column=1, sticky="w", pady=4)

        def update_fee(*args):
            try:
                p = float(price_var.get())
                v = int(vol_var.get())
                is_sell = type_var.get() == "SELL"
                fee = self._calc_fee(p, v, is_sell, code_var.get().strip())
                fee_var.set(f"{fee:.2f} 元（含滑点/过户费仅沪市）")
            except Exception:
                fee_var.set("自动计算")

        for v in (price_var, vol_var, type_var, code_var):
            v.trace_add("write", update_fee)

        def on_submit():
            try:
                date = self.api.align_trading_day(date_var.get().strip())
                code = code_var.get().strip()
                name = name_var.get().strip()
                typ = type_var.get()
                price = float(price_var.get())
                volume = int(vol_var.get())
                if not code or not name:
                    messagebox.showwarning("提示", "请填写代码和名称",
                                           parent=win)
                    return
                if volume <= 0 or price <= 0:
                    messagebox.showwarning("提示", "价格/数量必须大于0",
                                           parent=win)
                    return
                if typ == "BUY" and volume % 100:
                    messagebox.showwarning(
                        "提示", "A股买入须为100股（一手）整数倍\n"
                                "卖出允许零股（清仓）", parent=win)
                    return

                acc = self._view_acc()
                is_sell = typ == "SELL"
                fee = self._calc_fee(price, volume, is_sell, code)
                rec = {"date": date, "code": code, "name": name,
                       "type": typ, "price": price, "volume": volume,
                       "fee": fee, "account": acc}
                # 真实性校验：按日期重放（卖出≤持仓含T+1、买入≤现金）
                test = self._recs(acc) + [rec]
                _cash, _pos, errs = self._replay(
                    test, self._capital(acc))
                if errs:
                    messagebox.showwarning(
                        "记录未通过真实性校验",
                        "以下问题与账本已有记录冲突：\n\n"
                        + "\n".join(errs[-5:]), parent=win)
                    return

                self._records.append(rec)
                self._save_data()
                self._refresh_list()
                win.destroy()
            except ValueError:
                messagebox.showwarning("提示", "请输入有效的价格和数量",
                                       parent=win)

        btn_frame = tk.Frame(form, bg=c["DARK_BG"])
        btn_frame.grid(row=7, column=0, columnspan=2, pady=(12, 0))

        tk.Button(btn_frame, text="确认", command=on_submit,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
                  relief="flat", cursor="hand2",
                  font=("Microsoft YaHei", 9, "bold")).pack(side="left",
                                                            padx=(0, 8))
        tk.Button(btn_frame, text="取消", command=win.destroy,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
                  relief="flat", cursor="hand2",
                  font=("Microsoft YaHei", 9)).pack(side="left")

    def _delete_selected(self):
        """删除选中的交易记录（当前账本内按 日期+代码+类型+数量 匹配）。"""
        sel = self._tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要删除的记录")
            return
        if not messagebox.askyesno("确认", "确定删除选中的记录？"):
            return
        acc = self._view_acc()
        for item in sel:
            values = self._tree.item(item, "values")
            date, code = values[0], values[1]
            typ = "BUY" if values[3] == "买" else "SELL"
            for i, rec in enumerate(self._records):
                if (rec.get("date") == date and rec.get("code") == code
                        and rec.get("type") == typ
                        and rec.get("account", "live") == acc):
                    self._records.pop(i)
                    break
        self._save_data()
        self._refresh_list()

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------
    def _show_settings(self):
        c = self.api.colors()
        win = tk.Toplevel(self._frame)
        win.title("交易设置")
        win.configure(bg=c["DARK_BG"])
        win.transient(self._frame)
        win.grab_set()

        form = tk.Frame(win, bg=c["DARK_BG"], padx=16, pady=12)
        form.pack(fill="both", expand=True)

        tk.Label(form, text="交易费率 / 联动", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 10, "bold")).grid(
                     row=0, column=0, columnspan=2, pady=(0, 8))

        def _add(r, label, key, note=""):
            tk.Label(form, text=label, bg=c["DARK_BG"], fg=c["FG_MAIN"],
                     font=("Microsoft YaHei", 9)).grid(
                         row=r, column=0, sticky="e", pady=4)
            var = tk.StringVar(value=str(self._settings.get(key, "")))
            tk.Entry(form, textvariable=var, width=20,
                     font=("Microsoft YaHei", 9)).grid(row=r, column=1,
                                                       pady=4)
            if note:
                tk.Label(form, text=note, bg=c["DARK_BG"], fg=c["AXIS_TXT"],
                         font=("Microsoft YaHei", 8)).grid(
                             row=r + 1, column=1, sticky="w")
            return var

        comm_var = _add(1, "佣金费率：", "commission", "（如0.00025为万2.5）")
        min_comm_var = _add(3, "最低佣金(元)：", "min_commission")
        stamp_var = _add(4, "印花税：", "stamp_tax",
                         "（如0.001为千1，仅卖出收取）")
        transfer_var = _add(6, "过户费：", "transfer_fee",
                            "（如0.00001为十万1，仅沪市收取）")
        slip_var = _add(8, "滑点费率：", "slippage",
                        "（双边；0.001=千1，计入费用。市价单/追单建议设0.001~0.002）")
        cap_live_var = _add(10, "实盘本金(元)：", "capital_live",
                            "（联动开启时荐股按此资金算手数）")
        cap_sim_var = _add(12, "模拟本金(元)：", "capital_sim",
                           f"（模拟盘默认 {DEFAULT_CAPITAL_SIM:,.0f}）")

        link_var = tk.BooleanVar(value=bool(
            self._settings.get("link_analysis", True)))
        tk.Checkbutton(form, text="参与信号联动（荐股/每日组合按实盘资金计算手数；"
                                  "关闭后仅正常分析）",
                       variable=link_var, bg=c["DARK_BG"], fg=c["FG_MAIN"],
                       activebackground=c["DARK_BG"],
                       activeforeground=c["FG_MAIN"],
                       selectcolor=c["FIELD_BG"],
                       font=("Microsoft YaHei", 9)).grid(
                           row=14, column=0, columnspan=2, sticky="w", pady=4)

        def on_save():
            try:
                self._settings["commission"] = float(comm_var.get())
                self._settings["min_commission"] = float(min_comm_var.get())
                self._settings["stamp_tax"] = float(stamp_var.get())
                self._settings["transfer_fee"] = float(transfer_var.get())
                self._settings["slippage"] = max(
                    0.0, float(slip_var.get() or 0))
                self._settings["capital_live"] = max(
                    0.0, float(cap_live_var.get() or 0))
                self._settings["capital_sim"] = max(
                    0.0, float(cap_sim_var.get() or 0))
                self._settings["link_analysis"] = bool(link_var.get())
                self._save_data()
                self._update_capital_label()
                messagebox.showinfo("提示", "设置已保存", parent=win)
                win.destroy()
            except ValueError:
                messagebox.showwarning("提示", "请输入有效的数值", parent=win)

        btn_frame = tk.Frame(form, bg=c["DARK_BG"])
        btn_frame.grid(row=15, column=0, columnspan=2, pady=(12, 0))

        tk.Button(btn_frame, text="保存", command=on_save,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
                  relief="flat", cursor="hand2",
                  font=("Microsoft YaHei", 9, "bold")).pack(side="left",
                                                            padx=(0, 8))
        tk.Button(btn_frame, text="取消", command=win.destroy,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
                  relief="flat", cursor="hand2",
                  font=("Microsoft YaHei", 9)).pack(side="left")

    # ------------------------------------------------------------------
    # 收益曲线（逐交易日净值：现金 + 持仓按当日收盘市值）
    # ------------------------------------------------------------------
    def _equity_series(self, acc=None):
        """计算某账本逐交易日净值。

        返回 (dates, net_values, stats) 或 (None, None, reason)。
        net_values 为归一化净值（起点=本金/本金=1.0）；市值用库内显示价
        （与图表/现价同量纲），停牌日沿用最近收盘。"""
        acc = acc or self._view_acc()
        capital = self._capital(acc)
        recs = self._recs(acc)
        if capital <= 0:
            return None, None, "该账本未设置本金（点“本金”填写）"
        if not recs:
            return None, None, "该账本暂无交易记录"
        days = sorted(self.api.trading_days())
        if not days:
            return None, None, "宿主交易日历为空（库内无指数日K）"
        first = min(r.get("date", "") for r in recs)
        prevs = [d for d in days if d < first]
        start = prevs[-1] if prevs else first
        today = datetime.now().strftime("%Y-%m-%d")
        span = [d for d in days if start <= d <= max(today, recs[-1]["date"])]
        if not span:
            return None, None, "交易日历未覆盖记录区间"

        close_cache = {}
        hold = {}            # code -> vol
        cash = capital
        cur_date = None
        dates, nets = [], []
        idx = 0
        recs_by_date = {}
        for r in recs:
            recs_by_date.setdefault(r["date"], []).append(r)
        for d in span:
            for rec in recs_by_date.get(d, []):
                code = rec.get("code", "")
                vol = int(rec.get("volume", 0) or 0)
                if code not in close_cache:
                    close_cache[code] = self.api.close_map(code)
                if rec.get("type") == "SELL":
                    hold[code] = hold.get(code, 0) - vol
                    cash += rec.get("price", 0) * vol - rec.get("fee", 0)
                else:
                    hold[code] = hold.get(code, 0) + vol
                    cash -= rec.get("price", 0) * vol + rec.get("fee", 0)
            mv = 0.0
            for code, vol in hold.items():
                if vol <= 0:
                    continue
                if code not in close_cache:
                    close_cache[code] = self.api.close_map(code)
                cm = close_cache[code]
                px = cm.get(d)
                if px is None:
                    earlier = [x for x in cm if x <= d]
                    px = cm[earlier[-1]] if earlier else \
                        (self._last_price.get(code) or 0.0)
                mv += px * vol
            dates.append(d)
            nets.append((cash + mv) / capital)
        if not nets:
            return None, None, "无有效净值点"
        peak, mdd = nets[0], 0.0
        for v in nets:
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, 1 - v / peak)
        total_ret = nets[-1] - 1.0
        try:
            d0 = datetime.strptime(dates[0], "%Y-%m-%d")
            d1 = datetime.strptime(dates[-1], "%Y-%m-%d")
            years = max(0.08, (d1 - d0).days / 365.25)
            ann = (nets[-1]) ** (1 / years) - 1 if nets[-1] > 0 else -1.0
        except Exception:
            ann = 0.0
        stats = {"total": total_ret, "mdd": mdd, "ann": ann,
                 "n_trades": len(recs), "days": len(dates)}
        return dates, nets, stats

    def _show_equity_dialog(self):
        c = self.api.colors()
        acc = self._view_acc()
        name = dict(ACCOUNTS).get(acc, acc)
        win = tk.Toplevel(self._frame)
        win.title(f"账户收益曲线 · {name}")
        win.configure(bg=c["DARK_BG"])
        win.transient(self._frame)
        win.geometry("880x480")

        top = tk.Frame(win, bg=c["DARK_BG"])
        top.pack(fill="x", padx=8, pady=(8, 0))
        info = tk.Label(top, text="", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                        font=("Microsoft YaHei", 10), anchor="w",
                        justify="left")
        info.pack(side="left", fill="x", expand=True)

        cv = tk.Canvas(win, bg=c["PANEL_BG"], highlightthickness=1,
                       highlightbackground=c.get("BORDER", "#333"))
        cv.pack(fill="both", expand=True, padx=8, pady=8)

        result = self._equity_series(acc)
        state = {"dates": None, "nets": None}

        def draw(_e=None):
            dates, nets = state["dates"], state["nets"]
            cv.delete("all")
            if dates is None:
                cv.create_text(cv.winfo_width() // 2 or 400,
                               cv.winfo_height() // 2 or 200,
                               text=str(state.get("stats_msg") or "无数据"),
                               fill=c["AXIS_TXT"],
                               font=("Microsoft YaHei", 11))
                return
            w = max(cv.winfo_width(), 600)
            h = max(cv.winfo_height(), 300)
            L, R, T, B = 64, 16, 30, 34
            lo = min(min(nets), 0.98)
            hi = max(max(nets), 1.02)
            pad = (hi - lo) * 0.08 or 0.02
            lo, hi = lo - pad, hi + pad

            def ym(v):
                return T + (hi - v) / (hi - lo) * (h - T - B)

            def xm(i):
                return L + i / max(1, len(nets) - 1) * (w - L - R)

            # 网格 + 轴标签
            for k in range(5):
                v = lo + (hi - lo) * k / 4
                yy = ym(v)
                cv.create_line(L, yy, w - R, yy, fill=c["GRID_C"],
                               dash=(2, 3))
                cv.create_text(L - 4, yy, anchor="e",
                               text=f"{(v) * 100:.0f}%", fill=c["AXIS_TXT"],
                               font=("Consolas", 8))
            # 基准线 1.0
            cv.create_line(L, ym(1.0), w - R, ym(1.0),
                           fill=c.get("GUIDE_C", "#666"), dash=(4, 3))
            cv.create_text(L - 4, ym(1.0), anchor="e", text="本金",
                           fill=c["AXIS_TXT"], font=("Microsoft YaHei", 8))
            # 回撤区域（净值到区间顶）
            pts = [(xm(i), ym(v)) for i, v in enumerate(nets)]
            poly = [xm(0), ym(hi)] 
            poly += [coord for p in pts for coord in p]
            poly += [xm(len(nets) - 1), ym(hi)]
            cv.create_polygon(poly, fill=c["DOWN"], stipple="gray25",
                              outline="")
            # 净值折线
            if len(pts) >= 2:
                flat = [coord for p in pts for coord in p]
                cv.create_line(*flat, fill=c["UP"], width=2,
                               joinstyle="round", capstyle="round")
            # 日期轴
            for i in (0, len(nets) // 2, len(nets) - 1):
                cv.create_text(xm(i), h - B + 12, text=dates[i],
                               fill=c["AXIS_TXT"], font=("Consolas", 8))
            cv.create_text(L, T - 10, anchor="w",
                           text=f"{name}净值（起点=本金 1.00）",
                           fill=c["TITLE_TXT"],
                           font=("Microsoft YaHei", 9, "bold"))

        if isinstance(result[2], dict):
            state["dates"], state["nets"] = result[0], result[1]
            st = result[2]
            info.config(
                text=f"总收益 {st['total'] * 100:+.2f}%   年化 "
                     f"{st['ann'] * 100:+.1f}%   最大回撤 "
                     f"{st['mdd'] * 100:.1f}%   交易 {st['n_trades']} 笔 / "
                     f"{st['days']} 个交易日")
        else:
            state["dates"] = None
            state["stats_msg"] = result[2]
            info.config(text="")

        def paint():
            draw()
            if not isinstance(result[2], dict):
                cv.create_text(cv.winfo_width() // 2 or 400,
                               cv.winfo_height() // 2 or 200,
                               text=str(result[2]), fill=c["AXIS_TXT"],
                               font=("Microsoft YaHei", 11))

        cv.bind("<Configure>", lambda e: draw())
        win.after(120, draw)
        btns = tk.Frame(win, bg=c["DARK_BG"])
        btns.pack(fill="x", padx=8, pady=(0, 8))
        tk.Button(btns, text="关闭", command=win.destroy,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
                  relief="flat", cursor="hand2",
                  font=("Microsoft YaHei", 9)).pack(side="right")

    # ------------------------------------------------------------------
    # AI 分析（保持原逻辑，提示词带账本区分）
    # ------------------------------------------------------------------
    def _show_ai_dialog(self):
        c = self.api.colors()
        win = tk.Toplevel(self._frame)
        win.title("AI 交易分析")
        win.configure(bg=c["DARK_BG"])
        win.transient(self._frame)

        ai_text = tk.Text(win, height=20, bg=c["PANEL_BG"], fg=c["FG_MAIN"],
                          font=("Microsoft YaHei", 10), relief="flat",
                          wrap="word", state="disabled")
        ai_scroll = ttk.Scrollbar(win, command=ai_text.yview)
        ai_text.configure(yscrollcommand=ai_scroll.set)
        ai_scroll.pack(side="right", fill="y")
        ai_text.pack(fill="both", expand=True, padx=6, pady=6)

        def _render():
            ai_text.config(state="normal")
            ai_text.delete("1.0", "end")
            if not self._ai_msgs:
                ai_text.insert("end", "点击【开始分析】，AI将基于你的交易记录进行分析。")
            else:
                for m in self._ai_msgs:
                    who = "你" if m["role"] == "user" else "AI"
                    ai_text.insert("end", f"── {who} ──\n{m['content']}\n\n")
            ai_text.config(state="disabled")
            ai_text.see("end")

        def _ensure_key():
            key = self.api_key
            if key:
                return key
            key = simpledialog.askstring(
                "DeepSeek API Key",
                "首次使用请输入 DeepSeek API Key\n"
                "（建议配置环境变量 DEEPSEEK_API_KEY）：",
                show="*", parent=win)
            if not key:
                return None
            self.api_key = key.strip()
            return self.api_key

        def _build_prompt():
            acc = self._view_acc()
            name = dict(ACCOUNTS).get(acc, acc)
            _, positions = self._positions_detail(acc)
            pos_out = {}
            for code, p in positions.items():
                px = self._last_price.get(code) or 0.0
                pos_out[code] = {
                    "name": p["name"], "volume": p["volume"],
                    "avg_cost": round(p["avg_cost"], 3),
                    "last_price": px or None,
                    "pnl_pct": (round((px / p["avg_cost"] - 1) * 100, 2)
                                if px and p["avg_cost"] else None),
                    "today_buy_T1": p.get("today_vol", 0),
                }
            pos_text = json.dumps(pos_out, ensure_ascii=False, indent=2)
            records_text = json.dumps(
                self._recs(acc)[-20:], ensure_ascii=False, indent=2)
            result = self.api.current_result
            analysis_text = json.dumps(
                {k: v for k, v in (result or {}).items()
                 if k in ("action", "band_note", "t_pred", "bt_stats",
                          "strategy")},
                ensure_ascii=False, indent=2, default=str)
            capital = self._capital(acc)
            cash, _ = self._positions_detail(acc)
            st = self._equity_series(acc)
            eq_txt = (f"总收益{st[2]['total'] * 100:+.1f}% 回撤"
                      f"{st[2]['mdd'] * 100:.1f}%"
                      if isinstance(st[2], dict) else "暂无（未设本金或无记录）")

            prompt = f"""请基于以下数据进行交易分析（{name}账本）：

【账户】本金 {capital:,.0f} 元，可用现金 {cash:,.0f} 元；历史收益曲线：{eq_txt}
（持仓中 today_buy_T1>0 的部分当日买入、次日才可卖出，A股 T+1）

【当前持仓（含浮动盈亏）】
{pos_text}

【最近交易记录】
{records_text}

【当前股票分析结果（节选）】
{analysis_text}

请分析：
1. 当前持仓的风险评估（结合浮盈与T+1约束）
2. 买入/卖出时机建议
3. 仓位管理建议（可用现金约束下）
4. 其他需要注意的风险点"""
            return prompt

        def _call_ai():
            key = _ensure_key()
            if not key:
                return
            prompt = _build_prompt()
            self._ai_msgs = [{"role": "user", "content": prompt}]
            _render()

            ai_text.config(state="normal")
            ai_text.insert("end", "\nAI 分析中...\n")
            ai_text.config(state="disabled")

            msgs = list(self._ai_msgs)
            import threading

            def run():
                try:
                    from stock_gui import _deepseek_chat
                    result = _deepseek_chat(key, msgs)
                    self.api.on_ui_thread(lambda: done(result, None))
                except Exception as e:
                    self.api.on_ui_thread(lambda: done(None, str(e)))

            threading.Thread(target=run, daemon=True).start()

            def done(text, err):
                if err:
                    self._ai_msgs = self._ai_msgs[:-1]
                    ai_text.config(state="normal")
                    ai_text.insert("end",
                                   f"\n[AI 分析失败：{err}\n请检查 API Key 与网络。]\n")
                    ai_text.config(state="disabled")
                else:
                    self._ai_msgs.append({"role": "assistant",
                                          "content": text})
                    _render()

        btn_frame = tk.Frame(win, bg=c["DARK_BG"])
        btn_frame.pack(fill="x", padx=6, pady=6)

        tk.Button(btn_frame, text="开始分析", command=_call_ai,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
                  relief="flat", cursor="hand2",
                  font=("Microsoft YaHei", 9, "bold")).pack(side="left")
        tk.Button(btn_frame, text="关闭", command=win.destroy,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
                  relief="flat", cursor="hand2",
                  font=("Microsoft YaHei", 9)).pack(side="right")

        _render()

    # ------------------------------------------------------------------
    # 宿主钩子
    # ------------------------------------------------------------------
    def on_analysis(self, result):
        """分析完成：缓存现价并刷新持仓显示。"""
        try:
            q = (result or {}).get("quote") or {}
            code = (result or {}).get("full_code")
            px = q.get("price")
            if code and px:
                self._last_price[code] = float(px)
            self._update_position_label()
            self._update_capital_label()
        except Exception:
            pass

    def on_tick(self, quote):
        """行情快照：更新现价缓存（隔夜持仓实时盈亏）。

        快照 dict 不含代码字段，tick 恒为当前分析股票 → 从 api.current_code 取。"""
        try:
            code = self.api.current_code
            px = (quote or {}).get("price")
            if code and px:
                self._last_price[code] = float(px)
                self._update_position_label()
                self._update_capital_label()
        except Exception:
            pass

    def on_theme_changed(self):
        """主题切换。"""
        c = self.api.colors()
        try:
            self._frame.config(bg=c["DARK_BG"])
            self._pos_label.config(bg=c["DARK_BG"], fg=c["FG_MAIN"])
            self._cap_label.config(bg=c["DARK_BG"], fg=c["FG_MAIN"])
            self._summary_label.config(bg=c["DARK_BG"], fg=c["FG_MAIN"])
            for b in (self._btn_add, self._btn_delete, self._btn_settings,
                      self._btn_capital, self._btn_equity, self._btn_ai):
                b.config(bg=c["BTN_BG"], fg=c["BTN_FG"],
                         activebackground=c["BTN_HOVER"])
            self._highlight_acc_btn()
        except Exception:
            pass

    def on_close(self):
        """程序退出，保存数据。"""
        self._save_data()
