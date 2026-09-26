#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v61_dashboard.py - 本地回测仪表盘（自包含 HTML，浏览器直接打开文件）

整合三类数据，全部内联在单个 HTML 里（file:// 可直接打开，无外部依赖/CDN）：
  1. research/backtest_v*/report.json —— 研究回测（四口径 × 三档）：
     组合净值曲线（相位平均）/ 主基准、总收益/年化/回撤/Sharpe/超额、相位年化分布、
     逐笔荐股收益分布、三基准对照；
  2. research/gui_backtests/*.json —— GUI「工具→信号胜率」导出的单股回测：
     全期/训练/验证指标 + IC/信号后收益 + 净值曲线；
  3. 产物文件清单（report/tables/charts，相对链接直接点开）。

图表为原生 Canvas 手绘（无第三方库）：折线（净值对比，支持对数轴/hover）、
分组柱状（指标/跨版本对比）、箱线（相位年化、逐笔收益）。

用法：
  python v61_dashboard.py                       # 生成 research/dashboard.html
  python v61_dashboard.py --out research/dashboard.html
  python v61_dashboard.py --max-runs 6          # 内嵌最近 N 次回测
"""
import argparse
import glob
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TABLE_FILES = ("tier_metrics.csv", "picks_metrics.csv", "phase_anns.csv",
               "picks_returns.csv", "benchmarks.csv", "equity_curves.csv")
CHART_FILES = ("phase_box_all.svg", "phase_box_main.svg", "phase_box_etf.svg",
               "phase_box_all_etf.svg", "picks_box_all.svg",
               "picks_box_main.svg", "picks_box_etf.svg",
               "picks_box_all_etf.svg", "total_bars.svg", "picks_avg_bars.svg")


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _slim_report(rep):
    """只保留仪表盘需要的字段，控制 HTML 体积。"""
    out = {k: rep.get(k) for k in
           ("version", "ts", "label", "segment", "data_end", "db_stats")}
    out["results"] = {}
    for u, r in (rep.get("results") or {}).items():
        rr = {"tiers": {}, "picks": {}}
        for t, m in (r.get("tiers") or {}).items():
            rr["tiers"][t] = m
        for t, s in (r.get("picks") or {}).items():
            rr["picks"][t] = s
        out["results"][u] = rr
    return out


def collect_runs(research_dir, max_runs=6):
    """收集回测：优先版本化目录，其次根目录 v61_report*.json（去重）。"""
    runs, seen = [], set()

    def _key(rep):
        return (rep.get("ts"), rep.get("segment"), rep.get("label"))

    for d in sorted(glob.glob(os.path.join(research_dir, "backtest_v*")),
                    reverse=True):
        rp = os.path.join(d, "report.json")
        if not os.path.isfile(rp):
            continue
        rep = _load_json(rp)
        if not rep:
            continue
        rep = _slim_report(rep)
        rep["_dir"] = os.path.basename(d)
        meta = _load_json(os.path.join(d, "run_meta.json"))
        if meta:
            rep["_meta"] = {k: meta.get(k) for k in
                            ("version", "ts", "segment", "label",
                             "universes", "data_end", "db_stats",
                             "elapsed_s", "python", "argv")}
        runs.append(rep)
        seen.add(_key(rep))
    for p in sorted(glob.glob(os.path.join(research_dir,
                                           "v61_report*.json"))):
        rep = _load_json(p)
        if not rep:
            continue
        if _key(rep) in seen:
            continue
        rep = _slim_report(rep)
        base = os.path.basename(p)[:-5]
        rep["_dir"] = ""
        rep["_file"] = base
        runs.append(rep)
        seen.add(_key(rep))
    runs.sort(key=lambda r: (r.get("ts") or ""), reverse=True)
    return runs[:max_runs]


def _slim_gui(g, cap=800):
    """GUI 单股记录瘦身：曲线降采样、去掉逐信号列表（网页不用）。"""
    dates = g.get("curve_dates") or []
    curve = g.get("curve") or []
    dds = g.get("drawdown") or []
    n = len(curve)
    if n > cap:
        idx = sorted(set(round(i * (n - 1) / (cap - 1))
                           for i in range(cap)))
        g["curve_dates"] = [dates[i] for i in idx]
        g["curve"] = [curve[i] for i in idx]
        if len(dds) == n:
            g["drawdown"] = [dds[i] for i in idx]
        si = g.get("split_i")
        if si:
            g["split_i"] = sum(1 for i in idx if i < si)
    g.pop("signals", None)
    return g


def collect_gui(research_dir, max_n=30):
    gdir = os.path.join(research_dir, "gui_backtests")
    out = []
    for p in sorted(glob.glob(os.path.join(gdir, "*.json")), reverse=True):
        g = _load_json(p)
        if not g or g.get("kind") != "gui_backtest":
            continue
        g["_file"] = os.path.basename(p)
        out.append(_slim_gui(g))
    return out[:max_n]


def build_dashboard(research_dir, out=None, max_runs=6):
    runs = collect_runs(research_dir, max_runs=max_runs)
    gui = collect_gui(research_dir)
    data = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "research_dir": os.path.abspath(research_dir),
        "runs": runs,
        "gui": gui,
        "tables": list(TABLE_FILES),
        "charts": list(CHART_FILES),
    }
    out = out or os.path.join(research_dir, "dashboard.html")
    html_txt = HTML.replace("__DATA__",
                            json.dumps(data, ensure_ascii=False,
                                       allow_nan=True))
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_txt)
    return out


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>stock-analyzer · 回测仪表盘</title>
<style>
:root{--bg:#0f1419;--panel:#171d24;--panel2:#1e2630;--fg:#d7dee6;
      --dim:#8b98a5;--line:#2a3440;--gold:#e8c14a;--blue:#4da3ff;
      --up:#ff5252;--down:#26c281;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.5 "Microsoft YaHei","Segoe UI",sans-serif}
header{padding:14px 20px 8px;border-bottom:1px solid var(--line)}
h1{font-size:19px;margin:0 0 6px;color:#fff}
h1 .v{color:var(--gold)}
.sub{color:var(--dim);font-size:12.5px}
nav{display:flex;gap:4px;padding:8px 16px 0;flex-wrap:wrap;
    border-bottom:1px solid var(--line)}
nav button{background:transparent;border:1px solid transparent;
  border-bottom:none;color:var(--dim);padding:7px 14px;cursor:pointer;
  font-size:13.5px;border-radius:6px 6px 0 0}
nav button.on{background:var(--panel);border-color:var(--line);color:#fff}
main{padding:14px 18px 40px}
.tab{display:none}.tab.on{display:block}
.ctl{display:flex;gap:14px;flex-wrap:wrap;align-items:center;
     background:var(--panel);border:1px solid var(--line);border-radius:8px;
     padding:10px 12px;margin-bottom:12px}
.ctl label{color:var(--dim);font-size:12.5px}
select,input[type=text]{background:var(--panel2);color:var(--fg);
  border:1px solid var(--line);border-radius:5px;padding:5px 8px}
select:focus{outline:none;border-color:var(--blue)}
canvas{width:100%;display:block;background:var(--panel);
  border:1px solid var(--line);border-radius:8px}
.hover{min-height:20px;color:var(--dim);font-size:12.5px;padding:6px 4px}
table{border-collapse:collapse;width:100%;margin:10px 0 18px;font-size:13px}
th,td{border:1px solid var(--line);padding:5px 8px;text-align:right;
      white-space:nowrap}
th{background:var(--panel2);color:#cfd8e2;position:sticky;top:0}
td:first-child,th:first-child{text-align:left}
tr:hover td{background:#1b232c}
.legend{display:flex;gap:16px;flex-wrap:wrap;padding:8px 2px;
        font-size:12.5px;color:var(--dim)}
.legend span b{display:inline-block;width:10px;height:10px;border-radius:2px;
  margin-right:5px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:4px 0 14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
      padding:10px 14px;min-width:150px}
.card .k{color:var(--dim);font-size:12px}
.card .v{font-size:19px;font-weight:600;margin-top:2px}
.note{color:var(--dim);font-size:12.5px;margin:8px 0}
.files a{color:var(--blue);text-decoration:none;margin-right:16px;
         display:inline-block;padding:2px 0}
.files a:hover{text-decoration:underline}
.warn{color:#ffb86b}
</style>
</head>
<body>
<header>
  <h1>stock-analyzer · 回测仪表盘 <span class="v" id="hver"></span></h1>
  <div class="sub" id="hsub">加载中…</div>
</header>
<nav>
  <button data-tab="curve" class="on">组合净值曲线</button>
  <button data-tab="metrics">指标对比</button>
  <button data-tab="dist">分布箱线</button>
  <button data-tab="gui">单股回测（GUI）</button>
  <button data-tab="files">明细表 / 文件</button>
</nav>
<main>
  <section id="tab-curve" class="tab on">
    <div class="ctl">
      <label>回测批次 <select id="c-run"></select></label>
      <label>口径 <select id="c-uni"></select></label>
      <span id="c-tiers"></span>
      <label><input type="checkbox" id="c-bench" checked> 主基准</label>
      <label><input type="checkbox" id="c-log"> 对数轴</label>
    </div>
    <div class="legend" id="c-legend"></div>
    <canvas id="cv-curve" style="height:420px"></canvas>
    <div class="hover" id="c-hover">鼠标移入查看每日净值</div>
    <div id="c-sum"></div>
  </section>
  <section id="tab-metrics" class="tab">
    <div class="ctl">
      <label>回测批次 <select id="m-run"></select></label>
      <label>指标 <select id="m-metric">
        <option value="total">总收益</option>
        <option value="ann">年化</option>
        <option value="mdd">最大回撤</option>
        <option value="sharpe">Sharpe</option>
        <option value="excess_total">超额(vs主基准)</option>
        <option value="winrate">交易胜率</option>
      </select></label>
    </div>
    <canvas id="cv-bars" style="height:380px"></canvas>
    <div class="note">横轴=口径（全A/主板/ETF/全A含ETF），柱=三档；对数轴不适用。</div>
    <div id="m-cross"></div>
  </section>
  <section id="tab-dist" class="tab">
    <div class="ctl">
      <label>回测批次 <select id="d-run"></select></label>
      <label>分布 <select id="d-kind">
        <option value="phase">相位年化（组合）</option>
        <option value="picks">逐笔荐股收益</option>
      </select></label>
      <label>口径 <select id="d-uni"></select></label>
    </div>
    <canvas id="cv-box" style="height:420px"></canvas>
    <div class="note">箱=四分位，横线=中位，须=1.5×IQR，点=离群；n=样本数。</div>
  </section>
  <section id="tab-gui" class="tab">
    <div class="ctl">
      <label>单股记录 <select id="g-sel"></select></label>
      <label><input type="checkbox" id="g-log"> 对数轴</label>
    </div>
    <div id="g-body"></div>
  </section>
  <section id="tab-files" class="tab">
    <div id="f-body"></div>
  </section>
</main>
<script>
const DATA = __DATA__;
const PALETTE=["#4da3ff","#ffa94d","#69db7c","#e599f7","#f06595",
               "#ffd43b","#63e6be","#a5d8ff","#ffc9c9","#b197fc"];
const UP="#ff5252", DOWN="#26c281", GOLD="#e8c14a", DIM="#8b98a5",
      GRID="#26303b", LINE="#2a3440", FG="#d7dee6", PANEL="#171d24";
const TIERS=["稳健","均衡","激进"];
const UNI_NAME={all:"全A",main:"沪深主板",etf:"ETF",all_etf:"全A含ETF"};
const $=(q)=>document.querySelector(q);
const $$=(q)=>Array.from(document.querySelectorAll(q));
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function pct(x,d,sign){if(x==null||!isFinite(x))return "-";
  const v=x*100; return (sign&&v>=0?"+":"")+v.toFixed(d==null?1:d)+"%";}
function num(x,d){if(x==null||!isFinite(x))return "-";
  return (+x).toFixed(d==null?2:d);}
function fit(cv,h){
  const dpr=window.devicePixelRatio||1, w=Math.max(320,cv.clientWidth);
  cv.width=Math.floor(w*dpr); cv.height=Math.floor(h*dpr);
  const ctx=cv.getContext("2d"); ctx.setTransform(dpr,0,0,dpr,0,0);
  return {ctx,W:w,H:h};
}
function niceStep(span,n){ if(!(span>0))return 1;
  const raw=span/(n||6), b=Math.pow(10,Math.floor(Math.log10(raw)));
  for(const m of [1,2,2.5,5,10]) if(raw<=m*b+1e-12) return m*b; return 10*b;}

/* ---------- 折线图（净值对比，支持 hover） ---------- */
function lineChart(cv,series,opt){
  opt=opt||{};
  const h=parseInt(cv.style.height)||400;
  const {ctx,W,H}=fit(cv,h);
  const L=64,R=18,T=16,B=34, ph=H-T-B, pw=W-L-R;
  ctx.clearRect(0,0,W,H);
  const dates=opt.dates||[];
  const xs=dates.map(d=>Date.parse(d));
  const x0=Math.min(...xs), x1=Math.max(...xs)||x0+1;
  let lo=Infinity,hi=-Infinity;
  series.forEach(s=>s.points.forEach(p=>{if(isFinite(p.y)){if(p.y<lo)lo=p.y;
    if(p.y>hi)hi=p.y;}}));
  if(!isFinite(lo)){lo=0.9;hi=1.1;}
  if(opt.log){lo=Math.max(lo,1e-3);}
  const pad=(hi-lo)*0.08||0.02; lo-=pad; hi+=pad;
  const ymap=v=>opt.log
    ? T+(Math.log(Math.max(v,1e-3))-Math.log(lo))/(Math.log(hi)-Math.log(lo))*ph
    : T+(hi-v)/(hi-lo)*ph;
  const xmap=t=>L+(t-x0)/((x1-x0)||1)*pw;
  // 网格
  ctx.font="11px Consolas,monospace"; ctx.textAlign="right";
  const step=niceStep(hi-lo,5);
  for(let v=Math.ceil(lo/step)*step; v<=hi+1e-9; v+=step){
    const y=ymap(v);
    ctx.strokeStyle=GRID; ctx.beginPath(); ctx.moveTo(L,y);
    ctx.lineTo(W-R,y); ctx.stroke();
    ctx.fillStyle=DIM; ctx.fillText(num(v,2),L-6,y+4);
  }
  if(!opt.log && lo<1 && hi>1){ const y=ymap(1);
    ctx.strokeStyle="#66707c"; ctx.beginPath(); ctx.moveTo(L,y);
    ctx.lineTo(W-R,y); ctx.stroke(); }
  // x 轴日期
  ctx.textAlign="center";
  const ticks=Math.min(6,dates.length);
  for(let i=0;i<ticks;i++){
    const t=x0+(x1-x0)*(i/(ticks-1||1)), x=xmap(t);
    const d=new Date(t), lab=d.toISOString().slice(0,10);
    ctx.fillStyle=DIM; ctx.fillText(lab,x,H-14);
    ctx.strokeStyle=GRID; ctx.beginPath(); ctx.moveTo(x,T);
    ctx.lineTo(x,H-B); ctx.stroke();
  }
  // 曲线
  series.forEach((s,si)=>{
    ctx.strokeStyle=s.color; ctx.lineWidth=2; ctx.beginPath();
    let started=false;
    s.points.forEach(p=>{
      if(!isFinite(p.y))return;
      const X=xmap(Date.parse(p.x)), Y=ymap(p.y);
      if(!started){ctx.moveTo(X,Y);started=true;}else ctx.lineTo(X,Y);
    });
    ctx.stroke();
  });
  // hover
  const hov=opt.hover;
  if(hov!=null && dates.length){
    const t=hov, x=xmap(t);
    ctx.strokeStyle="#ffffff55"; ctx.beginPath(); ctx.moveTo(x,T);
    ctx.lineTo(x,H-B); ctx.stroke();
    series.forEach(s=>{
      let best=null,bd=1e18;
      s.points.forEach(p=>{const d=Math.abs(Date.parse(p.x)-t);
        if(d<bd){bd=d;best=p;}});
      if(best&&isFinite(best.y)&&bd<6e8){
        ctx.fillStyle=s.color; ctx.beginPath();
        ctx.arc(xmap(Date.parse(best.x)),ymap(best.y),3.2,0,7); ctx.fill();
      }
    });
  }
  return {xmap,ymap,x0,x1,L,R,T,B,ph,pw};
}
function bindHover(cv,redraw,opt){
  cv.onmousemove=e=>{
    if(!opt.dates||!opt.dates.length)return;
    const r=cv.getBoundingClientRect(), x=e.clientX-r.left;
    const t=opt.x0+(x-opt.L)/(opt.pw||1)*(opt.x1-opt.x0);
    opt.hover=t; redraw();
  };
  cv.onmouseleave=()=>{opt.hover=null; redraw();};
}

