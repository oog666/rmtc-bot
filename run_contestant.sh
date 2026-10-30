#!/usr/bin/env bash
# 一键启动参赛程序（现场只需改环境变量，不要改代码）
#
#   ./run_contestant.sh A '图灵队'
#
# 现场必须设置：
#   export ROS_DOMAIN_ID=xx            # 主办方公布
#   export RMTC_TEAM_TOKEN='本场令牌'   # 主办方按场发放
#   export RMTC_AI_URL='http://<裁判机>:<端口>/v1/chat/completions'
set -eo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEAM="${1:-A}"
NAME="${2:-图灵队}"
WS="$HERE/contestant_ws"

if [[ ! -f /opt/ros/humble/setup.bash ]]; then
  echo "未找到 /opt/ros/humble/setup.bash，请先安装 ROS 2 Humble" >&2
  exit 2
fi
# shellcheck disable=SC1091
source /opt/ros/humble/setup.bash

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-27}"
export ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-0}"
export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}"

if [[ ! -f "$WS/install/setup.bash" ]]; then
  echo "[build] 首次运行，正在构建 rmtc_bot ..."
  ( cd "$WS" && colcon build --symlink-install --packages-select rmtc_bot )
fi
# shellcheck disable=SC1091
source "$WS/install/setup.bash"

echo "[run] 席位=$TEAM 队名=$NAME 域=$ROS_DOMAIN_ID 网关=${RMTC_AI_URL:-默认本机 8080}"
exec ros2 run rmtc_bot rmtc_bot --ros-args \
  -p "team:=$TEAM" \
  -p "team_name:=$NAME" \
  -p "decision_config:=$HERE/decision_config.json"
