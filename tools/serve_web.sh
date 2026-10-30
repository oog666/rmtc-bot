#!/usr/bin/env bash
# 裁判端 8080 转发：让比赛机不用 SSH 隧道也能打开网页 / 调 AI 网关
#
#   ./tools/serve_web.sh                 # 自动挑选有线网卡地址
#   ./tools/serve_web.sh --listen 0.0.0.0
#
# 保持这个窗口开着；Ctrl+C 结束。
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LISTEN=""
EXTRA=()

while [ $# -gt 0 ]; do
  case "$1" in
    --listen) LISTEN="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --port|--target|--target-port) EXTRA+=("$1" "${2:?}"); shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$LISTEN" ]; then
  LISTEN="$(ip -4 -br addr show scope global 2>/dev/null \
    | awk '$2 ~ /^192\.168\.77\./ {print $3}' | cut -d/ -f1 | head -1)"
fi
if [ -z "$LISTEN" ]; then
  LISTEN="$(ip -4 -br addr show scope global 2>/dev/null \
    | awk '$1 !~ /^(wl|docker|br-|veth|virbr|tun|tap)/ {print $3}' \
    | cut -d/ -f1 | head -1)"
fi
[ -n "$LISTEN" ] || { echo "没找到有线地址，用 --listen 指定，例如 --listen 0.0.0.0" >&2; exit 2; }

exec /usr/bin/python3 "$HERE/web_relay.py" --listen "$LISTEN" "${EXTRA[@]}"
