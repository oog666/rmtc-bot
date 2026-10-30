#!/usr/bin/env bash
# 比赛机启动器（原生 ROS 2 Humble）——一条命令起自己的程序
#
#   ./start_bot.sh test-token
#   ./start_bot.sh test-token --team A --name '图灵队'
#   ./start_bot.sh test-token --gateway http://192.168.77.1:8080/v1/chat/completions
#   ./start_bot.sh --dry-run test-token        # 只打印将要做的事
#
# 做的事：选网关 → 设域/环境 → 必要时编译 → 启动 rmtc_bot
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEAM="A"
NAME="图灵队"
TOKEN=""
GATEWAY=""
DOMAIN="${ROS_DOMAIN_ID:-27}"
DRY=0
BUILD=0
DECISION=""

usage() {
  cat <<'EOF'
用法: ./start_bot.sh [令牌] [选项]

  令牌                 练习裁判固定令牌（见练习包说明）；正式赛用网页显示的那串
  --team A             席位（默认 A）
  --name 队名          队名（默认 图灵队）
  --gateway URL        指定 AI 网关；不给就自动探测
  --domain 27          ROS_DOMAIN_ID（默认 27，必须与裁判机一致）
  --decision NAME      启动前写入策略：balanced / greedy / aggressive / conservative
  --build              强制重新 colcon build
  --dry-run            只打印，不执行
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --team) TEAM="${2:?}"; shift 2 ;;
    --name) NAME="${2:?}"; shift 2 ;;
    --gateway) GATEWAY="${2:?}"; shift 2 ;;
    --domain) DOMAIN="${2:?}"; shift 2 ;;
    --decision) DECISION="${2:?}"; shift 2 ;;
    --build) BUILD=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "未知参数: $1" >&2; usage; exit 2 ;;
    *) TOKEN="$1"; shift ;;
  esac
done

[ -n "$TOKEN" ] || TOKEN="${RMTC_TEAM_TOKEN:-}"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
err()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }

step "环境检查"
ROS_SETUP="${RMTC_ROS_SETUP:-/opt/ros/humble/setup.bash}"
if [ ! -f "$ROS_SETUP" ]; then
  err "没有 $ROS_SETUP：这台机器没装 ROS 2 Humble（或用 RMTC_ROS_SETUP 指定）"
  exit 2
fi
ok "ROS 2：$ROS_SETUP"

WS="$HERE/contestant_ws"
if [ ! -f "$WS/src/rmtc_bot/package.xml" ]; then
  err "找不到 $WS/src/rmtc_bot —— 请在解压后的 rmtc_bot_repo 目录里运行本脚本"
  exit 2
fi
ok "工作空间 $WS"

if ! command -v colcon >/dev/null 2>&1; then
  err "没有 colcon：sudo apt install -y python3-colcon-common-extensions"
  exit 2
fi

# ---- 选网关：先看有线直连，再看本机隧道 ----
probe() { timeout 3 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }
if [ -z "$GATEWAY" ]; then
  step "选择 AI 网关"
  if probe 192.168.77.1 8080; then
    GATEWAY="http://192.168.77.1:8080/v1/chat/completions"
    ok "有线直连（转发/监听可达）：$GATEWAY"
  elif probe 127.0.0.1 8080; then
    GATEWAY="http://127.0.0.1:8080/v1/chat/completions"
    ok "本机隧道：$GATEWAY"
  else
    warn "8080 都连不通（裁判没起 / 隧道没建 / 转发没开）"
    GATEWAY="http://192.168.77.1:8080/v1/chat/completions"
    warn "先用默认：$GATEWAY"
  fi
else
  ok "网关（手动指定）：$GATEWAY"
fi

if [ -n "$TOKEN" ]; then ok "令牌已提供（长度 ${#TOKEN}）"; else warn "没有令牌：第二阶段 AI 会被拒（401）"; fi

if [ -n "$DECISION" ]; then
  case "$DECISION" in
    greedy|aggressive|conservative|balanced) ;;
    *) err "未知策略：$DECISION（可选 balanced/greedy/aggressive/conservative）"; exit 2 ;;
  esac
  if [ "$DRY" = 1 ]; then
    echo "  (dry-run) 将把 decision_config.json 写成 $DECISION"
  else
    printf '{\n  "decision_type": "%s"\n}\n' "$DECISION" > "$HERE/decision_config.json"
    ok "决策档位已写入：$DECISION"
  fi
fi

step "准备启动"
echo "  席位 : $TEAM"
echo "  队名 : $NAME"
echo "  域   : $DOMAIN"
echo "  网关 : $GATEWAY"
echo "  策略 : ${DECISION:-decision_config.json 现有值}"
echo "  工作区: $WS"

if [ "$DRY" = 1 ]; then
  echo "  (dry-run) 不执行"
  exit 0
fi

# shellcheck disable=SC1091
source "$ROS_SETUP"
export ROS_DOMAIN_ID="$DOMAIN"
export ROS_LOCALHOST_ONLY=0
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp

if [ "$BUILD" = 1 ] || [ ! -f "$WS/install/setup.bash" ]; then
  step "编译 rmtc_bot"
  ( cd "$WS" && colcon build --symlink-install --packages-select rmtc_bot ) || {
    err "编译失败：看上面第一条 error"; exit 2; }
fi
# shellcheck disable=SC1091
source "$WS/install/setup.bash"

export RMTC_TEAM_TOKEN="$TOKEN"
export RMTC_AI_URL="$GATEWAY"

step "启动（Ctrl+C 结束）"
exec ros2 run rmtc_bot rmtc_bot --ros-args \
  -p "team:=$TEAM" \
  -p "team_name:=$NAME" \
  -p "ai_gateway:=$GATEWAY" \
  -p "decision_config:=$HERE/decision_config.json"
