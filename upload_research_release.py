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
    "v6.1（2026-09-19）\n\n"
    "- 新增标准回测：两口径（full 全A / main 沪深主板）× 四类产品"
    "（稳健/均衡/激进组合 + 荐股逐笔），报告写入 README 第三节；\n"
    "- AI 分析升级：OpenAI 兼容平台（DeepSeek/智谱/opencode 等）+ 接口地址自定义 + "
    "模型列表自动获取 + 果断结论式提示词 + 更全数据上下文 + 多轮问答会话缓存；\n"
    "- 设置内新增荐股权限（行业多选 + 板块勾选）与 AI 自动选档（按风险偏好在三档内选一档）；\n"
    "- 消融升级：多指标结合选优（Calmar/PF/胜率/年化 rank 加权），消融对象新增筹码峰、"
    "板块轮动（9 类算法），全程本地计算、AI 不参与；\n"
    "- 主程序任意位置按键即聚焦搜索框，新增候选下拉与自动补全（缓存加载）；\n"
    "- 仓库整理：全部回测脚本归档 backtests/。\n\n"
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
    ap.add_argument("--tag", default="v6.1")
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
