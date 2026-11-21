#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯脚本自动画线（不依赖 AI / GUI）。

读取本地日K缓存（stock_cache.db，自动按 adjust 还原为展示价），用多轮
「生成 → 评分 → 精修 → 去重」优化支撑/压力位、趋势线、通道线与区间，
输出可直接被 stock_gui.py 的 ai-lines 解析器使用的 JSON。

用法：
  python auto_lines.py 600000 --days 240 --rounds 3 --report reports/auto_600000.md
  python auto_lines.py sh600000 --out auto.json
  python auto_lines.py --csv bars.csv --days 240

设计要点：
  1. 多尺度分形枢轴（2/3/5/8/13 根）+ ATR/成交量加权，避免单一周期漏点；
  2. 水平位：价格聚类 + 触及/反弹/跌破 + 近期权重 + 成交量节点；
  3. 趋势线：枢轴两点连线 → 触及/违约评分 → Theil-Sen 精修 → 非极大抑制；
  4. 通道线：趋势线一侧的高/低点分位宽度，再按触及数优选；
  5. 区间：近期震荡窗口 + 支撑/压力边界吸附；
  6. 每一轮收紧容差并精修，报告输出每轮候选数与最高分，便于人工复核。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = (os.environ.get("STOCK_DB") or "").strip() or os.path.join(
    HERE, "stock_cache.db")
DEFAULT_DAYS = 240
DEFAULT_ROUNDS = 3

_AI_LINE_KINDS = {"buy", "sell", "stop", "support", "resistance", "target"}
_AI_SEG_KINDS = {"trend", "support", "resistance", "channel", "ray"}
_AI_ZONE_KINDS = {"zone", "channel", "risk", "target"}
_AI_MAX_LEVELS = 6
_AI_MAX_SEGS = 5
_AI_MAX_ZONES = 1


# ======================== 数据读取 ========================

def normalize_code(code: str) -> str:
    """轻量代码归一化：600000→sh600000、000001→sz000001、5xxxxx→sh。"""
    s = str(code or "").strip().lower()
    s = "".join(ch for ch in s if ch.isascii() and ch.isalnum())
    if not s:
        raise ValueError("代码为空")
    for p in ("crypto", "hk", "us", "fu"):
        if s.startswith(p) and len(s) > len(p):
            return s
    if s.startswith(("sh", "sz", "bj")):
        return s
    if s.isdigit():
        if len(s) == 6:
            if s[0] in "69":
                return "sh" + s
            if s[0] in "03":
                return "sz" + s
            if s[0] in "48":
                return "bj" + s
            if s[0] == "5":
                return "sh" + s
            if s[0] == "1":
                return "sz" + s
        return "sh" + s
    return s


def _bar_valid(b: dict) -> bool:
    try:
        o, h, l, c = (float(b["open"]), float(b["high"]),
                      float(b["low"]), float(b["close"]))
    except (KeyError, TypeError, ValueError):
        return False
    if min(o, h, l, c) <= 0 or h < l or not all(
            math.isfinite(v) for v in (o, h, l, c)):
        return False
    return True


def load_bars_db(code: str, days: int = DEFAULT_DAYS,
                 db_path: str = "") -> dict:
    """从 stock_cache.db 读最近 days 根，按 adjust.k 转为展示价。"""
    full = normalize_code(code)
    db = db_path or DEFAULT_DB
    if not os.path.isfile(db):
        raise SystemExit(f"找不到数据库：{db}（用 --db 指定）")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        k = None
        try:
            row = con.execute("select k from adjust where code=?",
                              (full,)).fetchone()
            k = float(row[0]) if row and row[0] else None
        except sqlite3.Error:
            k = None
        name = ""
        try:
            row = con.execute("select name from stocks where code=?",
                              (full,)).fetchone()
            name = row[0] if row else ""
        except sqlite3.Error:
            pass
        rows = con.execute(
            "select date,open,high,low,close,vol from daily_bars "
            "where code=? order by date desc limit ?", (full, int(days))
        ).fetchall()
    finally:
        con.close()
    bars = []
    for r in reversed(rows):
        b = {"date": r["date"], "open": r["open"], "high": r["high"],
             "low": r["low"], "close": r["close"], "vol": r["vol"] or 0.0}
        if k and k > 0:
            for key in ("open", "high", "low", "close"):
                b[key] = b[key] * k
        if _bar_valid(b):
            bars.append(b)
    if len(bars) < 30:
        raise SystemExit(f"{full} 缓存日K不足（{len(bars)} 根 < 30），"
                         "先运行 GUI 分析/更新缓存")
    return {"code": full, "name": name, "bars": bars, "adjust": k,
            "db": db}