/* ---------- 分组柱状图 ---------- */
function barChart(cv,labels,groups,opt){
  opt=opt||{};
  const h=parseInt(cv.style.height)||360;
  const {ctx,W,H}=fit(cv,h);
  const L=64,R=18,T=26,B=52, ph=H-T-B, pw=W-L-R;
  ctx.clearRect(0,0,W,H);
  let all=[]; groups.forEach(g=>g.values.forEach(v=>{
    if(v!=null&&isFinite(v))all.push(v);}));
  all.push(0);
  let lo=Math.min(...all), hi=Math.max(...all);
  const pad=(hi-lo)*0.12||0.1; lo-=pad; hi+=pad;
  const ymap=v=>T+(hi-v)/(hi-lo)*ph;
  const step=niceStep(hi-lo,5);
  ctx.font="11px Consolas,monospace";
  for(let v=Math.ceil(lo/step)*step; v<=hi+1e-9; v+=step){
    const y=ymap(v); ctx.strokeStyle=GRID; ctx.beginPath();
    ctx.moveTo(L,y); ctx.lineTo(W-R,y); ctx.stroke();
    ctx.fillStyle=DIM; ctx.textAlign="right"; ctx.fillText(num(v,2),L-6,y+4);
  }
  const y0=ymap(0); ctx.strokeStyle="#66707c"; ctx.beginPath();
  ctx.moveTo(L,y0); ctx.lineTo(W-R,y0); ctx.stroke();
  const slot=pw/Math.max(labels.length,1);
  const bw=Math.min(46,slot*0.72/Math.max(groups.length,1));
  labels.forEach((lab,i)=>{
    const x0=L+slot*(i+0.5)-bw*groups.length/2;
    groups.forEach((g,j)=>{
      const v=g.values[i];
      if(v==null||!isFinite(v))return;
      const x=x0+j*bw, y=ymap(v);
      ctx.fillStyle=g.color; ctx.globalAlpha=0.88;
      ctx.fillRect(x,Math.min(y,y0),bw*0.88,Math.abs(y-y0));
      ctx.globalAlpha=1;
      ctx.fillStyle="#cfd8e2"; ctx.textAlign="center"; ctx.font="10px Consolas";
      ctx.fillText(opt.fmt?opt.fmt(v):num(v,2),x+bw*0.44,
                   Math.min(y,y0)-4);
    });
    ctx.fillStyle=FG; ctx.font="12.5px Microsoft YaHei"; ctx.textAlign="center";
    ctx.fillText(lab,L+slot*(i+0.5),H-30);
  });
  let lx=L+4;
  groups.forEach((g,j)=>{
    ctx.fillStyle=g.color; ctx.fillRect(lx,H-20,10,10);
    ctx.fillStyle="#cfd8e2"; ctx.textAlign="left";
    ctx.font="12px Microsoft YaHei";
    ctx.fillText(g.name,lx+14,H-11);
    lx+=Math.max(90,ctx.measureText(g.name).width+36);
  });
}

