#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_client_zip.py - 生成客户端压缩包（dist/stock-analyzer-client-<日期>.zip）

客户端包 = 运行所需最小集合：GUI/CLI（含 v6.1 三档引擎与 AI 客户端）+ 插件 +
配置模板 + 说明/许可 + 本地日K缓存 stock_cache.db。

默认先 VACUUM 压缩数据库（1GB+ 库可显著减小包体，需约 2 倍空闲磁盘），
用 --no-vacuum 跳过。

用法：
  python build_client_zip.py
  python build_client_zip.py --no-vacuum --out dist/custom.zip
"""
import argparse
import os
import sqlite3
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
DB = os.path.join(HERE, "stock_cache.db")
FILES = [
    "stock_gui.py", "stock_predict.py", "README.md", "LICENSE",
    "PLUGIN_API.md", "stock_gui.ini", "requirements-client.txt",
]
PLUGIN_DIR = "plugins"
PLUGIN_FILES = ["__init__.py", "api.py", "base.py", "trade_log.py"]


def vacuum(db):
    con = sqlite3.connect(db, timeout=60)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        t0 = time.time()
        con.execute("VACUUM")
        con.commit()
        print(f"VACUUM 完成 {time.time() - t0:.0f}s")
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-vacuum", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    if not os.path.exists(DB):
        raise SystemExit("缺少 stock_cache.db")
    if not args.no_vacuum:
        vacuum(DB)
    out = args.out or os.path.join(
        DIST, f"stock-analyzer-client-{time.strftime('%Y%m%d')}.zip")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    t0 = time.time()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in FILES:
            p = os.path.join(HERE, f)
            if os.path.exists(p):
                z.write(p, f)
                print(f"  + {f}")
        for f in PLUGIN_FILES:
            p = os.path.join(HERE, PLUGIN_DIR, f)
            if os.path.exists(p):
                z.write(p, f"{PLUGIN_DIR}/{f}")
                print(f"  + {PLUGIN_DIR}/{f}")
        z.write(DB, "stock_cache.db")
        print("  + stock_cache.db")
    print(f"写入 {out}（{os.path.getsize(out)/1e6:.0f} MB，"
          f"{time.time() - t0:.0f}s）")


if __name__ == "__main__":
    main()
