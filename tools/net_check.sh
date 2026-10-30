#!/usr/bin/env bash
# 双机直连自检工具（裁判机 / 比赛机通用）
#
#   ./tools/net_check.sh peer 192.168.77.2      # 查网卡、链路、路由、ping、22 端口
#   ./tools/net_check.sh ros  27 A              # 查 ROS 域、话题、感知是否可读
#
# 说明：ROS 2 话题不走 SSH 隧道，必须由 DDS 直接在网线上发现；
#       网页与 AI 网关（8080）才需要 SSH 转发。

set -uo pipefail

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

peer_check() {
  local ip="${1:?用法: net_check.sh peer <对端IP>}"
  echo "== 1. 本机网卡与地址 =="
  ip -br addr || true

  echo
  echo "== 2. 有线链路状态 =="
  local iface name carrier speed
  for iface in /sys/class/net/*; do
    name="$(basename "$iface")"
    [ "$name" = "lo" ] && continue
    [ -e "$iface/carrier" ] || continue
    carrier="$(cat "$iface/carrier" 2>/dev/null || echo '?')"
    speed="$(cat "$iface/speed" 2>/dev/null || echo '?')"
    printf '  %-12s carrier=%s  speed=%sMb/s\n' "$name" "$carrier" "$speed"
  done
  echo "  （carrier=1 表示网线已连通；speed=-1 多为虚拟网卡）"

  echo
  echo "== 3. 去对端的路由 =="
  ip route get "$ip" 2>/dev/null || echo "  无路由：两端可能不在同一网段"

  echo
  echo "== 4. ping 对端 =="
  local out
  out="$(ping -c 3 -W 1 "$ip" 2>&1)"
  if [ $? -eq 0 ]; then
    echo "  OK：$ip 可达"
  elif printf '%s' "$out" | grep -qiE 'not permitted|不允许'; then
    echo "  跳过：当前用户无 raw socket 权限，用 sudo 再试一次"
    echo "        sudo ping -c 3 $ip"
  else
    echo "  FAIL：$ip 不通 —— 查网线、静态 IP/掩码、防火墙"
    printf '%s\n' "$out" | tail -2 | sed 's/^/        /'
  fi

  echo
  echo "== 5. 对端 22 端口（SSH 隧道要用） =="
  if timeout 2 bash -c "cat < /dev/null > /dev/tcp/$ip/22" 2>/dev/null; then
    echo "  OK：$ip:22 可连（可以做网页与 AI 的 SSH 转发）"
  else
    echo "  FAIL：$ip:22 不通 —— 对端没开 sshd，或防火墙拦了"
  fi
}

ros_check() {
  local domain="${1:-27}" team="${2:-A}"
  export ROS_DOMAIN_ID="$domain"
  export ROS_LOCALHOST_ONLY=0
  export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}"

  echo "== ROS 环境 =="
  echo "  ROS_DOMAIN_ID=$ROS_DOMAIN_ID  ROS_LOCALHOST_ONLY=$ROS_LOCALHOST_ONLY  RMW=$RMW_IMPLEMENTATION"
  command -v ros2 >/dev/null 2>&1 || { echo "  未找到 ros2：先 source /opt/ros/humble/setup.bash"; return 1; }

  echo
  echo "== 话题列表 =="
  local topics
  topics="$(ros2 topic list 2>/dev/null)"
  printf '%s\n' "$topics" | sed 's/^/  /'

  echo
  echo "== 席位 $team 的三个话题 =="
  local t
  for t in perception action registration; do
    if printf '%s\n' "$topics" | grep -qx "/team_${team}/${t}"; then
      echo "  OK   /team_${team}/${t}"
    else
      echo "  缺失 /team_${team}/${t}"
    fi
  done

  echo
  echo "== 感知可读性（5 秒） =="
  if timeout 5 ros2 topic echo "/team_${team}/perception" --once >/dev/null 2>&1; then
    echo "  OK：能读到感知帧"
  else
    echo "  FAIL：5 秒内没读到 —— 未点开始 / 域或 RMW 不一致 / 组播被拦 / 网卡选错"
  fi

  echo
  echo "== 发现方式 =="
  echo "  ROS_STATIC_PEERS=${ROS_STATIC_PEERS:-（未设置，走组播发现）}"
  echo "  ROS_AUTOMATIC_DISCOVERY_RANGE=${ROS_AUTOMATIC_DISCOVERY_RANGE:-（默认 SUBNET）}"
}

case "${1:-help}" in
  peer) peer_check "${2:-}" ;;
  ros)  ros_check "${2:-27}" "${3:-A}" ;;
  *)    usage ;;
esac