/* ---------- 箱线图 ---------- */
function quant(vals,p){
  const s=vals.slice().sort((a,b)=>a-b), n=s.length;
  if(!n)return NaN;
  const k=(n-1)*p, lo=Math.floor(k), hi=Math.min(Math.ceil(k),n-1);
  return s[lo]*(hi-k)+s[hi]*(k-lo);
}
function boxChart(cv,groups,opt){
  opt=opt||{};
  const h=parseInt(cv.style.height)||400;
  const {ctx,W,H}=fit(cv,h);
  const L=64,R=18,T=22,B=52, ph=H-T-B, pw=W-L-R;
  ctx.clearRect(0,0,W,H);
  const data=groups.filter(g=>g.values&&g.values.length)
    .map(g=>({name:g.name,color:g.color,
      v:g.values.filter(x=>x!=null&&isFinite(x))}))
    .filter(g=>g.v.length);
  if(!data.length){ctx.fillStyle=DIM;ctx.textAlign="center";
    ctx.fillText("无数据",W/2,H/2);return;}
  const all=data.flatMap(g=>g.v);
  let lo=Math.min(...all),hi=Math.max(...all);
  if(lo===hi){lo-=0.01;hi+=0.01;}
  const pad=(hi-lo)*0.10; lo-=pad; hi+=pad;
  const ymap=v=>T+(hi-v)/(hi-lo)*ph;
  const step=niceStep(hi-lo,5);
  ctx.font="11px Consolas,monospace";
  for(let v=Math.ceil(lo/step)*step; v<=hi+1e-9; v+=step){
    const y=ymap(v); ctx.strokeStyle=GRID; ctx.beginPath();
    ctx.moveTo(L,y); ctx.lineTo(W-R,y); ctx.stroke();
    ctx.fillStyle=DIM; ctx.textAlign="right"; ctx.fillText(num(v,2),L-6,y+4);
  }
  if(lo<0&&hi>0){const y=ymap(0);ctx.strokeStyle="#66707c";ctx.beginPath();
    ctx.moveTo(L,y);ctx.lineTo(W-R,y);ctx.stroke();}
  const slot=pw/data.length, bw=Math.min(64,slot*0.5);
  data.forEach((g,i)=>{
    const cx=L+slot*(i+0.5), s=g.v.slice().sort((a,b)=>a-b);
    const q1=quant(s,.25), med=quant(s,.5), q3=quant(s,.75), iqr=q3-q1;
    const wlo=Math.min(...s.filter(v=>v>=q1-1.5*iqr));
    const whi=Math.max(...s.filter(v=>v<=q3+1.5*iqr));
    const fl=s.filter(v=>v<wlo||v>whi);
    ctx.strokeStyle="#66707c"; ctx.lineWidth=1.2;
    ctx.beginPath(); ctx.moveTo(cx,ymap(wlo)); ctx.lineTo(cx,ymap(q1)); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(cx,ymap(q3)); ctx.lineTo(cx,ymap(whi)); ctx.stroke();
    [wlo,whi].forEach(v=>{ctx.beginPath();
      ctx.moveTo(cx-bw*.22,ymap(v)); ctx.lineTo(cx+bw*.22,ymap(v)); ctx.stroke();});
    ctx.fillStyle=g.color; ctx.globalAlpha=0.72;
    ctx.fillRect(cx-bw/2,ymap(q3),bw,Math.max(ymap(q1)-ymap(q3),1));
    ctx.globalAlpha=1; ctx.strokeStyle=g.color;
    ctx.strokeRect(cx-bw/2,ymap(q3),bw,Math.max(ymap(q1)-ymap(q3),1));
    ctx.strokeStyle="#10161c"; ctx.lineWidth=2.2;
    ctx.beginPath(); ctx.moveTo(cx-bw/2,ymap(med));
    ctx.lineTo(cx+bw/2,ymap(med)); ctx.stroke();
    ctx.lineWidth=1;
    const show=fl.length>200?fl.filter((_,k)=>k%Math.ceil(fl.length/200)===0):fl;
    show.forEach(v=>{ctx.fillStyle=g.color;ctx.globalAlpha=.5;
      ctx.beginPath();ctx.arc(cx,ymap(v),1.8,0,7);ctx.fill();ctx.globalAlpha=1;});
    ctx.fillStyle="#cfd8e2"; ctx.font="11px Consolas"; ctx.textAlign="center";
    ctx.fillText("n="+s.length,cx,ymap(whi)-7);
    ctx.fillStyle=FG; ctx.font="12.5px Microsoft YaHei";
    ctx.fillText(g.name,cx,H-30);
  });
}

