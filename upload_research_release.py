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

OWNER = "Languangxun"
REPO = "stock-analyzer"
HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")

BODY_DEFAULT = (
    "## v6.1.6（2026-09-27）\n\n"
    "### 每日荐股：「综合(18策略)」\n"
    "- 下拉「综合(旧评分)」→「综合(18策略)」：移植通达信口径 18 个短线策略"
    "（布林突破/断板反包/\n"
    "  均线多头/连板/超跌反弹/回踩/量价齐升…）为自包含实现（stock_gui.py / CLI 同步，"
    "build_cli 生成）；\n"
    "- 单次全市场扫描（尾部 500 根、乘法前复权、真实换手、逐策略基础过滤，"
    "套用荐股权限与股票池），\n"
    "  各策略候选池内 min-max 归一化打分，综合分 = 命中策略数 × 100 + 命中策略均分"
    "（共振优先）；\n"
    "  列表显示共振数/均分/命中策略，双击行直接分析；\n"
    "- 与源引擎随机 500 只 × 18 策略命中集合对拍全部一致；全A 约 35s（纯本地缓存不联网）。\n\n"
    "### 一致性与仪表盘\n"
    "- `dashboard.html` 默认展示最新「全期 + 新复权口径」批次（此前默认最新 ts，"
    "会落到 val/bull\n"
    "  分段批次而与 README 不一致）；旧复权口径批次/单股记录/每只股导出统一标注"
    "「已作废」\n"
    "  且不参与默认选择；回测/导出产物新增 adj 口径标记，不再靠时间判断；\n"
    "- 「工具→信号胜率」面板与「策略消融」弹窗改用**同一引擎与切分**"
    "（`_bt_segments` / `_bt_events`），\n"
    "  交易/胜率/年化/回撤逐项一致；每只股回测导出同口径；\n"
    "- 旧口径产物已按新口径重跑：全库消融 7179 只 + 三档选型（刷新策略缓存 + "
    "`perstock_tier_picks.json`）\n"
    "  + 每只股回测导出 `research/perstock_backtest_v6.1.6_*`。\n\n"
    "### 资产说明\n"
    "- `stock-analyzer-client-v6.1.6-<日期>.zip`：客户端（GUI + CLI + 插件 + "
    "**全量数据缓存**），\n"
    "  解压即用；配置为**空 Key 模板**，首次运行请在设置内填自己的 API Key；\n"
    "- `research_v6.1.6.zip`：回测报告/图表/本地仪表盘/逐对象消融明细与三档选型"
    "（排除 legacy 与 *.pkl/*.npz）。\n\n"
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
    ap.add_argument("--tag", default="v6.1.6")
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
