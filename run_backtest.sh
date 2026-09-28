#!/usr/bin/env bash
# run_backtest.sh - 一键全量回测（桌面快捷方式「一键回测」的入口）
#
# 流程（与仪表盘「全量回测命令」一致）：
#   1/4 全期      四口径 × 四档（all/main/etf/all_etf）
#   2/4 样本外    val 段（--tag val）
#   3/4 强势段    bull 段（--tag bull）
#   4/4 逐股回测  全市场每只股（多进程），并刷新本地仪表盘
#   --yearly      额外跑逐年分段（研究脚本，更新 research/tiers_yearly.json）
#
# 产物：research/backtest_v6.1.7_*、research/perstock_backtest_v6.1.7_*、
#       research/dashboard.html；终端日志另存 research/oneclick_backtest_<时间戳>.log
#
# 用法：bash run_backtest.sh [--yearly] [--dry-run]
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1
PY="$ROOT/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

DO_YEARLY=0
DRY=0
for a in "$@"; do
  case "$a" in
    --yearly)  DO_YEARLY=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "未知参数：$a（支持 --yearly / --dry-run）"; exit 2 ;;
  esac
done

TS="$(date +%Y%m%d_%H%M%S)"
LOG="$ROOT/research/oneclick_backtest_${TS}.log"
W="$(nproc 2>/dev/null || echo 4)"
[ "$W" -gt 8 ] && W=8          # 与既有批次一致的上限（GUI 默认 4）

say() { printf '\n\033[1;36m===== %s =====\033[0m\n' "$*"; }

run() {
  local label="$1"; shift
  say "$label"
  printf '$'; printf ' %q' "$@"; printf '\n'
  if [ "$DRY" = "1" ]; then return 0; fi
  "$@" 2>&1 | tee -a "$LOG"
  local rc=${PIPESTATUS[0]}
  if [ "$rc" -ne 0 ]; then
    printf '\n\033[1;31m[失败] 上一步退出码 %s，已停止。日志：%s\033[0m\n' \
           "$rc" "$LOG"
    [ -t 0 ] && read -rp "按回车关闭…" _ || true
    exit "$rc"
  fi
}

printf '一键全量回测开始 %s\n仓库：%s\n日志：%s\n' \
       "$(date '+%F %T')" "$ROOT" "$LOG"

N=4
[ "$DO_YEARLY" = "1" ] && N=5
I=0
STEP=""
step() { I=$((I + 1)); STEP="$I/$N $1"; }

step "全期 · 四口径 × 四档"
run "$STEP" "$PY" backtests/backtest_v61.py
step "样本外 val · 四口径"
run "$STEP" "$PY" backtests/backtest_v61.py --segment val --tag val
step "强势段 bull · 四口径"
run "$STEP" "$PY" backtests/backtest_v61.py --segment bull --tag bull
if [ "$DO_YEARLY" = "1" ]; then
  step "逐年分段 · 全A"
  run "$STEP" "$PY" backtests/backtest_tiers.py --tier all --segment yearly
fi
step "全市场逐股回测 · 多进程($W)"
run "$STEP" "$PY" backtests/stock_backtest_export.py --pool all --workers "$W"

say "全部完成（$(date '+%F %T')）"
DASH="$ROOT/research/dashboard.html"
if [ "$DRY" != "1" ] && [ -f "$DASH" ]; then
  printf '打开仪表盘：%s\n' "$DASH"
  { command -v xdg-open >/dev/null 2>&1 && xdg-open "$DASH" \
      >/dev/null 2>&1; } || true
fi
[ -t 0 ] && read -rp "按回车关闭…" _ || true