def load_bars_csv(path: str, days: int = DEFAULT_DAYS) -> dict:
    """CSV：date,open,high,low,close[,vol]；自动识别表头。"""
    if not os.path.isfile(path):
        raise SystemExit(f"找不到 CSV：{path}")
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        delim = "\t" if "\t" in sample and "," not in sample else ","
        reader = csv.reader(f, delimiter=delim)
        for row in reader:
            if not row:
                continue
            cells = [c.strip() for c in row]
            if not cells or not re.search(r"\d", cells[0]):
                continue
            if len(cells) < 5:
                continue
            try:
                date = cells[0][:10].replace("/", "-").replace(".", "-")
                vals = [float(c.replace(",", "")) for c in cells[1:6]]
            except ValueError:
                continue
            b = {"date": date, "open": vals[0], "high": vals[1],
                 "low": vals[2], "close": vals[3],
                 "vol": float(cells[6]) if len(cells) > 6 else 0.0}
            if _bar_valid(b):
                rows.append(b)
    rows = rows[-max(30, int(days)):]
    if len(rows) < 30:
        raise SystemExit(f"CSV 有效日K不足（{len(rows)} 根 < 30）")
    return {"code": os.path.basename(path), "name": "", "bars": rows,
            "adjust": None, "db": path}


# ======================== 数值工具 ========================

def _mean(vals):
    return sum(vals) / len(vals) if vals else 0.0


def _median(vals):
    return statistics.median(vals) if vals else 0.0


def _pct(vals, q):
    """线性插值分位数，q∈[0,1]。"""
    if not vals:
        return 0.0
    a = sorted(vals)
    if len(a) == 1:
        return a[0]
    pos = max(0.0, min(1.0, float(q))) * (len(a) - 1)
    lo = int(math.floor(pos))
    hi = min(len(a) - 1, lo + 1)
    return a[lo] + (a[hi] - a[lo]) * (pos - lo)


def _atr(bars, n=14):
    trs = []
    prev = None
    for b in bars:
        h, l = b["high"], b["low"]
        if prev is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - prev), abs(l - prev))
        trs.append(tr)
        prev = b["close"]
    if len(trs) < n:
        return _mean(trs) or 0.0
    # Wilder ATR
    atr = _mean(trs[:n])
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr


def _safe_round(v, nd=2):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return v


# ======================== 自动画线引擎 ========================

