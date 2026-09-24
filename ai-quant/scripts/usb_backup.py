#!/usr/bin/env python3
"""U 盘快照备份：时间戳快照 + sha256 清单 + 复制后校验 + 保留策略。

- 挂载点探测：/media/*、/media/*/*（ismount）、/mnt/usb、/mnt/sda1、/mnt/sdb1
- 快照目录：<mount>/ai-quant-backup/<label>/<YYYYMMDD-HHMMSS>/
- manifest.json 记录每个文件的相对路径/大小/sha256；复制后逐项校验
- 保留最近 keep 份快照，旧的自动清理

用法：
  .venv/bin/python scripts/usb_backup.py                  # 默认集合
  .venv/bin/python scripts/usb_backup.py --label stock --keep 14
  代码内：from scripts.usb_backup import backup
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_ROOT = "ai-quant-backup"

DEFAULT_PATHS = [
    "sim/state/account.json",
    "sim/state/stock_account.json",
    "memory/lessons.json",
    "memory/embeddings.json",
    "memory/daily",
    "config/model.yaml",
    "config/risk.yaml",
    "config/stock.yaml",
    "config/system.yaml",
    "README.md",
]


def _sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _is_writable(path):
    try:
        probe = os.path.join(path, ".ai-quant-write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def find_mounts():
    """返回可写挂载点列表（去重）。"""
    cands = []
    media = "/media"
    if os.path.isdir(media):
        for user_dir in sorted(os.listdir(media)):
            base = os.path.join(media, user_dir)
            if os.path.ismount(base):
                cands.append(base)
            elif os.path.isdir(base):
                for name in sorted(os.listdir(base)):
                    p = os.path.join(base, name)
                    if os.path.ismount(p):
                        cands.append(p)
    for p in ("/mnt/usb", "/mnt/sda1", "/mnt/sdb1", "/mnt/usb0"):
        if os.path.ismount(p):
            cands.append(p)
    out = []
    for p in cands:
        if p not in out and _is_writable(p):
            out.append(p)
    return out


def _collect_files(paths):
    """展开为 [(绝对路径, 相对路径)]；目录递归收集。"""
    out = []
    for p in paths:
        ap = p if os.path.isabs(p) else os.path.join(BASE_DIR, p)
        if os.path.isfile(ap):
            out.append((ap, os.path.basename(ap)))
        elif os.path.isdir(ap):
            for root, _dirs, files in os.walk(ap):
                for fn in sorted(files):
                    fp = os.path.join(root, fn)
                    rel = os.path.relpath(fp, BASE_DIR)
                    out.append((fp, rel))
    return out


def backup(paths=None, label="sim", keep=14, dry_run=False,
           progress=print, min_free_mb=50):
    """执行一次 U 盘快照备份。返回结果 dict（ok/reason/mount/snapshot/...）。"""
    paths = list(paths or DEFAULT_PATHS)
    files = _collect_files(paths)
    total_bytes = sum(os.path.getsize(ap) for ap, _ in files)
    mounts = find_mounts()
    if not mounts:
        progress("[backup] 未检测到可写 U 盘挂载，跳过")
        return {"ok": False, "reason": "未检测到 U 盘挂载", "copied": 0}
    mount = mounts[0]
    free = shutil.disk_usage(mount).free
    if free < total_bytes * 1.2 + min_free_mb * (1 << 20):
        progress(f"[backup] U 盘空间不足：需 {total_bytes / 1e6:.1f}MB，"
                 f"剩 {free / 1e9:.1f}GB")
        return {"ok": False, "reason": "空间不足", "mount": mount}

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label_dir = os.path.join(mount, BACKUP_ROOT, label)
    snap_dir = os.path.join(label_dir, stamp)
    if dry_run:
        progress(f"[backup][dry-run] {len(files)} 个文件 "
                 f"({total_bytes / 1e6:.1f}MB) -> {snap_dir}")
        return {"ok": True, "dry_run": True, "mount": mount,
                "snapshot": snap_dir, "files": len(files)}
    os.makedirs(snap_dir, exist_ok=True)
    manifest = {"created": datetime.now().isoformat(), "label": label,
                "source": BASE_DIR, "files": []}
    copied = verified = 0
    for ap, rel in files:
        dst = os.path.join(snap_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copy2(ap, dst)
        except OSError as e:
            progress(f"[backup] 拷贝失败 {rel}: {e}")
            continue
        digest = _sha256(ap)
        ok = (os.path.exists(dst) and _sha256(dst) == digest)
        manifest["files"].append({
            "path": rel, "size": os.path.getsize(ap), "sha256": digest,
            "verified": bool(ok),
        })
        copied += 1
        verified += 1 if ok else 0
    with open(os.path.join(snap_dir, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    with open(os.path.join(mount, BACKUP_ROOT, "last_backup.txt"), "w") as f:
        f.write(datetime.now().isoformat())
    removed = _rotate(label_dir, keep)
    status = "OK" if copied and verified == copied else "PARTIAL"
    progress(f"[backup] {status} -> {snap_dir}：{copied} 个文件，"
             f"校验 {verified}/{copied}，清理旧快照 {len(removed)} 份")
    return {"ok": copied > 0 and verified == copied, "mount": mount,
            "snapshot": snap_dir, "copied": copied, "verified": verified,
            "removed": removed}


def _rotate(label_dir, keep):
    """只保留最近 keep 份快照。"""
    try:
        snaps = sorted(
            [d for d in os.listdir(label_dir)
             if os.path.isdir(os.path.join(label_dir, d)) and d != "latest"],
            reverse=True)
    except OSError:
        return []
    removed = []
    for old in snaps[max(1, keep):]:
        try:
            shutil.rmtree(os.path.join(label_dir, old))
            removed.append(old)
        except OSError:
            pass
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="sim")
    ap.add_argument("--keep", type=int, default=14)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--path", action="append", default=None,
                    help="自定义备份路径（可多次），默认使用内置集合")
    args = ap.parse_args()
    res = backup(paths=args.path, label=args.label, keep=args.keep,
                 dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