/* ---------- 数据访问 ---------- */
const R_={index:-1};
function curRun(){return DATA.runs[R_.index];}
function tierOf(run,u,t){return (((run.results||{})[u]||{}).tiers||{})[t];}
function picksOf(run,u,t){return (((run.results||{})[u]||{}).picks||{})[t];}
function curvePoints(m){
  const ds=m.curve_dates||[], vs=m.curve||[];
  return ds.map((d,i)=>({x:d,y:vs[i]}));
}
function benchPoints(m){
  const ds=m.bench_curve_dates||m.curve_dates||[], vs=m.bench_curve||[];
  return ds.map((d,i)=>({x:d,y:vs[i]}));
}
function runLabel(r){
  return (r.version?"v"+r.version+" · ":"")+(r.segment||"")+
    (r.label?" · "+r.label:"")+" · "+(r.ts||"");
}

/* ---------- 各页渲染 ---------- */
function renderHeader(){
  const r=DATA.runs[0];
  $("#hver").textContent = r?("v"+(r.version||"?")):"";
  let s="生成 "+DATA.generated+" | 共 "+DATA.runs.length+" 个回测批次"+
        " | "+DATA.gui.length+" 条单股回测记录";
  if(r){ const db=r.db_stats||{};
    s+=" | 最新批次数据截至 "+(r.data_end||"?")+
       (db.codes?("（库内 "+db.codes+" 只 / "+
        (db.bars||0).toLocaleString()+" 根）"):""); }
  $("#hsub").textContent=s;
}
function fillRunSelect(sel){
  sel.innerHTML=DATA.runs.map((r,i)=>
    `<option value="${i}">${esc(runLabel(r))}</option>`).join("");
  sel.value=String(R_.index);
}
function selectedTiers(){
  return $$("#c-tiers input:checked").map(x=>x.value);
}
function renderCurve(){
  const run=curRun(); if(!run)return;
  const u=$("#c-uni").value, tiers=selectedTiers();
  const series=[]; const dates=[];
  tiers.forEach((t,i)=>{
    const m=tierOf(run,u,t); if(!m)return;
    const pts=curvePoints(m);
    if(!dates.length)pts.forEach(p=>dates.push(p.x));
    series.push({name:t,color:PALETTE[i%PALETTE.length],points:pts});
  });
  if($("#c-bench").checked && tiers.length){
    const m=tierOf(run,u,tiers[0]);
    if(m && (m.bench_curve||[]).length)
      series.push({name:"基准 "+(m.benchmark||""),
        color:"#8b98a5",points:benchPoints(m)});
  }
  const opt={dates:dates,log:$("#c-log").checked,hover:null};
  const cv=$("#cv-curve");
  const st=lineChart(cv,series,opt);
  Object.assign(opt,st);
  const redraw=()=>{const st2=lineChart(cv,series,opt);
    Object.assign(opt,st2);};
  bindHover(cv,redraw,opt);
  const hov=$("#c-hover");
  cv.onmousemove=e=>{
    const r=cv.getBoundingClientRect(), x=e.clientX-r.left;
    const t=opt.x0+(x-opt.L)/(opt.pw||1)*(opt.x1-opt.x0);
    let best=null,bd=1e18;
    series.forEach(s=>s.points.forEach(p=>{const d=Math.abs(Date.parse(p.x)-t);
      if(d<bd){bd=d;best=p;}}));
    if(best&&bd<6e8){
      const parts=series.map(s=>{
        let b=null,b2=1e18; s.points.forEach(p=>{const d=Math.abs(Date.parse(p.x)-t);
          if(d<b2){b2=d;b=p;}});
        return `<span style="color:${s.color}">■</span> ${esc(s.name)} `+
               (b&&isFinite(b.y)?b.y.toFixed(4):"-");
      });
      hov.innerHTML=esc(best.x.slice(0,10))+" | "+parts.join("  ");
    }
    opt.hover=t; redraw();
  };
  // 汇总卡片
  const m0=tierOf(run,u,tiers[0]+"");
  let cards="";
  tiers.forEach(t=>{const m=tierOf(run,u,t); if(!m)return;
    cards+=`<div class="card"><div class="k">${esc(t)} 总收益 / 年化 / 回撤</div>
      <div class="v">${pct(m.total,1,true)} / ${pct(m.ann,1,true)} / ${pct(m.mdd,1)}</div></div>`;});
  $("#c-legend").innerHTML=series.map(s=>
    `<span><b style="background:${s.color}"></b>${esc(s.name)}</span>`).join("");
  $("#c-sum").innerHTML=cards?('<div class="cards">'+cards+'</div>'):"";
}
function renderMetrics(){
  const run=curRun(); if(!run)return;
  const metric=$("#m-metric").value;
  const unis=Object.keys(run.results||{});
  const groups=TIERS.map((t,i)=>({name:t,color:PALETTE[i%PALETTE.length],
    values:unis.map(u=>{const m=tierOf(run,u,t);
      return m?m[metric]:null;})}));
  barChart($("#cv-bars"),unis.map(u=>UNI_NAME[u]||u),groups,
    {fmt:v=>metric==="sharpe"?num(v,2):pct(v,1,true)});
  // 跨批次对比
  if(DATA.runs.length>1){
    let h='<div class="note">跨批次对比（同区间/口径，选中批次为主）：</div>'+
      '<table><tr><th>批次</th><th>口径</th><th>档位</th>'+
      '<th>总收益</th><th>年化</th><th>回撤</th><th>Sharpe</th></tr>';
    DATA.runs.slice(0,6).forEach((r,ri)=>{
      unis.forEach(u=>TIERS.forEach(t2=>{
        const m=tierOf(r,u,t2); if(!m)return;
        h+=`<tr><td>${esc(runLabel(r))}</td><td>${esc(UNI_NAME[u]||u)}</td>`+
           `<td>${esc(t2)}</td><td>${pct(m.total,1,true)}</td>`+
           `<td>${pct(m.ann,1,true)}</td><td>${pct(m.mdd,1)}</td>`+
           `<td>${num(m.sharpe,2)}</td></tr>`;
      }));
    });
    $("#m-cross").innerHTML=h+"</table>";
  } else $("#m-cross").innerHTML="";
}
function renderDist(){
  const run=curRun(); if(!run)return;
  const kind=$("#d-kind").value, u=$("#d-uni").value;
  const groups=TIERS.map((t,i)=>{
    let vals=[];
    if(kind==="phase"){const m=tierOf(run,u,t); vals=(m&&m.phase_anns)||[];}
    else{const s=picksOf(run,u,t); vals=(s&&s.rets)||[];}
    return {name:t,color:PALETTE[i%PALETTE.length],values:vals};
  });
  boxChart($("#cv-box"),groups,{});
}
function renderGui(){
  const sel=$("#g-sel");
  if(!DATA.gui.length){
    $("#g-body").innerHTML='<div class="warn">暂无单股回测记录：在 GUI '+
      '「工具→信号胜率→导出回测」导出一次即可（会自动留档到 '+
      'research/gui_backtests/）。</div>';
    sel.innerHTML=""; return;
  }
  if(sel.options.length!==DATA.gui.length){
    sel.innerHTML=DATA.gui.map((g,i)=>
      `<option value="${i}">${esc(g.code)} ${esc(g.name||"")} · `+
      `${esc((g.strategy||{}).label||"")} · ${esc(g.ts||"")}</option>`).join("");
  }
  const g=DATA.gui[+sel.value||0]; if(!g)return;
  const dates=g.curve_dates||[], curve=g.curve||[];
  const si=g.split_i||dates.length;
  const f=g.full||{}, tr=g.train||{}, va=g.val||{};
  const ic1=g.ic1||[null,0], ic5=g.ic5||[null,0];
  const fwd=g.fwd||{};
  function mrow(name,m){
    if(!m||m.winrate==null)return `<tr><td>${name}</td><td colspan="7">无交易</td></tr>`;
    return `<tr><td>${name}</td><td>${m.trades}</td><td>${pct(m.winrate)}</td>`+
      `<td>${pct(m.total,1,true)}</td><td>${pct(m.ann,1,true)}</td>`+
      `<td>${pct(m.mdd,1)}</td><td>${num(m.profit_loss,2)}</td>`+
      `<td>${pct(m.avg_win,1,true)} / ${pct(m.avg_loss,1,true)}</td></tr>`;
  }
  function frow(typ){
    const rec=fwd[typ]||{};
    return [1,5].map(h=>{const r=rec[String(h)]||rec[h];
      if(!r||r[1]==null)return "T+"+h+" -";
      return "T+"+h+" 均值"+pct(r[1],2,true)+" 上涨"+pct(r[2],0)+"(n="+r[0]+")";
    }).join("　");
  }
  const strat=g.strategy||{};
  $("#g-body").innerHTML=
    `<div class="note">${esc(g.code)} ${esc(g.name||"")} · 策略 `+
    `<b>${esc(strat.label||"")}</b> · 参数 <code>${esc(JSON.stringify(strat.params||{}))}</code></div>`+
    `<div class="note">区间 ${esc((g.range||[])[0]||"")} ~ ${esc((g.range||[])[1]||"")}`+
    ` · 成交口径 ${g.exec==="open"?"次日开盘":"次日收盘"} · 训练/验证 = 前75% / 后25%</div>`+
    `<canvas id="cv-gcurve" style="height:360px"></canvas>`+
    `<div class="note">仅显示训练集净值（验证集只做指标检验）</div>`+
    `<table><tr><th>区间</th><th>交易</th><th>胜率</th><th>总收益</th>`+
    `<th>年化</th><th>回撤</th><th>盈亏比</th><th>均盈/均亏</th></tr>`+
    mrow("全期",f)+mrow("训练集",tr)+mrow("验证集",va)+`</table>`+
    `<div class="note">IC(T+1)=${num(ic1[0],3)} (n=${ic1[1]})　`+
    `IC(T+5)=${num(ic5[0],3)} (n=${ic5[1]})</div>`+
    `<div class="note">买入信号后：${frow("BUY")}<br>卖出信号后：${frow("SELL")}</div>`;
  const cv=$("#cv-gcurve");
  const mkSeries=()=>[{name:"训练集净值",color:GOLD,
    points:dates.slice(0,si).map((d,i)=>({x:d,y:curve[i]}))}];
  const opt2={dates:dates.slice(0,si),log:$("#g-log").checked,hover:null};
  Object.assign(opt2,lineChart(cv,mkSeries(),opt2));
  bindHover(cv,()=>{Object.assign(opt2,lineChart(cv,mkSeries(),opt2));},opt2);
}
function renderFiles(){
  const run=curRun(); if(!run)return;
  let h="";
  h+='<div class="note">当前批次：'+esc(runLabel(run))+
     (run._dir?(' · 目录 <code>research/'+esc(run._dir)+'/</code>'):'')+
     (run._file?(' · 文件 <code>research/'+esc(run._file)+'.json</code>'):'')+
     '</div>';
  h+='<div class="files">';
  if(run._dir){
    const d=run._dir+"/";
    h+=`<a href="${esc(d)}report.json" target="_blank">report.json</a>`;
    h+=`<a href="${esc(d)}report.md" target="_blank">report.md</a>`;
    h+=`<a href="${esc(d)}run_meta.json" target="_blank">run_meta.json</a>`;
    DATA.tables.forEach(t=>h+=`<a href="${esc(d)}tables/${esc(t)}" target="_blank">tables/${esc(t)}</a>`);
    DATA.charts.forEach(t=>h+=`<a href="${esc(d)}charts/${esc(t)}" target="_blank">charts/${esc(t)}</a>`);
  } else if(run._file){
    h+=`<a href="${esc(run._file)}.json" target="_blank">${esc(run._file)}.json</a>`;
    h+=`<a href="${esc(run._file)}.md" target="_blank">${esc(run._file)}.md</a>`;
  }
  h+='</div>';
  // 指标总表
  const unis=Object.keys(run.results||{});
  h+='<h3>组合指标（相位平均，含全部费用）</h3><table><tr><th>口径</th>'+
     '<th>档位</th><th>区间</th><th>总收益</th><th>年化</th><th>回撤</th>'+
     '<th>Sharpe</th><th>交易</th><th>主基准</th><th>基准年化</th>'+
     '<th>超额</th><th>相位年化区间</th></tr>';
  unis.forEach(u=>TIERS.forEach(t=>{const m=tierOf(run,u,t); if(!m)return;
    const b=m.bench||{};
    h+=`<tr><td>${esc(UNI_NAME[u]||u)}</td><td>${esc(t)}</td>`+
       `<td>${esc((m.range||[])[0]||"")}~${esc((m.range||[])[1]||"")}</td>`+
       `<td>${pct(m.total,1,true)}</td><td>${pct(m.ann,1,true)}</td>`+
       `<td>${pct(m.mdd,1)}</td><td>${num(m.sharpe,2)}</td>`+
       `<td>${m.trades}</td><td>${esc(m.benchmark||"")}</td>`+
       `<td>${pct(b.ann,1,true)}</td><td>${pct(m.excess_total,1,true)}</td>`+
       `<td>${pct(m.phase_ann_min,1,true)} ~ ${pct(m.phase_ann_max,1,true)}</td></tr>`;
  }));
  h+='</table>';
  h+='<h3>荐股逐笔指标</h3><table><tr><th>口径</th><th>档位</th><th>笔数</th>'+
     '<th>平均</th><th>中位</th><th>胜率</th><th>盈亏比</th><th>PF</th>'+
     '<th>均持有</th><th>最好</th><th>最差</th><th>&gt;50%右尾</th>'+
     '<th>退出(调仓/闸门/退市)</th></tr>';
  unis.forEach(u=>TIERS.forEach(t=>{const s=picksOf(run,u,t);
    if(!s||!s.n){h+=`<tr><td>${esc(UNI_NAME[u]||u)}</td><td>${esc(t)}</td>`+
      `<td colspan="11">0（闸门长期关闭/无信号）</td></tr>`;return;}
    const r=s.by_reason||{};
    h+=`<tr><td>${esc(UNI_NAME[u]||u)}</td><td>${esc(t)}</td><td>${s.n}</td>`+
       `<td>${pct(s.avg_ret,2,true)}</td><td>${pct(s.med_ret,2,true)}</td>`+
       `<td>${pct(s.winrate)}</td><td>${num(s.payoff,2)}</td><td>${num(s.pf,2)}</td>`+
       `<td>${num(s.avg_hold,1)}</td><td>${pct(s.best,1,true)}</td>`+
       `<td>${pct(s.worst,1,true)}</td><td>${pct(s.tail50,1)}</td>`+
       `<td>${r.target||0}/${r.gate||0}/${r.delist||0}</td></tr>`;
  }));
  h+='</table>';
  $("#f-body").innerHTML=h;
}

