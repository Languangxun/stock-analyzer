#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""upload_research_release.py - 研究产物打包 + GitHub Release 发布（v6.1 起通用化）

两种用法（默认都不做，必须显式指定，避免误触发）：

  1) 只打包（本地，不联网）：
     python upload_research_release.py --pack --tag v6.1
        → dist/research_v6.1.zip           （research/ 产物，排除 legacy/ 与 .pkl）
        → dist/stock-analyzer-client-<日期>.zip（加 --with-client 时调用 build_client_zip.py）

  2) 创建/更新 Release 并上传资产：
     python upload_research_release.py --upload --tag v6.1 \
         --name "stock-analyzer v6.1" \
         --asset dist/research_v6.1.zip dist/stock-analyzer-client-20260919.zip
       同名资产会先删除再上传（可重复执行）。

Token：从 git credential manager 读取（git credential fill），不落盘。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

OWNER = "monologue-github"
REPO = "stock-analyzer"
HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")

BODY_DEFAULT = (
    "## v6.1.5（2026-09-26）\n\n"
    "### 回测产物版本化 + 全样本重跑\n"
    "- `APP_VERSION=6.1.5`；`backtests/backtest_v61.py` 每次运行自动新建\n"
    "  `research/backtest_v6.1.5_<时间戳>_<区间>[_tag]/`：`report.json/md`、"
    "`run_meta.json`、\n"
    "  `tables/*.csv`（组合/逐笔/相位/逐笔分布/基准/净值曲线）、`charts/*.svg`"
    "（相位箱线/逐笔箱线/收益柱状）；\n"
    "  research 根保留最新报告副本，`--compare`/发布链不变。\n"
    "- 全样本四口径重跑（数据截至 2026-09-24，109s，10 图 / 6 表）："
    "全A/主板与 v6.1.4 权威口径逐项一致；\n"
    "  ETF/全A含ETF 因当日新缓存 ETF 入池小幅变化（详见 README 第三节 3.5/3.6）。\n\n"
    "### 工具→信号胜率（回测面板）\n"
    "- 策略**全历史**信号回测（训练前75%/验证后25%）+ IC(T+1/T+5) + "
    "信号后 1/5 日收益；\n"
    "- 收益曲线只画训练集净值（验证集仅指标）；「导出回测」支持 "
    ".txt（完整明细+净值数据）/ .csv（逐日净值+回撤）。\n\n"
    "### 数据层修复随版\n"
    "- 节假日锚（1.7G 缓存不再周末全量空拉）；`stock_fetch.log` 逐条拉取诊断；\n"
    "- 死源热修：web.ifzq 对 hfq 曾返 501（时段性）不再被自愈写入、501 纳入熔断、"
    "探测改 hfq；急救箱腾讯探测同步改 hfq。\n\n"
    "## v6.1.4（2026-09-25）\n\n"
    "### AI 网关兼容与配置修复\n"
    "- AI 请求带自有 UA `stock-analyzer/6.1.4`（Cloudflare 会以 error code 1010 拦截"
    "通用库 UA），并带稳定会话头 `x-opencode-session`（opencode zen 必需，缺失报 400）；\n"
    "- 修复设置「保存并应用」时 ini 被整体重写，导致 `base_url/model`、`[predict]`、"
    "`[picks]`、`[data]` 全部丢失的问题（改为读旧配置后合并保存）。\n\n"
    "### 单股买卖点（GUI）\n"
    "- 消融选中的**筹码峰 / 板块轮动**信号可在图上回放（此前无展示分支 → 选激进档后"
    "没有买卖点）；\n"
    "- **连续同向信号压缩**：同一轮机会只保留首个 B/S，不再成串重复；\n"
    "- **激进档信号密度兜底**：所选策略近 250 日信号 <8 个时改用多维评分"
    "（沿用该档参数：买点门槛 1 / 冷却 3），震荡区间也能标出足够波段买卖点；"
    "面板与报告标注「信号口径」；\n"
    "- 「工具→信号胜率」改用所选策略的风险参数（此前误用默认档）。\n\n"
    "### 数据与后台\n"
    "- **后台主动预取未分析股K线**：启动 60s 后首轮、此后每小时一轮，"
    "当前股/自选 → 样本池 → 全库滚动补齐（跳过北交所/退市股），"
    "收盘 15:05 后自动回补当日K线；`stock_gui.ini [predict] auto_prefetch = 0` 可关。\n\n"
    "### 因子实验室（研究）\n"
    "- `backtest_factor_ablation.py` 新增 `--db` / `--out-dir`：大样本实验可用独立快照库"
    "与独立产物目录，避免与正在运行的程序争用主库；\n"
    "- `assemble_panel` 筹码三因子缺失按**截面中性值**填充（此前约六成个股因价格跑出"
    "因果网格被整只丢弃）；\n"
    "- 修复 `--stage all` 漏组装面板（`panel` 阶段）导致 Stage 3 直接报错的问题。\n\n"
    "### 资产说明\n"
    "- `stock-analyzer-client-v6.1.4-<日期>.zip`：客户端（GUI + CLI + 插件 + "
    "**全量数据缓存**），解压即用；配置为**空 Key 模板**，首次运行请在设置内填自己的 API Key；\n"
    "- `research_v6.1.4.zip`：全部回测 JSON 与报告产物"
    "（含新增 factor_lab 大样本复核产物；逐对象消融明细随包分发，不入仓）。\n\n"
    "全部输出仅为历史统计研究，不构成投资建议。"
)

try:                                # Windows 控制台 GBK 兜底
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


# ---------------- 打包 ----------------