class AutoLineEngine:
    """多轮自动画线引擎。输入 bars（升序，显示价）→ ai-lines dict。"""

    def __init__(self, bars, rounds=DEFAULT_ROUNDS, verbose=False,
                 max_bars=480):
        bars = list(bars or [])
        if max_bars and len(bars) > int(max_bars):
            bars = bars[-int(max_bars):]
        self.bars = bars
        self.n = len(self.bars)
        if self.n < 30:
            raise ValueError("日K不足 30 根")
        self.rounds = max(1, int(rounds))
        self.verbose = bool(verbose)
        self.dates = [b["date"] for b in self.bars]
        self.highs = [float(b["high"]) for b in self.bars]
        self.lows = [float(b["low"]) for b in self.bars]
        self.closes = [float(b["close"]) for b in self.bars]
        self.vols = [float(b.get("vol") or 0) for b in self.bars]
        self.cur = self.closes[-1]
        self.atr = _atr(self.bars, 14) or (self.cur * 0.01)
        self.avg_vol = _mean([v for v in self.vols if v > 0]) or 1.0
        self.pivots = []
        self.round_log = []
        self._find_pivots()
        self._find_volume_nodes()

    # ---------- 基础工具 ----------

    def _rec(self, i):
        """近期权重：越靠近当前越大（0.55~1.0）。"""
        return 0.55 + 0.45 * (i / max(1, self.n - 1))

    def _vol_w(self, i):
        v = self.vols[i] if 0 <= i < self.n else 0
        if v <= 0:
            return 1.0
        return max(0.6, min(2.0, v / self.avg_vol))

    def _tol(self, mult=1.0):
        return max(0.004 * self.cur, 0.35 * self.atr) * mult

    def _line_val(self, c, i):
        a, b = c["a_idx"], c["b_idx"]
        if b == a:
            return c["a_price"]
        t = (i - a) / (b - a)
        return c["a_price"] + (c["b_price"] - c["a_price"]) * t

    # ---------- 枢轴 / 成交量节点 ----------

    def _find_pivots(self):
        raw = []
        for k in (2, 3, 5, 8, 13):
            if self.n <= 2 * k + 1:
                continue
            for i in range(k, self.n - k):
                win_h = self.highs[i - k:i + k + 1]
                win_l = self.lows[i - k:i + k + 1]
                # 高点：左侧不高于它，右侧严格低于它（避免平台重复）
                if (self.highs[i] >= max(win_h[:k + 1])
                        and self.highs[i] > max(win_h[k + 1:])):
                    prom = (self.highs[i] - min(win_l)) / self.atr
                    raw.append({"i": i, "price": self.highs[i],
                                "kind": "high", "scale": k,
                                "strength": max(0.2, prom)})
                if (self.lows[i] <= min(win_l[:k + 1])
                        and self.lows[i] < min(win_l[k + 1:])):
                    prom = (max(win_h) - self.lows[i]) / self.atr
                    raw.append({"i": i, "price": self.lows[i],
                                "kind": "low", "scale": k,
                                "strength": max(0.2, prom)})
        # 强度优先去重：相邻 2 根 / 0.45 ATR 内只保留最强
        raw.sort(key=lambda p: p["strength"], reverse=True)
        keep = []
        for p in raw:
            dup = False
            for q in keep:
                if (p["kind"] == q["kind"]
                        and abs(p["i"] - q["i"]) <= 2
                        and abs(p["price"] - q["price"]) <= 0.45 * self.atr):
                    dup = True
                    break
            if not dup:
                keep.append(p)
        self.pivots = sorted(keep, key=lambda p: p["i"])

    def _find_volume_nodes(self):
        """成交量价格分布，取前若干个高成交节点（辅助水平位）。"""
        self.volume_nodes = []
        if not self.vols:
            return
        width = max(0.35 * self.atr, 0.003 * self.cur)
        bins = {}
        for i, b in enumerate(self.bars):
            tp = (b["high"] + b["low"] + b["close"]) / 3.0
            key = int(round(tp / width))
            bins[key] = bins.get(key, 0.0) + (self.vols[i] or 0)
        if not bins:
            return
        top = sorted(bins.items(), key=lambda kv: kv[1], reverse=True)[:10]
        maxv = top[0][1] or 1.0
        self.volume_nodes = [
            {"price": k * width, "weight": v / maxv}
            for k, v in top if v > 0]

    # ---------- 水平位 ----------

    def _cluster_pivots(self, pts, tol):
        if not pts:
            return []
        pts = sorted(pts, key=lambda p: p["price"])
        out, cur = [], [pts[0]]
        for p in pts[1:]:
            if abs(p["price"] - cur[-1]["price"]) <= tol:
                cur.append(p)
            else:
                out.append(cur)
                cur = [p]
        out.append(cur)
        clusters = []
        for cl in out:
            wsum = sum(max(0.2, p["strength"]) * self._rec(p["i"]) for p in cl)
            if wsum <= 0:
                continue
            price = sum(p["price"] * max(0.2, p["strength"])
                        * self._rec(p["i"]) for p in cl) / wsum
            clusters.append({"price": price, "members": cl})
        return clusters

    def _score_level(self, price, side, tol):
        """支撑/压力位的触及/反弹/跌破评分（全窗口，近端权重更高）。"""
        look = self.n
        start = self.n - look
        bounce = breaks = 0.0
        touches = []
        vol_hit = 0.0
        for i in range(start, self.n):
            low, high, close = self.lows[i], self.highs[i], self.closes[i]
            near = (low <= price + tol and high >= price - tol)
            if not near:
                continue
            r = self._rec(i)
            vw = self._vol_w(i)
            if side == "support":
                if close >= price - 0.35 * tol:
                    bounce += r * vw
                    touches.append(i)
                    vol_hit += (self.vols[i] or 0) / self.avg_vol
                elif close < price - 0.8 * tol:
                    breaks += r * vw
            else:
                if close <= price + 0.35 * tol:
                    bounce += r * vw
                    touches.append(i)
                    vol_hit += (self.vols[i] or 0) / self.avg_vol
                elif close > price + 0.8 * tol:
                    breaks += r * vw
        # 连续触及合并为“波次”（同一段横盘不重复计分）
        clusters = 0
        last = -10
        for i in touches:
            if i - last > 3:
                clusters += 1
            last = i
        recent = self._rec(touches[-1]) if touches else 0.0
        vol_ratio = (vol_hit / len(touches)) if touches else 0.0
        score = (2.4 * bounce + 1.6 * clusters + 1.2 * recent
                 + 0.5 * min(3.0, vol_ratio) - 3.5 * breaks)
        return {"score": score, "bounces": bounce, "breaks": breaks,
                "clusters": clusters, "touches": touches,
                "recent": recent, "vol_ratio": vol_ratio}

    def _detect_levels(self, tol_mult=1.0):
        tol = self._tol(tol_mult)
        out = []
        for side, kind in (("support", "low"), ("resistance", "high")):
            pts = [p for p in self.pivots if p["kind"] == kind]
            for cl in self._cluster_pivots(pts, tol):
                price = cl["price"]
                st = self._score_level(price, side, tol)
                # 成交量节点额外加分（无枢轴但成交密集也算）
                for node in self.volume_nodes:
                    if abs(node["price"] - price) <= tol:
                        st["score"] += 1.2 * node["weight"]
                        break
                if st["score"] <= 0.2:
                    continue
                # 按当前位置重新定 side：跌破的支撑变压力，反之亦然
                real_side = "support" if price < self.cur else "resistance"
                if abs(price / self.cur - 1) < 0.002:
                    real_side = side
                out.append({"price": price, "side": real_side,
                            **st, "tol_mult": tol_mult})
        # 同价位去重（保留分数高者）
        out.sort(key=lambda x: x["score"], reverse=True)
        keep = []
        for x in out:
            if any(abs(x["price"] - y["price"]) <= 0.65 * tol
                   for y in keep):
                continue
            keep.append(x)
        return keep

    def _refine_levels(self, levels):
        """第二轮：用成员枢轴的成交量/强度重新加权，并再评分。"""
        out = []
        tol = self._tol(0.85)
        for lv in levels:
            price = lv["price"]
            st = self._score_level(price, lv["side"], tol)
            if st["score"] < lv.get("score", 0) * 0.9:
                # 精修后明显变差：保留原评分，避免伪精修
                st = lv
            merged = dict(lv)
            merged.update({k: v for k, v in st.items()
                           if k in ("score", "bounces", "breaks",
                                    "clusters", "touches", "recent",
                                    "vol_ratio")})
            merged["price"] = price
            merged["side"] = ("support" if price < self.cur
                              else "resistance")
            out.append(merged)
        out.sort(key=lambda x: x["score"], reverse=True)
        return out

    def _pick_levels(self):
        levels = self._detect_levels(1.25)
        self._log_round("levels R1", levels, levels[:6])
        if self.verbose:
            print(f"  round1 levels candidates: {len(levels)}")
        for r in range(1, self.rounds):
            mult = 1.0 if r == 1 else 0.8
            more = self._detect_levels(mult)
            levels = self._merge_levels(levels + more)
            levels = self._refine_levels(levels)
            self._log_round(f"levels R{r + 1}", levels, levels[:6])
            if self.verbose:
                print(f"  round{r + 1} levels: {len(levels)} "
                      f"(best={levels[0]['score']:.1f})" if levels else
                      f"  round{r + 1} levels: 0")
        # 支撑/压力各取 3 条：2 条最强 + 1 条最近（去重后不足再补强分）
        def pick_side(arr, n=3):
            # 离现价 0.8% 内的“贴身位”先剔除（与现价线重叠，信息量低）
            pool = [x for x in arr
                    if abs(x["price"] - self.cur) >= 0.008 * self.cur]
            if len(pool) >= 2:
                arr = pool
            # 同侧价位最小间距：避免 1% 内两三条重复线
            step = max(1.5 * self._tol(0.7), 0.013 * self.cur)
            chosen = []

            def add(x):
                if any(abs(x["price"] - y["price"]) < step for y in chosen):
                    return
                chosen.append(x)

            for x in sorted(arr, key=lambda y: y["score"],
                            reverse=True)[:4]:
                add(x)
            for x in sorted(arr, key=lambda y: abs(y["price"] - self.cur)):
                if len(chosen) >= n:
                    break
                add(x)
            for x in sorted(arr, key=lambda y: y["score"], reverse=True):
                if len(chosen) >= n:
                    break
                add(x)
            return chosen[:n]

        sup = [x for x in levels if x["side"] == "support"]
        res = [x for x in levels if x["side"] == "resistance"]
        # 近端优先：离现价 >35% 的远端位只在同侧不足 2 条时保留
        near_sup = [x for x in sup if x["price"] >= self.cur * 0.75]
        near_res = [x for x in res if x["price"] <= self.cur * 1.25]
        if len(near_sup) >= 2:
            sup = near_sup
        if len(near_res) >= 2:
            res = near_res
        chosen = pick_side(sup, 3) + pick_side(res, 3)
        chosen.sort(key=lambda x: x["price"])
        self._log_round("levels final", levels, chosen)
        return chosen[:_AI_MAX_LEVELS]

    def _merge_levels(self, levels):
        """合并候选：同价位保留得分高者。"""
        levels = sorted(levels, key=lambda x: x["score"], reverse=True)
        out = []
        # 0.9% 现价的硬下限：过近的价位对短线没有区分度
        tol = max(self._tol(0.7), 0.009 * self.cur)
        for x in levels:
            if any(abs(x["price"] - y["price"]) <= 0.75 * tol
                   for y in out):
                continue
            out.append(x)
        return out

    # ---------- 趋势线 / 通道 ----------

    def _score_trend(self, a, b, side, tol):
        span = b["i"] - a["i"]
        if span < 5:
            return None
        c = {"a_idx": a["i"], "b_idx": b["i"],
             "a_price": a["price"], "b_price": b["price"]}
        slope = (c["b_price"] - c["a_price"]) / span
        # 太平 → 交给水平位；太陡 → 不可靠
        if abs(c["b_price"] - c["a_price"]) < 0.012 * self.cur:
            return None
        if abs(c["b_price"] - c["a_price"]) > 0.7 * self.cur:
            return None
        touches = []
        violations = 0.0
        start = max(0, a["i"])
        for i in range(start, self.n):
            lv = self._line_val(c, i)
            if side == "support":
                # 影线触及/略微刺穿都算；收市明显跌破算违约
                if self.lows[i] <= lv + tol and self.highs[i] >= lv - tol:
                    if self.closes[i] >= lv - 0.8 * tol:
                        touches.append(i)
                    else:
                        violations += self._rec(i)
                elif self.closes[i] < lv - 1.5 * tol:
                    violations += self._rec(i) * 1.5
            else:
                if self.highs[i] >= lv - tol and self.lows[i] <= lv + tol:
                    if self.closes[i] <= lv + 0.8 * tol:
                        touches.append(i)
                    else:
                        violations += self._rec(i)
                elif self.closes[i] > lv + 1.5 * tol:
                    violations += self._rec(i) * 1.5
        # 端点始终计入触及
        if a["i"] not in touches:
            touches.insert(0, a["i"])
        if b["i"] not in touches:
            touches.append(b["i"])
        # 合并连续波次
        clusters = 0
        last = -10
        for i in touches:
            if i - last > 3:
                clusters += 1
            last = i
        recent = self._rec(touches[-1]) if touches else 0.0
        bounce_w = sum(self._rec(i) * self._vol_w(i) for i in touches)
        vol_w = _mean([self._vol_w(i) for i in touches]) if touches else 0
        score = (2.2 * bounce_w + 1.5 * clusters + 0.8 * span / 10.0
                 + 1.2 * recent + 0.3 * vol_w - 3.2 * violations)
        # 至少 3 个波次/2 个独立触及，违约率不能太高
        if clusters < 2 or len(touches) < 3:
            return None
        if violations > max(4.0, span * 0.12):
            return None
        c.update({"side": side, "score": score, "touches": touches,
                  "touch_clusters": clusters, "violations": violations,
                  "slope": slope, "span": span})
        return c

    def _refine_trend(self, c):
        """Theil-Sen 精修：用触及点重新拟合，保持原端点区间。"""
        pts = c.get("touches") or []
        pts = [i for i in pts if 0 <= i < self.n]
        if len(pts) < 4:
            return c
        # 取最近/均匀的至多 16 个触及点，避免计算量
        pts = pts[-16:]
        ys = self.lows if c["side"] == "support" else self.highs
        slopes = []
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                di = pts[j] - pts[i]
                if di > 0:
                    slopes.append((ys[pts[j]] - ys[pts[i]]) / di)
        if not slopes:
            return c
        slope = _median(slopes)
        intercepts = [ys[i] - slope * i for i in pts]
        intercept = _median(intercepts)
        a_idx, b_idx = c["a_idx"], c["b_idx"]
        out = dict(c)
        out["a_price"] = intercept + slope * a_idx
        out["b_price"] = intercept + slope * b_idx
        out["slope"] = slope
        # 重评分
        a = {"i": a_idx, "price": out["a_price"]}
        b = {"i": b_idx, "price": out["b_price"]}
        st = self._score_trend(a, b, c["side"], self._tol(0.85))
        if st and st["score"] >= c["score"] * 0.92:
            return st
        return c

    def _similar_trend(self, a, b):
        if a["side"] != b["side"]:
            return False
        # 重叠区间
        lo = max(a["a_idx"], b["a_idx"])
        hi = min(a["b_idx"], b["b_idx"])
        if hi <= lo:
            return False
        ov = (hi - lo) / max(1, min(a["span"], b["span"]))
        if ov < 0.45:
            return False
        tol = self._tol(1.0)
        mid = (lo + hi) // 2
        if abs(self._line_val(a, mid) - self._line_val(b, mid)) > 1.2 * tol:
            return False
        if abs(a["slope"] - b["slope"]) * max(1, hi - lo) > 1.2 * tol:
            return False
        return True

    def _suppress_trends(self, cands, max_each=2):
        cands = sorted(cands, key=lambda x: x["score"], reverse=True)
        out = []
        for c in cands:
            if sum(1 for x in out if x["side"] == c["side"]) >= max_each:
                continue
            if any(self._similar_trend(c, x) for x in out):
                continue
            out.append(c)
        out.sort(key=lambda x: (x["a_idx"], x["side"]))
        return out

    def _pick_trends(self):
        tol = self._tol(1.25)
        cands = []
        for side, kind in (("support", "low"), ("resistance", "high")):
            pts = [p for p in self.pivots if p["kind"] == kind]
            pts = sorted(pts, key=lambda p: p["i"])
            for i in range(len(pts)):
                for j in range(i + 1, len(pts)):
                    if pts[j]["i"] - pts[i]["i"] < 6:
                        continue
                    c = self._score_trend(pts[i], pts[j], side, tol)
                    if c:
                        cands.append(c)
        if self.verbose:
            print(f"  round1 trend candidates: {len(cands)}")
        cands = self._suppress_trends(cands, 4)
        self._log_round("trends R1", cands, cands)
        for r in range(1, self.rounds):
            # 收紧容差 + 精修
            more = []
            mt = 1.0 if r == 1 else 0.8
            tol2 = self._tol(mt)
            for side, kind in (("support", "low"), ("resistance", "high")):
                pts = [p for p in self.pivots if p["kind"] == kind]
                for i in range(len(pts)):
                    for j in range(i + 1, len(pts)):
                        if pts[j]["i"] - pts[i]["i"] < 6:
                            continue
                        c = self._score_trend(pts[i], pts[j], side, tol2)
                        if c:
                            more.append(c)
            cands = [self._refine_trend(c) for c in cands + more]
            cands = self._suppress_trends(cands, 3 if r == 1 else 2)
            self._log_round(f"trends R{r + 1}", cands, cands)
            if self.verbose:
                best = cands[0]["score"] if cands else 0
                print(f"  round{r + 1} trends: {len(cands)} "
                      f"(best={best:.1f})")
        # 最终每侧最多 1 条（避免两条近乎平行的重复趋势线）；
        # 过滤已远离现价且长期未触及的历史线
        out = self._suppress_trends(cands, 1)
        keep = []
        for c in out:
            last = c["touches"][-1] if c.get("touches") else c["b_idx"]
            line_now = self._line_val(c, self.n - 1)
            # 线已跑到现价另一侧（压力线在价下方/支撑线在价上方）→ 视为
            # 已破位，不再作为当前支撑压力趋势线，避免误导
            tol_now = 0.6 * self.atr
            if c["side"] == "resistance" and line_now < self.cur - tol_now:
                continue
            if c["side"] == "support" and line_now > self.cur + tol_now:
                continue
            near = abs(line_now - self.cur) <= max(0.05 * self.cur,
                                                    3.0 * self.atr)
            if last >= self.n - 30 or near:
                c["extend"] = bool(last >= self.n - 20 or near)
                c["last_touch"] = last
                keep.append(c)
        out = keep
        self._log_round("trends final", cands, out)
        return out

    def _build_channels(self, trends):
        """对趋势线用对侧极值分位生成平行通道线，每侧保留最佳 1 条。"""
        out = []
        tol = self._tol(0.9)
        for c in trends:
            a_idx, b_idx = c["a_idx"], c["b_idx"]
            if b_idx - a_idx < 6:
                continue
            dists = []
            for i in range(a_idx, self.n):
                lv = self._line_val(c, i)
                if c["side"] == "support":
                    dists.append(self.highs[i] - lv)
                else:
                    dists.append(lv - self.lows[i])
            dists = [d for d in dists if d > 0]
            if len(dists) < 5:
                continue
            best = None
            for q in (0.7, 0.8, 0.9):
                width = _pct(dists, q)
                if width <= 0.25 * self.atr:
                    continue
                if c["side"] == "support":
                    prices = (c["a_price"] + width,
                              c["b_price"] + width)
                    ys = self.highs
                    label = "通道上轨"
                else:
                    prices = (c["a_price"] - width,
                              c["b_price"] - width)
                    ys = self.lows
                    label = "通道下轨"
                touches = 0
                for i in range(a_idx, self.n):
                    lv = self._line_val(
                        {"a_idx": a_idx, "b_idx": b_idx,
                         "a_price": prices[0], "b_price": prices[1]}, i)
                    if abs(ys[i] - lv) <= tol:
                        touches += 1
                if touches >= 2 and (best is None or touches > best[0]):
                    best = (touches, q, width, prices, label)
            if not best:
                continue
            touches, q, width, prices, label = best
            out.append({
                "a_idx": a_idx, "b_idx": b_idx,
                "a_price": prices[0], "b_price": prices[1],
                "side": c["side"], "score": c["score"] * 0.8
                + touches * 1.5, "touches": touches,
                "label": label, "kind": "channel",
                "quantile": q, "extend": bool(c.get("extend")),
            })
        out.sort(key=lambda x: x["score"], reverse=True)
        keep = []
        seen = set()
        for ch in out:
            if ch["side"] in seen:
                continue
            seen.add(ch["side"])
            keep.append(ch)
        return keep[:2]

    # ---------- 区间 ----------

    def _detect_zones(self, levels):
        zones = []
        tol = self._tol(0.8)
        sups = [x for x in levels if x["side"] == "support"
                and x["price"] <= self.cur * 1.02]
        ress = [x for x in levels if x["side"] == "resistance"
                and x["price"] >= self.cur * 0.98]
        # 1) 支撑/压力组成的箱体
        for s in sups:
            for r in ress:
                lo, hi = s["price"], r["price"]
                if hi <= lo or hi / lo - 1 > 0.22:
                    continue
                start = max(0, self.n - 60)
                inside = touches_lo = touches_hi = 0
                for i in range(start, self.n):
                    if self.lows[i] >= lo - tol and self.highs[i] <= hi + tol:
                        inside += 1
                    if abs(self.lows[i] - lo) <= tol:
                        touches_lo += 1
                    if abs(self.highs[i] - hi) <= tol:
                        touches_hi += 1
                score = (inside / max(1, self.n - start) * 3.0
                         + min(3, touches_lo) + min(3, touches_hi)
                         + (s["score"] + r["score"]) * 0.08)
                if inside >= 8 and touches_lo >= 1 and touches_hi >= 1:
                    zones.append({
                        "a_idx": start, "b_idx": self.n - 1,
                        "lo": lo, "hi": hi, "score": score,
                        "label": "震荡箱体", "kind": "zone",
                    })
        # 2) 纯价格窗口（无明确边界时兜底）
        best_win = None
        for win in (20, 30, 40, 60):
            if self.n < win + 2:
                continue
            lo = min(self.lows[-win:])
            hi = max(self.highs[-win:])
            rng = (hi - lo) / max(1e-9, self.cur)
            if rng > 0.09:
                continue
            score = (1.0 - rng / 0.09) * 3.0 + win / 30.0
            if best_win is None or score > best_win[0]:
                best_win = (score, win, lo, hi)
        if best_win:
            score, win, lo, hi = best_win
            zones.append({
                "a_idx": self.n - win, "b_idx": self.n - 1,
                "lo": lo, "hi": hi, "score": score,
                "label": "近期震荡", "kind": "zone",
            })
        zones.sort(key=lambda x: x["score"], reverse=True)
        out = []
        for z in zones:
            dup = False
            for y in out:
                ilo = max(z["lo"], y["lo"])
                ihi = min(z["hi"], y["hi"])
                if ihi <= ilo:
                    continue
                # 交集占较小箱体 >70% 视为同一区域（避免大小箱体套娃）
                contain = (ihi - ilo) / max(1e-9,
                                          min(z["hi"] - z["lo"],
                                              y["hi"] - y["lo"]))
                if contain > 0.35:
                    dup = True
                    break
            if not dup:
                out.append(z)
        return out[:_AI_MAX_ZONES]

    # ---------- 多轮汇总 ----------

    def _log_round(self, name, cands, chosen):
        self.round_log.append({
            "name": name, "candidates": len(cands), "chosen": len(chosen),
            "best": max((c.get("score", 0) for c in cands), default=0),
            "selected": [
                {k: (round(v, 2) if isinstance(v, float) else v)
                 for k, v in c.items() if k in
                 ("price", "score", "side", "label", "kind")}
                for c in chosen
            ],
        })

    def run(self):
        t0 = time.time()
        levels = self._pick_levels()
        trends = self._pick_trends()
        channels = self._build_channels(trends)
        zones = self._detect_zones(levels)
        self.elapsed = time.time() - t0
        return self._build_output(levels, trends, channels, zones)

    def _build_output(self, levels, trends, channels, zones):
        out_levels = []
        # 水平位标签：按距离现价由近到远编号
        for lv in levels:
            side = lv["side"]
            kind = "support" if side == "support" else "resistance"
            lab = "支撑" if kind == "support" else "压力"
            out_levels.append({
                "price": _safe_round(lv["price"], 2),
                "label": lab,
                "kind": kind,
            })
        # 同 side 由近及远编号
        for kind in ("support", "resistance"):
            arr = [x for x in out_levels if x["kind"] == kind]
            arr.sort(key=lambda x: abs(x["price"] - self.cur))
            for idx, x in enumerate(arr, 1):
                x["label"] = f"{x['label']}{idx}"
        out_segs = []
        for c in trends:
            direction = "上升" if c["slope"] > 0 else "下降"
            side_lab = "支撑" if c["side"] == "support" else "压力"
            lab = f"{direction}趋势·{side_lab}"
            out_segs.append({
                "a": {"date": self.dates[c["a_idx"]],
                      "price": _safe_round(c["a_price"], 2)},
                "b": {"date": self.dates[c["b_idx"]],
                      "price": _safe_round(c["b_price"], 2)},
                "label": lab, "kind": "trend",
                "extend": bool(c.get("extend", True)), "dash": False,
            })
        for c in channels:
            out_segs.append({
                "a": {"date": self.dates[c["a_idx"]],
                      "price": _safe_round(c["a_price"], 2)},
                "b": {"date": self.dates[c["b_idx"]],
                      "price": _safe_round(c["b_price"], 2)},
                "label": c["label"], "kind": "channel",
                "extend": bool(c.get("extend", True)), "dash": True,
            })
        out_zones = []
        for z in zones:
            out_zones.append({
                "a": {"date": self.dates[z["a_idx"]],
                      "price": _safe_round(z["lo"], 2)},
                "b": {"date": self.dates[z["b_idx"]],
                      "price": _safe_round(z["hi"], 2)},
                "label": z["label"], "kind": "zone",
            })
        return {
            "lines": out_levels[:_AI_MAX_LEVELS],
            "segs": out_segs[:_AI_MAX_SEGS],
            "notes": [],
            "zones": out_zones[:_AI_MAX_ZONES],
            "meta": {
                "code": "", "bars": self.n, "last_date": self.dates[-1],
                "close": _safe_round(self.cur, 2),
                "atr": _safe_round(self.atr, 3),
                "pivots": len(self.pivots), "rounds": self.rounds,
                "elapsed_sec": round(getattr(self, "elapsed", 0.0), 3),
                "round_log": self.round_log,
            },
        }


