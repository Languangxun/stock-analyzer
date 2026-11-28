#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_open_source.py - 开源仓库私有模型 / API Key 隔离自检

GitHub 开源版只允许携带 sm70x **适配层代码**，不允许任何模型或数据：
  - sm70x 数据/模型目录（sm701/、sm702/…）与行情库；
  - production 特征清单、三周期模型、历史预测、训练脚本；
  - 任意改名的 LightGBM 文本模型（内容含 num_tree_per_iteration=
    与 feature_names= 两个头部标记）；
  - 疑似 API Key（sk- / sk_ / oc_sk_ 前缀的长串）。

扫描对象 = `git ls-files -co --exclude-standard`（已跟踪 + 未忽略的新文件），
因此既能检查当前提交，也能在 `git add -A` 之前拦住误拷进来的 sm70x 目录。
无 git 时回退扫描工作区（跳过 .git/.venv/__pycache__/dist 等）。

用法：
    python3 check_open_source.py
退出码：0=通过；1=发现私有资产或疑似密钥。
"""
import os
import re
import subprocess
import sys

# Windows GBK 控制台打 ✅/❌ 会 UnicodeEncodeError（扫描结果丢失退出码），
# 统一重配 stdout/stderr 为 UTF-8（v7.5.17）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_SCAN = 4 * 1024 * 1024        # 每个文本文件最多读 4MB（模型头在开头）
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "dist", "_data",
             "logs", ".idea", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
TEXT_EXT = {".py", ".md", ".json", ".txt", ".ini", ".cfg", ".toml", ".yml",
            ".yaml", ".csv", ".tsv", ".js", ".html", ".css", ".sh", ".bat",
            ".ps1", ".xml", ".log"}
MODEL_MARKERS = (b"num_tree_per_iteration=", b"feature_names=")
KEY_RE = re.compile(r"(?:oc_)?sk[-_][A-Za-z0-9_\-]{16,}")


def _git_candidates():
    """已跟踪 + 未忽略的工作区文件（相对路径）；非 git 仓库返回 None。"""
    try:
        ok = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=HERE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if ok.returncode != 0:
            return None
        out = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30)
        if out.returncode != 0:
            return None
        return [p.decode("utf-8", "replace") for p in out.stdout.split(b"\0")
                if p]
    except Exception:
        return None


def _walk_candidates():
    """无 git 时兜底：遍历工作区（跳过常见缓存/依赖目录）。"""
    out = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            out.append(os.path.relpath(os.path.join(root, f), HERE))
    return out


def _name_reason(rel):
    """路径是否命中私有模型/数据文件名；命中返回原因，否则空串。"""
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if not parts:
        return ""
    for p in parts[:-1]:
        if p.lower().startswith("sm70"):
            return f"sm70x 目录/{p}"
    name = parts[-1].lower()
    if name.startswith("sm70") and name.endswith(
            (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".txt",
             ".bin", ".model")):
        return "sm70x 模型/数据文件"
    if name in ("features_tech.json", "lgbm_model.txt",
                "lgbm_train_backtest.py"):
        return "sm70x 特征/模型/训练脚本"
    if name.startswith("prod_") and name.endswith("_tech.txt"):
        return "生产模型文件"
    if name.startswith("valid_predictions") and ".csv" in name:
        return "历史预测文件"
    if name.startswith("global_daily") and ".db" in name:
        return "多市场行情库"
    if name.startswith(("crypto_daily", "crypto_hourly")) and ".db" in name:
        return "加密行情库"
    if name.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")):
        return "数据库文件"
    return ""


def _content_reasons(path):
    """内容自检：改名后的 LightGBM 文本模型 / 疑似 API Key。"""
    if os.path.abspath(path) == os.path.abspath(__file__):
        return []                       # 本文件含检测标记字符串，跳过
    ext = os.path.splitext(path)[1].lower()
    if ext not in TEXT_EXT:
        return []
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_SCAN)
    except OSError:
        return []
    out = []
    if all(m in raw for m in MODEL_MARKERS):
        out.append("内容疑似 LightGBM 文本模型")
    if KEY_RE.search(raw.decode("utf-8", "ignore")):
        out.append("疑似 API Key")
    return out


def check():
    files = _git_candidates()
    mode = "git 跟踪+未忽略"
    if files is None:
        files = _walk_candidates()
        mode = "工作区回退"
    bad = []
    for rel in files:
        reason = _name_reason(rel)
        if reason:
            bad.append((rel, reason))
            continue
        try:
            content = _content_reasons(os.path.join(HERE, rel))
        except Exception:
            content = []
        for why in content:
            bad.append((rel, why))
    return files, bad, mode


def main():
    files, bad, mode = check()
    print(f"扫描 {len(files)} 个文件（{mode}）...")
    if bad:
        print("\n❌ 开源检查失败：发现私有资产 / 疑似密钥（请移出仓库）")
        for rel, why in bad:
            print(f"  - {rel}  [{why}]")
        print("\n提示：模型/数据目录应放在仓库外（~/桌面/sm701、sm702），"
              "并在 .gitignore 中保持 sm70* 忽略规则。")
        return 1
    print("✅ 开源检查通过：无 sm70x 模型/行情库/私有特征，"
          "无 API Key 痕迹")
    return 0


if __name__ == "__main__":
    sys.exit(main())