def pack_research(tag="v6.1", out=None, include_legacy=False):
    """把 research/ 产物打成 dist/research_<tag>.zip，返回路径。"""
    out = out or os.path.join(DIST, f"research_{tag}.zip")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    skip_dirs = () if include_legacy else ("legacy",)
    n = 0
    t0 = time.time()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(os.path.join(HERE, "research")):
            rel_root = os.path.relpath(root, HERE)
            if skip_dirs and rel_root.split(os.sep)[0] in skip_dirs:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for f in files:
                if f.endswith((".pkl", ".npz", ".bak")):
                    continue
                p = os.path.join(root, f)
                arc = os.path.relpath(p, HERE).replace(os.sep, "/")
                z.write(p, arc)
                n += 1
    print(f"打包 {n} 个文件 → {out}"
          f"（{os.path.getsize(out)/1e6:.1f} MB，{time.time()-t0:.0f}s）")
    return out


def pack_client():
    """调用 build_client_zip.py 生成客户端包，返回路径。"""
    r = subprocess.run([sys.executable, os.path.join(HERE, "build_client_zip.py")],
                       cwd=HERE)
    if r.returncode != 0:
        raise SystemExit("build_client_zip.py 失败")
    outs = sorted((os.path.join(DIST, f) for f in os.listdir(DIST)
                   if f.startswith("stock-analyzer-client-")), key=os.path.getmtime)
    return outs[-1] if outs else None


# ---------------- GitHub API ----------------

def _get_token():
    """从 git credential manager 读取 GitHub token。"""
    inp = "protocol=https\nhost=github.com\n\n"
    try:
        out = subprocess.run(["git", "credential", "fill"], input=inp, text=True,
                             capture_output=True, check=True).stdout
    except Exception as e:
        print(f"无法从 git credential 读取 token: {e}")
        return None
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    return None


def _api_request(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        if isinstance(data, dict):
            payload = json.dumps(data).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        else:
            payload = data
        req.data = payload
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def ensure_release(tag, name, body, token, prerelease=False):
    """Release 不存在则创建；返回 (release_id, upload_url)。"""
    h = {"Authorization": f"Bearer {token}"}
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/tags/{tag}"
    status, resp = _api_request(url, headers=h)
    if status == 200:
        rel = json.loads(resp)
        print(f"Release {tag} 已存在，id={rel['id']}")
        return rel["id"], rel["upload_url"].replace("{?name,label}", "")
    status, resp = _api_request(
        f"https://api.github.com/repos/{OWNER}/{REPO}/releases", method="POST",
        data={"tag_name": tag, "name": name, "body": body,
              "prerelease": bool(prerelease)}, headers=h)
    if status not in (200, 201):
        raise SystemExit(f"创建 release 失败: {status}\n{resp}")
    rel = json.loads(resp)
    print(f"Release {tag} 创建成功，id={rel['id']}")
    return rel["id"], rel["upload_url"].replace("{?name,label}", "")


def upload_asset(rel_id, upload_url, path, token):
    """上传单个资产（同名先删除）。"""
    h = {"Authorization": f"Bearer {token}"}
    name = os.path.basename(path)
    status, resp = _api_request(
        f"https://api.github.com/repos/{OWNER}/{REPO}/releases/{rel_id}", headers=h)
    if status == 200:
        for a in json.loads(resp).get("assets", []):
            if a["name"] == name:
                print(f"  删除旧资产 {name} (id={a['id']})")
                _api_request(
                    f"https://api.github.com/repos/{OWNER}/{REPO}"
                    f"/releases/assets/{a['id']}", method="DELETE", headers=h)
    size = os.path.getsize(path)
    print(f"  上传 {name}（{size/1e6:.1f} MB）...")
    with open(path, "rb") as f:
        data = f.read()
    status, resp = _api_request(
        f"{upload_url}?name={name}", method="POST", data=data,
        headers={**h, "Content-Type": "application/octet-stream"})
    if status not in (200, 201):
        raise SystemExit(f"上传 {name} 失败: {status}\n{resp}")
    print(f"  ✓ {json.loads(resp)['browser_download_url']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v6.1.5")
    ap.add_argument("--name", default=None, help="Release 标题（默认 stock-analyzer <tag>）")
    ap.add_argument("--body", default=BODY_DEFAULT)
    ap.add_argument("--asset", nargs="*", default=[], help="要上传的本地文件")
    ap.add_argument("--pack", action="store_true", help="打包 research 产物")
    ap.add_argument("--with-client", action="store_true",
                    help="打包客户端 zip（调用 build_client_zip.py，需 stock_cache.db）")
    ap.add_argument("--include-legacy", action="store_true",
                    help="research 打包时包含 legacy/")
    ap.add_argument("--prerelease", action="store_true")
    ap.add_argument("--upload", action="store_true", help="创建/更新 Release 并上传")
    args = ap.parse_args()

    assets = list(args.asset)
    if args.pack:
        assets.append(pack_research(args.tag, include_legacy=args.include_legacy))
    if args.with_client:
        p = pack_client()
        if p:
            assets.append(p)

    if not args.upload:
        print("\n（未指定 --upload：仅本地打包，未联网）")
        for a in assets:
            print(f"  待上传: {a}")
        return

    missing = [a for a in assets if not os.path.exists(a)]
    if missing:
        raise SystemExit("资产不存在: " + ", ".join(missing))
    token = _get_token()
    if not token:
        raise SystemExit("未获取到 GitHub token，退出")
    rel_id, upload_url = ensure_release(
        args.tag, args.name or f"stock-analyzer {args.tag}", args.body,
        token, args.prerelease)
    for a in assets:
        upload_asset(rel_id, upload_url, a, token)
    print(f"\n完成：https://github.com/{OWNER}/{REPO}/releases/tag/{args.tag}")


if __name__ == "__main__":
    main()
