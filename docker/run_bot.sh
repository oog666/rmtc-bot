#!/usr/bin/env bash
# 在 Docker 里跑比赛端（ROS 2 Humble）
#
#   ./docker/run_bot.sh --team A --name '图灵队' --token 'xxx' --build
#
# 关键点：必须 --network host，DDS 的组播与动态端口无法用 -p 映射。
# 容器与主机共享网络后，主机上建的 SSH 隧道（127.0.0.1:8080）在容器里直接可用。

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${RMTC_IMAGE:-rmtc/humble:1.0}"
DOCKER=(docker)
if [ "${RMTC_DOCKER_SUDO:-0}" = "1" ]; then DOCKER=(sudo docker); fi
TEAM="A"
NAME="图灵队"
DOMAIN="${ROS_DOMAIN_ID:-27}"
TOKEN="${RMTC_TEAM_TOKEN:-}"
GATEWAY="${RMTC_AI_URL:-http://127.0.0.1:8080/v1/chat/completions}"
DDS_IP="${RMTC_DDS_IP:-}"
BUILD=0
DRY=0
PROFILE_DIR=""

usage() {
  cat <<'EOF'
用法: docker/run_bot.sh [选项]

  --team A            席位（默认 A）
  --name 队名         队名（默认 图灵队）
  --domain 27         ROS_DOMAIN_ID（默认取环境变量，其次 27）
  --token TOKEN       本场令牌（也可用 RMTC_TEAM_TOKEN）
  --gateway URL       LLM 网关（默认 http://127.0.0.1:8080/v1/chat/completions）
  --dds-ip IP         指定 DDS 使用的本机 IP，生成 Fast DDS 白名单（多网卡时用）
  --build             强制重新 colcon build
  --dry-run           只打印 docker 命令，不执行
  -h, --help          显示帮助

示例:
  ./docker/run_bot.sh --team A --name '图灵队' --token 'test-token' --build
  ./docker/run_bot.sh --dds-ip 192.168.77.2 --token 'test-token'
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --team)    TEAM="${2:?--team 需要值}"; shift 2 ;;
    --name)    NAME="${2:?--name 需要值}"; shift 2 ;;
    --domain)  DOMAIN="${2:?--domain 需要值}"; shift 2 ;;
    --token)   TOKEN="${2:?--token 需要值}"; shift 2 ;;
    --gateway) GATEWAY="${2:?--gateway 需要值}"; shift 2 ;;
    --dds-ip)  DDS_IP="${2:?--dds-ip 需要值}"; shift 2 ;;
    --build)   BUILD=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

if [ "$DRY" -eq 0 ] && ! command -v "${DOCKER[0]}" >/dev/null 2>&1; then
  echo "未找到 docker 命令：先安装 Docker Engine（Linux），并确认当前用户能执行 docker ps" >&2
  exit 2
fi

# 容器以当前用户运行并写 contestant_ws/build，所以要求免 sudo；
# 若确实只能用 sudo，RMTC_DOCKER_SUDO=1 会以 root 运行（build/ 会变成 root 所有）
if [ "$DRY" -eq 0 ] && ! "${DOCKER[@]}" info >/dev/null 2>&1; then
  echo "当前用户无法访问 Docker（/var/run/docker.sock 权限不足）。二选一：" >&2
  echo "  A) 推荐：sudo usermod -aG docker $USER && newgrp docker   # 之后重开终端" >&2
  echo "  B) 临时：RMTC_DOCKER_SUDO=1 $0 --token '...'              # 容器以 root 运行" >&2
  exit 2
fi

if [ "$DRY" -eq 0 ] && ! "${DOCKER[@]}" image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[i] 本地没有镜像 $IMAGE，开始构建（首次约 5-10 分钟）"
  "${DOCKER[@]}" build -t "$IMAGE" "$REPO/docker"
fi

TTY=()
[ -t 0 ] && TTY=(-t)

ARGS=(--rm -i "${TTY[@]}" --network host --ipc host
  --user "$(id -u):$(id -g)"
  -e HOME=/tmp -e ROS_HOME=/tmp/.ros
  -e "ROS_DOMAIN_ID=$DOMAIN"
  -e ROS_LOCALHOST_ONLY=0
  -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp
  -e "RMTC_FORCE_BUILD=$BUILD"
  -e "RMTC_TEAM_TOKEN=$TOKEN"
  # 容器内 cwd 是 /ws/contestant_ws，决策文件在仓库根，必须给绝对路径
  -e "RMTC_DECISION_CONFIG=${RMTC_DECISION_CONFIG:-/ws/decision_config.json}"
  -v "$REPO":/ws)

if [ -n "$DDS_IP" ]; then
  PROFILE_DIR="$(mktemp -d)"
  cat > "$PROFILE_DIR/fastdds.xml" <<XML
<?xml version="1.0" encoding="UTF-8" ?>
<profiles xmlns="http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles">
  <transport_descriptors>
    <transport_descriptor>
      <transport_id>rmtc_udp</transport_id>
      <type>UDPv4</type>
      <interfaceWhiteList>
        <address>${DDS_IP}</address>
      </interfaceWhiteList>
    </transport_descriptor>
  </transport_descriptors>
  <participant profile_name="rmtc_profile" is_default_profile="true">
    <rtps>
      <userTransports>
        <transport_id>rmtc_udp</transport_id>
      </userTransports>
      <useBuiltinTransports>false</useBuiltinTransports>
    </rtps>
  </participant>
</profiles>
XML
  ARGS+=(-v "$PROFILE_DIR":/dds:ro -e FASTRTPS_DEFAULT_PROFILES_FILE=/dds/fastdds.xml)
  trap 'rm -rf "$PROFILE_DIR"' EXIT
fi

INNER='set -e
source /opt/ros/humble/setup.bash
cd /ws/contestant_ws
if [ ! -f install/setup.bash ] || [ "${RMTC_FORCE_BUILD:-0}" = "1" ]; then
  echo "[build] colcon build --symlink-install --packages-select rmtc_bot"
  colcon build --symlink-install --packages-select rmtc_bot
fi
source install/setup.bash
echo "[run] team=$1 name=$2 domain=${ROS_DOMAIN_ID} gateway=$3"
exec ros2 run rmtc_bot rmtc_bot --ros-args \
  -p "team:=$1" -p "team_name:=$2" -p "ai_gateway:=$3"'

CMD=("${DOCKER[@]}" run "${ARGS[@]}" "$IMAGE" bash -lc "$INNER" _ "$TEAM" "$NAME" "$GATEWAY")

if [ "$DRY" -eq 1 ]; then
  printf '%q ' "${CMD[@]}"
  printf '\n'
  exit 0
fi

"${CMD[@]}"
