# -*- coding: utf-8 -*-
"""交易记录插件。

在右侧栏显示交易记录管理面板，支持：
- 添加买入/卖出记录
- 显示当前持仓
- 删除交易记录
- 集成AI分析
- 设置手续费、印花税等参数
"""

import json
import os
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
from datetime import datetime

from .base import StockPlugin


class TradeLogPlugin(StockPlugin):
    """交易记录插件。"""

    name = "交易记录"
    order = 10  # 排在预测参考之后

    def __init__(self, api):
        super().__init__(api)
        self._records = []  # 交易记录列表
        self._settings = {
            "commission": 0.00025,  # 佣金费率（万2.5）
            "min_commission": 5.0,  # 最低佣金（元）
            "stamp_tax": 0.001,     # 印花税（千1，仅卖出）
            "transfer_fee": 0.00001,  # 过户费（十万1）
        }
        self._ai_msgs = []  # AI对话历史
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        self._load_data()

    def _load_data(self):
        """加载交易记录和设置。"""
        try:
            records_path = self.api.storage_path("trade_records.json")
            if os.path.exists(records_path):
                with open(records_path, "r", encoding="utf-8") as f:
                    self._records = json.load(f)
        except Exception:
            self._records = []

        try:
            settings_path = self.api.storage_path("trade_settings.json")
            if os.path.exists(settings_path):
                with open(settings_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    self._settings.update(saved)
        except Exception:
            pass

    def _save_data(self):
        """保存交易记录和设置。"""
        try:
            records_path = self.api.storage_path("trade_records.json")
            with open(records_path, "w", encoding="utf-8") as f:
                json.dump(self._records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.api.log.exception("保存交易记录失败")

        try:
            settings_path = self.api.storage_path("trade_settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.api.log.exception("保存设置失败")

    def _calc_fee(self, price, volume, is_sell=False):
        """计算交易费用。"""
        amount = price * volume
        commission = max(amount * self._settings["commission"],
                         self._settings["min_commission"])
        stamp_tax = amount * self._settings["stamp_tax"] if is_sell else 0
        transfer_fee = amount * self._settings["transfer_fee"]
        return round(commission + stamp_tax + transfer_fee, 2)

    def _calc_position(self):
        """计算当前持仓。"""
        position = {}
        for rec in self._records:
            code = rec.get("code", "")
            if code not in position:
                position[code] = {
                    "name": rec.get("name", ""),
                    "volume": 0,
                    "cost": 0.0,
                    "total_fee": 0.0,
                }
            pos = position[code]
            vol = rec.get("volume", 0)
            price = rec.get("price", 0)
            fee = rec.get("fee", 0)

            if rec.get("type") == "BUY":
                pos["volume"] += vol
                pos["cost"] += price * vol
                pos["total_fee"] += fee
            else:  # SELL
                pos["volume"] -= vol
                pos["cost"] -= price * vol
                pos["total_fee"] += fee

        # 过滤掉已清仓的
        return {k: v for k, v in position.items() if v["volume"] > 0}

    def build_panel(self, parent):
        """创建面板。"""
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
        self._btn_settings.pack(side="left")

        self._btn_ai = tk.Button(
            btn_frame, text="AI分析", command=self._show_ai_dialog,
            bg=c["BTN_BG"], fg=c["BTN_FG"],
            activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei", 9))
        self._btn_ai.pack(side="right")

        # 持仓信息标签
        self._pos_label = tk.Label(
            self._frame, text="持仓：无", justify="left", anchor="nw",
            bg=c["DARK_BG"], fg=c["FG_MAIN"],
            font=("Microsoft YaHei", 9))
        self._pos_label.pack(fill="x", padx=6, pady=2)

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

    def _refresh_list(self):
        """刷新交易记录列表。"""
        for item in self._tree.get_children():
            self._tree.delete(item)

        for rec in reversed(self._records):
            self._tree.insert("", "end", values=(
                rec.get("date", ""),
                rec.get("code", ""),
                rec.get("name", ""),
                "买" if rec.get("type") == "BUY" else "卖",
                rec.get("price", ""),
                rec.get("volume", ""),
                rec.get("fee", ""),
            ))

        self._update_position_label()
        self._update_summary()

    def _update_position_label(self):
        """更新持仓显示。"""
        position = self._calc_position()
        if not position:
            text = "持仓：无"
        else:
            items = []
            for code, pos in position.items():
                avg_cost = pos["cost"] / pos["volume"] if pos["volume"] > 0 else 0
                items.append(f"{pos['name']}({code[-6:]}) {pos['volume']}股 "
                             f"成本{avg_cost:.2f}")
            text = "持仓：" + " | ".join(items)
        try:
            self._pos_label.config(text=text)
        except Exception:
            pass

    def _update_summary(self):
        """更新汇总信息。"""
        total_buy = sum(r.get("volume", 0) * r.get("price", 0)
                        for r in self._records if r.get("type") == "BUY")
        total_sell = sum(r.get("volume", 0) * r.get("price", 0)
                         for r in self._records if r.get("type") == "SELL")
        total_fee = sum(r.get("fee", 0) for r in self._records)
        text = f"累计买入：{total_buy:.0f}元 | 累计卖出：{total_sell:.0f}元 | "
        text += f"累计费用：{total_fee:.2f}元"
        try:
            self._summary_label.config(text=text)
        except Exception:
            pass

    def _show_add_dialog(self):
        """显示添加交易记录对话框。"""
        c = self.api.colors()
        win = tk.Toplevel(self._frame)
        win.title("添加交易记录")
        win.configure(bg=c["DARK_BG"])
        win.transient(self._frame)
        win.grab_set()

        # 获取当前股票信息
        result = self.api.current_result
        default_code = self.api.current_code or ""
        default_name = self.api.current_name or ""
        default_price = ""
        if result:
            q = result.get("quote") or {}
            default_price = str(q.get("price", ""))

        form = tk.Frame(win, bg=c["DARK_BG"], padx=16, pady=12)
        form.pack(fill="both", expand=True)

        # 日期
        tk.Label(form, text="日期：", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 9)).grid(row=0, column=0, sticky="e", pady=4)
        date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        tk.Entry(form, textvariable=date_var, width=20,
                 font=("Microsoft YaHei", 9)).grid(row=0, column=1, pady=4)

        # 代码
        tk.Label(form, text="代码：", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 9)).grid(row=1, column=0, sticky="e", pady=4)
        code_var = tk.StringVar(value=default_code)
        tk.Entry(form, textvariable=code_var, width=20,
                 font=("Microsoft YaHei", 9)).grid(row=1, column=1, pady=4)

        # 名称
        tk.Label(form, text="名称：", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 9)).grid(row=2, column=0, sticky="e", pady=4)
        name_var = tk.StringVar(value=default_name)
        tk.Entry(form, textvariable=name_var, width=20,
                 font=("Microsoft YaHei", 9)).grid(row=2, column=1, pady=4)

        # 类型
        tk.Label(form, text="类型：", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 9)).grid(row=3, column=0, sticky="e", pady=4)
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

        # 价格
        tk.Label(form, text="价格：", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 9)).grid(row=4, column=0, sticky="e", pady=4)
        price_var = tk.StringVar(value=default_price)
        tk.Entry(form, textvariable=price_var, width=20,
                 font=("Microsoft YaHei", 9)).grid(row=4, column=1, pady=4)

        # 数量
        tk.Label(form, text="数量：", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 9)).grid(row=5, column=0, sticky="e", pady=4)
        vol_var = tk.StringVar()
        tk.Entry(form, textvariable=vol_var, width=20,
                 font=("Microsoft YaHei", 9)).grid(row=5, column=1, pady=4)

        # 费用（自动计算）
        tk.Label(form, text="费用：", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 9)).grid(row=6, column=0, sticky="e", pady=4)
        fee_var = tk.StringVar(value="自动计算")
        fee_label = tk.Label(form, textvariable=fee_var, bg=c["DARK_BG"],
                             fg=c["FG_MAIN"], font=("Microsoft YaHei", 9))
        fee_label.grid(row=6, column=1, sticky="w", pady=4)

        def update_fee(*args):
            try:
                p = float(price_var.get())
                v = int(vol_var.get())
                is_sell = type_var.get() == "SELL"
                fee = self._calc_fee(p, v, is_sell)
                fee_var.set(f"{fee:.2f} 元")
            except Exception:
                fee_var.set("自动计算")

        price_var.trace_add("write", update_fee)
        vol_var.trace_add("write", update_fee)
        type_var.trace_add("write", update_fee)

        def on_submit():
            try:
                date = date_var.get().strip()
                code = code_var.get().strip()
                name = name_var.get().strip()
                typ = type_var.get()
                price = float(price_var.get())
                volume = int(vol_var.get())
                if not code or not name:
                    messagebox.showwarning("提示", "请填写代码和名称", parent=win)
                    return
                if volume <= 0:
                    messagebox.showwarning("提示", "数量必须大于0", parent=win)
                    return

                is_sell = typ == "SELL"
                fee = self._calc_fee(price, volume, is_sell)

                record = {
                    "date": date,
                    "code": code,
                    "name": name,
                    "type": typ,
                    "price": price,
                    "volume": volume,
                    "fee": fee,
                }
                self._records.append(record)
                self._save_data()
                self._refresh_list()
                win.destroy()
            except ValueError:
                messagebox.showwarning("提示", "请输入有效的价格和数量", parent=win)

        btn_frame = tk.Frame(form, bg=c["DARK_BG"])
        btn_frame.grid(row=7, column=0, columnspan=2, pady=(12, 0))

        tk.Button(btn_frame, text="确认", command=on_submit,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
                  relief="flat", cursor="hand2",
                  font=("Microsoft YaHei", 9, "bold")).pack(side="left", padx=(0, 8))
        tk.Button(btn_frame, text="取消", command=win.destroy,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
                  relief="flat", cursor="hand2",
                  font=("Microsoft YaHei", 9)).pack(side="left")

    def _delete_selected(self):
        """删除选中的交易记录。"""
        sel = self._tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要删除的记录")
            return

        if not messagebox.askyesno("确认", "确定删除选中的记录？"):
            return

        # 获取选中行的日期和代码，反向查找并删除
        for item in sel:
            values = self._tree.item(item, "values")
            date, code = values[0], values[1]
            for i, rec in enumerate(self._records):
                if rec.get("date") == date and rec.get("code") == code:
                    self._records.pop(i)
                    break

        self._save_data()
        self._refresh_list()

    def _show_settings(self):
        """显示设置对话框。"""
        c = self.api.colors()
        win = tk.Toplevel(self._frame)
        win.title("交易设置")
        win.configure(bg=c["DARK_BG"])
        win.transient(self._frame)
        win.grab_set()

        form = tk.Frame(win, bg=c["DARK_BG"], padx=16, pady=12)
        form.pack(fill="both", expand=True)

        tk.Label(form, text="交易费率设置", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 10, "bold")).grid(
                     row=0, column=0, columnspan=2, pady=(0, 8))

        # 佣金费率
        tk.Label(form, text="佣金费率：", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 9)).grid(row=1, column=0, sticky="e", pady=4)
        comm_var = tk.StringVar(value=str(self._settings["commission"]))
        tk.Entry(form, textvariable=comm_var, width=20,
                 font=("Microsoft YaHei", 9)).grid(row=1, column=1, pady=4)
        tk.Label(form, text="（如0.00025为万2.5）", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 8)).grid(row=2, column=1, sticky="w")

        # 最低佣金
        tk.Label(form, text="最低佣金(元)：", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 9)).grid(row=3, column=0, sticky="e", pady=4)
        min_comm_var = tk.StringVar(value=str(self._settings["min_commission"]))
        tk.Entry(form, textvariable=min_comm_var, width=20,
                 font=("Microsoft YaHei", 9)).grid(row=3, column=1, pady=4)

        # 印花税
        tk.Label(form, text="印花税：", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 9)).grid(row=4, column=0, sticky="e", pady=4)
        stamp_var = tk.StringVar(value=str(self._settings["stamp_tax"]))
        tk.Entry(form, textvariable=stamp_var, width=20,
                 font=("Microsoft YaHei", 9)).grid(row=4, column=1, pady=4)
        tk.Label(form, text="（如0.001为千1，仅卖出收取）", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 8)).grid(row=5, column=1, sticky="w")

        # 过户费
        tk.Label(form, text="过户费：", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 9)).grid(row=6, column=0, sticky="e", pady=4)
        transfer_var = tk.StringVar(value=str(self._settings["transfer_fee"]))
        tk.Entry(form, textvariable=transfer_var, width=20,
                 font=("Microsoft YaHei", 9)).grid(row=6, column=1, pady=4)
        tk.Label(form, text="（如0.00001为十万1）", bg=c["DARK_BG"], fg=c["FG_MAIN"],
                 font=("Microsoft YaHei", 8)).grid(row=7, column=1, sticky="w")

        def on_save():
            try:
                self._settings["commission"] = float(comm_var.get())
                self._settings["min_commission"] = float(min_comm_var.get())
                self._settings["stamp_tax"] = float(stamp_var.get())
                self._settings["transfer_fee"] = float(transfer_var.get())
                self._save_data()
                messagebox.showinfo("提示", "设置已保存", parent=win)
                win.destroy()
            except ValueError:
                messagebox.showwarning("提示", "请输入有效的数值", parent=win)

        btn_frame = tk.Frame(form, bg=c["DARK_BG"])
        btn_frame.grid(row=8, column=0, columnspan=2, pady=(12, 0))

        tk.Button(btn_frame, text="保存", command=on_save,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
                  relief="flat", cursor="hand2",
                  font=("Microsoft YaHei", 9, "bold")).pack(side="left", padx=(0, 8))
        tk.Button(btn_frame, text="取消", command=win.destroy,
                  bg=c["BTN_BG"], fg=c["BTN_FG"],
                  activebackground=c["BTN_HOVER"], activeforeground=c["BTN_FG"],
                  relief="flat", cursor="hand2",
                  font=("Microsoft YaHei", 9)).pack(side="left")

    def _show_ai_dialog(self):
        """显示AI分析对话框。"""
        c = self.api.colors()
        win = tk.Toplevel(self._frame)
        win.title("AI 交易分析")
        win.configure(bg=c["DARK_BG"])
        win.transient(self._frame)

        # AI结果显示区
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
            """构建分析提示词。"""
            position = self._calc_position()
            pos_text = json.dumps(position, ensure_ascii=False, indent=2)
            records_text = json.dumps(self._records[-20:], ensure_ascii=False,
                                      indent=2)
            result = self.api.current_result
            analysis_text = json.dumps(result or {}, ensure_ascii=False,
                                       indent=2)

            prompt = f"""请基于以下数据进行交易分析：

【当前持仓】
{pos_text}

【最近交易记录】
{records_text}

【当前股票分析结果】
{analysis_text}

请分析：
1. 当前持仓的风险评估
2. 买入/卖出时机建议
3. 仓位管理建议
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

            def bg():
                from stock_gui import _deepseek_chat
                return _deepseek_chat(key, msgs)

            def done(text, err):
                if err:
                    self._ai_msgs = self._ai_msgs[:-1]
                    ai_text.config(state="normal")
                    ai_text.insert("end",
                                   f"\n[AI 分析失败：{err}\n请检查 API Key 与网络。]\n")
                    ai_text.config(state="disabled")
                else:
                    self._ai_msgs.append({"role": "assistant", "content": text})
                    _render()

            self.api.on_ui_thread(lambda: None)
            import threading

            def run():
                try:
                    result = bg()
                    self.api.on_ui_thread(lambda: done(result, None))
                except Exception as e:
                    self.api.on_ui_thread(lambda: done(None, str(e)))

            threading.Thread(target=run, daemon=True).start()

        # 底部按钮栏
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

    def on_analysis(self, result):
        """分析完成/切换股票。"""
        pass  # 交易记录插件不需要主动响应分析

    def on_tick(self, quote):
        """行情快照刷新。"""
        pass

    def on_theme_changed(self):
        """主题切换。"""
        c = self.api.colors()
        try:
            self._frame.config(bg=c["DARK_BG"])
            self._pos_label.config(bg=c["DARK_BG"], fg=c["FG_MAIN"])
            self._summary_label.config(bg=c["DARK_BG"], fg=c["FG_MAIN"])
            self._btn_add.config(bg=c["BTN_BG"], fg=c["BTN_FG"],
                                 activebackground=c["BTN_HOVER"])
            self._btn_delete.config(bg=c["BTN_BG"], fg=c["BTN_FG"],
                                    activebackground=c["BTN_HOVER"])
            self._btn_settings.config(bg=c["BTN_BG"], fg=c["BTN_FG"],
                                      activebackground=c["BTN_HOVER"])
            self._btn_ai.config(bg=c["BTN_BG"], fg=c["BTN_FG"],
                                activebackground=c["BTN_HOVER"])
        except Exception:
            pass

    def on_close(self):
        """程序退出，保存数据。"""
        self._save_data()