/* ---------- 初始化 ---------- */
function showTab(id){
  $$("nav button").forEach(b=>b.classList.toggle("on",b.dataset.tab===id));
  $$(".tab").forEach(s=>s.classList.toggle("on",s.id==="tab-"+id));
  if(id==="curve")renderCurve();
  if(id==="metrics")renderMetrics();
  if(id==="dist")renderDist();
  if(id==="gui")renderGui();
  if(id==="files")renderFiles();
}
function init(){
  renderHeader();
  if(!DATA.runs.length){$("#hsub").textContent="未发现回测批次（先运行 backtests/backtest_v61.py）";return;}
  R_.index=0;
  fillRunSelect($("#c-run")); fillRunSelect($("#m-run")); fillRunSelect($("#d-run"));
  const unis=Object.keys(curRun().results||{});
  ["#c-uni","#d-uni"].forEach(sel=>{
    $(sel).innerHTML=unis.map(u=>`<option value="${u}">${esc(UNI_NAME[u]||u)}</option>`).join("");
  });
  $("#c-tiers").innerHTML=TIERS.map((t,i)=>
    `<label style="margin-right:8px"><input type="checkbox" value="${t}" checked> ${t}</label>`).join("");
  $("#c-run").onchange=e=>{R_.index=+e.target.value;fillRunSelect($("#m-run"));
    fillRunSelect($("#d-run"));renderCurve();renderMetrics();renderDist();renderFiles();};
  $("#m-run").onchange=e=>{R_.index=+e.target.value;fillRunSelect($("#c-run"));
    fillRunSelect($("#d-run"));renderCurve();renderMetrics();renderDist();renderFiles();};
  $("#d-run").onchange=e=>{R_.index=+e.target.value;fillRunSelect($("#c-run"));
    fillRunSelect($("#m-run"));renderCurve();renderMetrics();renderDist();renderFiles();};
  ["#c-uni","#c-bench","#c-log"].forEach(s=>$(s).onchange=renderCurve);
  $("#c-tiers").onchange=renderCurve;
  ["#m-metric","#m-run"].forEach(s=>$(s).onchange=renderMetrics);
  $("#d-kind").onchange=renderDist; $("#d-uni").onchange=renderDist;
  $$("nav button").forEach(b=>b.onclick=()=>showTab(b.dataset.tab));
  let rz=null;
  window.onresize=()=>{clearTimeout(rz);rz=setTimeout(()=>{
    const on=$$("nav button").find(b=>b.classList.contains("on"));
    if(on)showTab(on.dataset.tab);},200);};
  const hash=location.hash.replace("#","");
  showTab(["curve","metrics","dist","gui","files"].includes(hash)?hash:"curve");
}
document.addEventListener("DOMContentLoaded",init);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--research", default=os.path.join(ROOT, "research"),
                    help="research 目录")
    ap.add_argument("--out", default="",
                    help="输出 HTML（默认 research/dashboard.html）")
    ap.add_argument("--max-runs", type=int, default=6,
                    help="内嵌最近 N 个回测批次（默认6）")
    args = ap.parse_args()
    p = build_dashboard(args.research, args.out or None,
                        max_runs=args.max_runs)
    n_runs = len(collect_runs(args.research, max_runs=args.max_runs))
    n_gui = len(collect_gui(args.research))
    size = os.path.getsize(p) / 1024
    print(f"已生成 {p}（{size:.0f} KB；回测批次 {n_runs}，单股记录 {n_gui}）")
    print("浏览器直接打开该文件即可（file:// 无需服务器）")


if __name__ == "__main__":
    main()