# ======================== 报告 / CLI ========================

def _report_text(info, out):
    meta = out.get("meta") or {}
    lines = [
        "# 自动画线报告",
        "",
        f"- 代码：{info.get('code')} {info.get('name') or ''}",
        f"- 数据：{meta.get('bars')} 根，末日 {meta.get('last_date')}，"
        f"收 {meta.get('close')}（ATR {meta.get('atr')}）",
        f"- 枢轴：{meta.get('pivots')} 个；轮数：{meta.get('rounds')}；"
        f"耗时 {meta.get('elapsed_sec')}s",
        "",
        "## 多轮优化日志",
    ]
    for r in meta.get("round_log") or []:
        lines.append(f"- {r['name']}：候选 {r['candidates']} → 选中 "
                     f"{r['chosen']}，最高分 {r['best']:.2f}")
    lines += ["", "## 水平位"]
    for x in out.get("lines") or []:
        lines.append(f"- {x['label']}（{x['kind']}）：{x['price']}")
    lines += ["", "## 趋势 / 通道"]
    for x in out.get("segs") or []:
        lines.append(f"- {x['label']}：{x['a']['date']} {x['a']['price']} → "
                     f"{x['b']['date']} {x['b']['price']}")
    lines += ["", "## 区间"]
    for x in out.get("zones") or []:
        lines.append(f"- {x['label']}：{x['a']['date']} {x['a']['price']} ~ "
                     f"{x['b']['date']} {x['b']['price']}")
    lines += ["", "> 纯脚本统计线，仅为技术参考，不构成投资建议。"]
    return "\n".join(lines) + "\n"


