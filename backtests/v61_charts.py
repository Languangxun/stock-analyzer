#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v61_charts.py - 标准回测报告图表（纯标准库 SVG，无需 matplotlib）

用途：`backtest_v61.py` 跑完自动出图，或 `--charts-only/--compare` 只出图：
  - boxplot      箱线图（中位/四分位/1.5IQR 须/离群点），用于相位年化、逐笔收益分布
  - grouped_bars 分组柱状图，用于总收益/均值的版本对比
  - draw_report  单个报告出图（各口径 × 档位）
  - draw_compare 多版本对比出图（同一 segment 的历史报告叠加）

数值约定：传入的收益均为小数（0.14=+14%），图表内换算成 % 展示。
产物为自包含 .svg（浏览器可看，也可嵌入 Markdown）。
"""
import glob
import html
import json
import math
import os

W, H = 1000, 560
L, R, T, B = 80, 26, 66, 104
PALETTE = ["#4da3ff", "#ffa94d", "#69db7c", "#e599f7", "#f06595",
           "#ffd43b", "#63e6be", "#a5d8ff", "#ffc9c9", "#b197fc",
           "#f783ac", "#8ce99a"]
UNI_NAME = {"all": "全A", "main": "主板", "etf": "ETF", "all_etf": "全A含ETF"}
TIERS = ("稳健", "均衡", "激进")


def _esc(s):
    return html.escape(str(s), quote=True)


def _nice_step(span, target=6):
    if span <= 0:
        return 1.0
    raw = span / target
    base = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * base + 1e-12:
            return m * base
    return 10 * base


def _quantiles(vals):
    s = sorted(vals)
    n = len(s)

    def q(p):
        k = (n - 1) * p
        lo = int(math.floor(k))
        hi = min(int(math.ceil(k)), n - 1)
        return s[lo] * (hi - k) + s[hi] * (k - lo)
    return q(0.25), q(0.5), q(0.75)


def _svg_open(f):
    f.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" '
            f'height="{H}" viewBox="0 0 {W} {H}" font-family="'
            'DejaVu Sans, Microsoft YaHei, sans-serif">\n')
    f.write('<rect width="100%" height="100%" fill="#ffffff"/>\n')


def _svg_close(f):
    f.write("</svg>\n")


def _yaxis(f, lo, hi, ymap, step, x0, x1, fmt=lambda v: f"{v:+.0f}%"):
    v = math.ceil(lo / step) * step
    while v <= hi + 1e-9:
        y = ymap(v)
        f.write(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" '
                'stroke="#e6e9ee" stroke-width="1"/>\n')
        f.write(f'<text x="{x0-8}" y="{y+4:.1f}" font-size="12" '
                f'fill="#5a6673" text-anchor="end">{_esc(fmt(v))}</text>\n')
        v += step
    y0 = ymap(0)
    if lo < 0 < hi:
        f.write(f'<line x1="{x0}" y1="{y0:.1f}" x2="{x1}" y2="{y0:.1f}" '
                'stroke="#9aa6b2" stroke-width="1.2"/>\n')


def _title(f, title, subtitle):
    f.write(f'<text x="{L}" y="30" font-size="19" font-weight="bold" '
            f'fill="#20262e">{_esc(title)}</text>\n')
    if subtitle:
        f.write(f'<text x="{W-R}" y="30" font-size="12" fill="#6b7684" '
                f'text-anchor="end">{_esc(subtitle)}</text>\n')


def boxplot(path, title, groups, subtitle="", ylabel="年化收益"):
    """groups = [(label, [收益小数...]), ...]；空组自动跳过。"""
    data = []
    for lab, vals, color in [(g[0], g[1], g[2] if len(g) > 2 else None)
                             for g in groups]:
        vs = [float(v) * 100 for v in (vals or []) if v is not None]
        if vs:
            data.append((lab, vs, color))
    if not data:
        return False
    allv = [v for _, vs, _ in data for v in vs]
    lo, hi = min(allv), max(allv)
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.10
    lo, hi = lo - pad, hi + pad
    step = _nice_step(hi - lo)
    ph = H - T - B
    pw = W - L - R

    def ymap(v):
        return T + (hi - v) / (hi - lo) * ph

    with open(path, "w", encoding="utf-8") as f:
        _svg_open(f)
        _title(f, title, subtitle)
        f.write(f'<rect x="{L}" y="{T}" width="{pw}" height="{ph}" '
                'fill="none" stroke="#c8d0d8"/>\n')
        _yaxis(f, lo, hi, ymap, step, L, W - R)
        f.write(f'<text x="22" y="{T+ph/2:.0f}" font-size="12" fill="#5a6673" '
                f'text-anchor="middle" transform="rotate(-90 22 '
                f'{T+ph/2:.0f})">{_esc(ylabel)}（%）</text>\n')
        n = len(data)
        slot = pw / n
        bw = min(64.0, slot * 0.52)
        for i, (lab, vs, color) in enumerate(data):
            color = color or PALETTE[i % len(PALETTE)]
            cx = L + slot * (i + 0.5)
            q1, med, q3 = _quantiles(vs)
            iqr = q3 - q1
            wlo = min(v for v in vs if v >= q1 - 1.5 * iqr)
            whi = max(v for v in vs if v <= q3 + 1.5 * iqr)
            fl = [v for v in vs if v < wlo or v > whi]
            f.write(f'<line x1="{cx:.1f}" y1="{ymap(wlo):.1f}" x2="{cx:.1f}" '
                    f'y2="{ymap(q1):.1f}" stroke="#5a6673" '
                    'stroke-width="1.2"/>\n')
            f.write(f'<line x1="{cx:.1f}" y1="{ymap(q3):.1f}" x2="{cx:.1f}" '
                    f'y2="{ymap(whi):.1f}" stroke="#5a6673" '
                    'stroke-width="1.2"/>\n')
            for wv in (wlo, whi):
                f.write(f'<line x1="{cx-bw*0.22:.1f}" y1="{ymap(wv):.1f}" '
                        f'x2="{cx+bw*0.22:.1f}" y2="{ymap(wv):.1f}" '
                        'stroke="#5a6673" stroke-width="1.2"/>\n')
            f.write(f'<rect x="{cx-bw/2:.1f}" y="{ymap(q3):.1f}" '
                    f'width="{bw:.1f}" height="{max(ymap(q1)-ymap(q3),1):.1f}" '
                    f'fill="{color}" fill-opacity="0.72" stroke="{color}"/>\n')
            f.write(f'<line x1="{cx-bw/2:.1f}" y1="{ymap(med):.1f}" '
                    f'x2="{cx+bw/2:.1f}" y2="{ymap(med):.1f}" '
                    'stroke="#111820" stroke-width="2.2"/>\n')
            show = fl if len(fl) <= 200 else fl[::max(1, len(fl) // 200)]
            for v in show:
                f.write(f'<circle cx="{cx:.1f}" cy="{ymap(v):.1f}" r="1.8" '
                        f'fill="{color}" fill-opacity="0.55"/>\n')
            f.write(f'<text x="{cx:.1f}" y="{ymap(whi)-8:.1f}" font-size="11" '
                    f'fill="#5a6673" text-anchor="middle">n={len(vs)}</text>\n')
            rot = -22 if (len(lab) > 6 or n > 6) else 0
            ty = H - B + 20
            tr = (f' transform="rotate({rot} {cx:.0f} {ty})"'
                  if rot else "")
            f.write(f'<text x="{cx:.1f}" y="{ty}" font-size="12.5" '
                    f'fill="#20262e" text-anchor="middle"{tr}>'
                    f'{_esc(lab)}</text>\n')
        f.write(f'<text x="{L}" y="{H-10}" font-size="11" fill="#8b95a1">'
                '箱=四分位 中横线=中位 须=1.5×IQR 点=离群</text>\n')
        _svg_close(f)
    return True


def grouped_bars(path, title, x_labels, series, subtitle="",
                 ylabel="总收益", value_fmt=lambda v: f"{v:+.0f}%"):
    """series = {名称: [与 x_labels 等长的数值(小数)]}。"""
    series = {k: v for k, v in series.items() if any(x is not None
                                                     for x in v)}
    if not series or not x_labels:
        return False
    allv = [x for v in series.values() for x in v if x is not None]
    lo, hi = min(allv + [0]), max(allv + [0])
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad
    step = _nice_step(hi - lo)
    ph, pw = H - T - B, W - L - R

    def ymap(v):
        return T + (hi - v) / (hi - lo) * ph

    with open(path, "w", encoding="utf-8") as f:
        _svg_open(f)
        _title(f, title, subtitle)
        f.write(f'<rect x="{L}" y="{T}" width="{pw}" height="{ph}" '
                'fill="none" stroke="#c8d0d8"/>\n')
        _yaxis(f, lo, hi, ymap, step, L, W - R, fmt=value_fmt)
        f.write(f'<text x="22" y="{T+ph/2:.0f}" font-size="12" fill="#5a6673" '
                f'text-anchor="middle" transform="rotate(-90 22 '
                f'{T+ph/2:.0f})">{_esc(ylabel)}（%）</text>\n')
        names = list(series)
        nx = len(x_labels)
        slot = pw / nx
        bw = min(46.0, slot * 0.72 / max(1, len(names)))
        base = ymap(0)
        for i, lab in enumerate(x_labels):
            x0 = L + slot * (i + 0.5) - bw * len(names) / 2
            for j, name in enumerate(names):
                v = series[name][i]
                if v is None:
                    continue
                color = PALETTE[j % len(PALETTE)]
                x = x0 + j * bw
                y = ymap(v * 100)
                f.write(f'<rect x="{x:.1f}" y="{min(y, base):.1f}" '
                        f'width="{bw*0.88:.1f}" height="{abs(y-base):.1f}" '
                        f'fill="{color}" fill-opacity="0.85"/>\n')
                f.write(f'<text x="{x+bw*0.44:.1f}" y="{min(y, base)-4:.1f}" '
                        f'font-size="10" fill="#3a4552" text-anchor="middle">'
                        f'{_esc(value_fmt(v*100))}</text>\n')
            f.write(f'<text x="{L+slot*(i+0.5):.1f}" y="{H-B+20}" '
                    f'font-size="12.5" fill="#20262e" text-anchor="middle">'
                    f'{_esc(lab)}</text>\n')
        for j, name in enumerate(names):
            color = PALETTE[j % len(PALETTE)]
            lx = L + 6 + j * 130
            f.write(f'<rect x="{lx}" y="{H-30}" width="11" height="11" '
                    f'fill="{color}" fill-opacity="0.85"/>\n')
            f.write(f'<text x="{lx+16}" y="{H-20}" font-size="12" '
                    f'fill="#3a4552">{_esc(name)}</text>\n')
        _svg_close(f)
    return True


def _subtitle(report):
    return (f"{report.get('label') or ''} · {report.get('segment', '')} · "
            f"数据截至 {report.get('data_end', '?')}").strip(" ·")


def draw_report(report, out_dir):
    """单报告：每口径 相位箱线 + 逐笔箱线，另加 总收益/逐笔均值 柱状图。"""
    os.makedirs(out_dir, exist_ok=True)
    made = []
    res = report.get("results") or {}
    for uni, rep in res.items():
        name = UNI_NAME.get(uni, uni)
        groups = [(t, (rep.get("tiers") or {}).get(t, {}).get("phase_anns"))
                  for t in TIERS]
        p = os.path.join(out_dir, f"phase_box_{uni}.svg")
        if boxplot(p, f"相位年化收益分布 · {name}",
                   groups, _subtitle(report), "相位年化收益"):
            made.append(p)
        groups = [(t, (rep.get("picks") or {}).get(t, {}).get("rets"))
                  for t in TIERS]
        p = os.path.join(out_dir, f"picks_box_{uni}.svg")
        if boxplot(p, f"逐笔荐股收益分布 · {name}",
                   groups, _subtitle(report), "单笔收益"):
            made.append(p)
    xlab = [UNI_NAME.get(u, u) for u in res]
    totals = {t: [(res[u].get("tiers") or {}).get(t, {}).get("total")
                  for u in res] for t in TIERS}
    p = os.path.join(out_dir, "total_bars.svg")
    if grouped_bars(p, "三档总收益对比（各口径）", xlab, totals,
                    _subtitle(report)):
        made.append(p)
    avgs = {t: [(res[u].get("picks") or {}).get(t, {}).get("avg_ret")
                for u in res] for t in TIERS}
    p = os.path.join(out_dir, "picks_avg_bars.svg")
    if grouped_bars(p, "逐笔平均收益对比（各口径）", xlab, avgs,
                    _subtitle(report)):
        made.append(p)
    return made


def draw_compare(reports, out_dir, segment="full"):
    """多版本对比：x=档位，颜色=版本（相位箱线 + 总收益/逐笔均值柱状）。"""
    reports = [r for r in reports if (r.get("segment") or "full") == segment]
    if len(reports) < 2:
        return []
    reports = sorted(reports, key=lambda r: (r.get("ts") or "",
                                             r.get("label") or ""))
    os.makedirs(out_dir, exist_ok=True)
    made = []
    unis = []
    for r in reports:
        for u in (r.get("results") or {}):
            if u not in unis:
                unis.append(u)
    for uni in unis:
        name = UNI_NAME.get(uni, uni)
        groups = []
        for ti, t in enumerate(TIERS):
            for ri, r in enumerate(reports):
                lab = r.get("label") or f"#{ri+1}"
                rep = (r.get("results") or {}).get(uni, {})
                groups.append((f"{t}·{lab}",
                               (rep.get("tiers") or {}).get(t, {})
                               .get("phase_anns"),
                               PALETTE[ri % len(PALETTE)]))
        p = os.path.join(out_dir, f"compare_phase_{uni}.svg")
        if boxplot(p, f"版本对比 · 相位年化收益 · {name}", groups,
                   f"segment={segment}", "相位年化收益"):
            made.append(p)
        xlab = TIERS
        totals = {(r.get("label") or f"#{i+1}"):
                  [(r.get("results") or {}).get(uni, {})
                   .get("tiers", {}).get(t, {}).get("total") for t in TIERS]
                  for i, r in enumerate(reports)}
        p = os.path.join(out_dir, f"compare_total_{uni}.svg")
        if grouped_bars(p, f"版本对比 · 三档总收益 · {name}", xlab, totals,
                        f"segment={segment}"):
            made.append(p)
        avgs = {(r.get("label") or f"#{i+1}"):
                [(r.get("results") or {}).get(uni, {})
                 .get("picks", {}).get(t, {}).get("avg_ret") for t in TIERS]
                for i, r in enumerate(reports)}
        p = os.path.join(out_dir, f"compare_picks_{uni}.svg")
        if grouped_bars(p, f"版本对比 · 逐笔平均收益 · {name}", xlab, avgs,
                        f"segment={segment}"):
            made.append(p)
    return made


def discover_reports(research_dir, segment="full"):
    """扫描 research/v61_report*.json，按 segment 过滤（label 去重，后者覆盖）。"""
    out = {}
    for p in sorted(glob.glob(os.path.join(research_dir,
                                           "v61_report*.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
        except Exception:
            continue
        if (r.get("segment") or "full") != segment:
            continue
        lab = r.get("label")
        if not lab:
            base = os.path.basename(p)[:-5]
            lab = base.replace("v61_report", "").strip("_") or "default"
        r["label"] = lab
        r["_path"] = p
        out[lab] = r
    return [out[k] for k in sorted(out)]