def auto_draw(bars, rounds=DEFAULT_ROUNDS, verbose=False, max_bars=480):
    """GUI 可直接调用的入口：bars 为升序 OHLCV dict 列表。"""
    eng = AutoLineEngine(bars, rounds=rounds, verbose=verbose,
                         max_bars=max_bars)
    return eng.run()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="纯脚本自动画线（支撑/压力/趋势/通道/区间，多轮优化）")
    ap.add_argument("code", nargs="?", help="股票代码，如 600000 / sh600000")
    ap.add_argument("--csv", help="改用 CSV（date,open,high,low,close,vol）")
    ap.add_argument("--db", default="", help="stock_cache.db 路径")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help="使用最近多少根日K（默认240）")
    ap.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                    help="多轮优化轮数（默认3）")
    ap.add_argument("--out", help="输出 JSON 文件（默认 stdout）")
    ap.add_argument("--report", help="输出 Markdown 报告")
    ap.add_argument("--verbose", action="store_true", help="打印每轮优化日志")
    args = ap.parse_args(argv)
    if args.csv:
        info = load_bars_csv(args.csv, args.days)
    elif args.code:
        info = load_bars_db(args.code, args.days, args.db)
    else:
        ap.error("请提供股票代码或 --csv")
    eng = AutoLineEngine(info["bars"], rounds=args.rounds,
                         verbose=args.verbose, max_bars=max(args.days, 60))
    out = eng.run()
    out["meta"]["code"] = info.get("code") or ""
    out["meta"]["name"] = info.get("name") or ""
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"已写入 {args.out}")
    else:
        print(text)
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)),
                    exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(_report_text(info, out))
        print(f"已写入 {args.report}")
    if args.verbose:
        print(f"多轮优化完成：{len(out['lines'])}水平线 / "
              f"{len(out['segs'])}趋势通道 / {len(out['zones'])}区间 "
              f"({out['meta']['elapsed_sec']}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
