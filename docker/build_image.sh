#!/usr/bin/env bash
# 构建裁判/比赛共用镜像 —— 默认走轩辕镜像，单站 5 秒探测，不可用自动切换
#
#   ./docker/build_image.sh                    # 轩辕优先，自动切换备选
#   ./docker/build_image.sh --daemon-mirror    # 顺便把轩辕设为 Docker 默认加速器
#   ./docker/build_image.sh --mirror docker.xuanyuan.run --login
#   ./docker/build_image.sh --base docker.m.daocloud.io/library/ros:humble-ros-base
#   ./docker/build_image.sh --tar rmtc-humble-1.0.tar.gz
#
# 说明：国内直连 registry-1.docker.io / auth.docker.io 会超时，
#       本脚本先用 curl 探测各镜像站能否供图，再对第一个可用站执行 docker pull。

set -uo pipefail

ORIG_ARGS=("$@")

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${RMTC_IMAGE:-rmtc/humble:1.0}"
REPO_NAME="${RMTC_BASE_REPO:-library/ros}"
TAG="${RMTC_BASE_TAG:-humble-ros-base}"

BASE=""
APT_MIRROR=""
ROS_MIRROR=""
TAR=""
DRY=0
FORCE=0
PROBE_RETRY="${RMTC_PROBE_RETRY:-0}"
PROBE_TIMEOUT="${RMTC_PROBE_TIMEOUT:-5}"
DAEMON_MIRROR=0
LOGIN=0
MIRRORS_OVERRIDE=""

# 轩辕第一；其后是实测可用的备选（2026-10 本机实测 daocloud / 1ms 可正常供图）
DEFAULT_MIRRORS=(
  "docker.xuanyuan.me"
  "docker.m.daocloud.io"
  "docker.1ms.run"
  "dockerproxy.net"
)

usage() {
  cat <<'EOF'
用法: ./docker/build_image.sh [选项]

  --daemon-mirror        把轩辕写入 Docker 守护进程加速器（首选项），并重启 docker
  --mirror HOSTS         自定义候选站，逗号分隔；第一个可用者用于构建
  --login                docker login docker.xuanyuan.run（需要轩辕账号）
  --base IMAGE           直接指定基础镜像，跳过探测
  --apt-mirror HOST      Ubuntu 源镜像，如 mirrors.tuna.tsinghua.edu.cn
  --ros-mirror URL       ROS 2 源，如 https://mirrors.tuna.tsinghua.edu.cn/ros2/ubuntu
  --tar FILE             离线镜像包（docker save 产物）
  --image TAG            产出镜像名（默认 rmtc/humble:1.0）
  --probe-timeout SEC    单站探测超时（默认 5）
  --retry N              限流/超时时补试次数（默认 0；抢轩辕免费节点可设 3~5）
  --force                即使镜像已存在也重建
  --dry-run              只打印命令
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="${2:?}"; shift 2 ;;
    --mirror) MIRRORS_OVERRIDE="${2:?}"; shift 2 ;;
    --apt-mirror) APT_MIRROR="${2:?}"; shift 2 ;;
    --ros-mirror) ROS_MIRROR="${2:?}"; shift 2 ;;
    --tar) TAR="${2:?}"; shift 2 ;;
    --image) IMAGE="${2:?}"; shift 2 ;;
    --probe-timeout) PROBE_TIMEOUT="${2:?}"; shift 2 ;;
    --retry) PROBE_RETRY="${2:?}"; shift 2 ;;
    --daemon-mirror) DAEMON_MIRROR=1; shift ;;
    --login) LOGIN=1; shift ;;
    --force) FORCE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
err()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }
info() { printf '  %s\n' "$*"; }

# ------------------------------------------------------------------ docker
DOCKER_BIN="$(command -v docker 2>/dev/null)"
[ -n "$DOCKER_BIN" ] || DOCKER_BIN="/snap/bin/docker"
DOCKER=("$DOCKER_BIN")
if [ "${RMTC_DOCKER_SUDO:-0}" = "1" ]; then DOCKER=(sudo docker); fi
if ! "${DOCKER[@]}" info >/dev/null 2>&1 && [ "${DOCKER[0]}" = "$DOCKER_BIN" ]; then
  if sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo docker)
    export RMTC_DOCKER_SUDO=1
  fi
fi
DOCKER_OK=1
if ! "${DOCKER[@]}" info >/dev/null 2>&1; then
  DOCKER_OK=0
  # 需要 root 才能访问 /var/run/docker.sock：自动用 sudo 重跑（终端里只提示一次密码）
  if [ "$DRY" = 0 ] && [ "$(id -u)" != "0" ] && [ -t 0 ]; then
    warn "当前用户不能直接访问 Docker，改用 sudo 重新运行（会提示输入一次密码）"
    exec sudo -- "$0" "${ORIG_ARGS[@]}"
  fi
  if [ "$DRY" = 1 ]; then
    warn "docker 暂不可用（dry-run 模式下继续）"
  else
    err "docker 不可用：当前用户不在 docker 组，且尚未缓存 sudo 凭据"
    err "  在终端里直接运行本脚本即可（会自动 sudo 提权），或先执行：sudo docker ps"
    err "  或加入 docker 组：sudo usermod -aG docker $USER && newgrp docker"
    exit 2
  fi
fi
if [ "$DOCKER_OK" = 1 ] && "${DOCKER[@]}" info --format '{{json .SecurityOptions}}' 2>/dev/null | grep -q rootless; then
  warn "检测到 rootless Docker：--network host 语义受限，建议改用 rootful"
fi

SUDO=""
if [ "$(id -u)" != "0" ]; then SUDO="sudo"; fi

# --------------------------------------------------------------- 守护进程加速器
daemon_config_path() {
  if [ -f /var/snap/docker/current/config/daemon.json ]; then
    echo "/var/snap/docker/current/config/daemon.json"
  else
    echo "/etc/docker/daemon.json"
  fi
}

restart_docker() {
  if [ -f /var/snap/docker/current/config/daemon.json ]; then
    $SUDO snap restart docker
  else
    $SUDO systemctl restart docker
  fi
}

configure_daemon_mirror() {
  local cfg="$1" backup mirrors tmp_json
  mirrors='["https://docker.xuanyuan.me","https://docker.m.daocloud.io","https://docker.1ms.run"]'
  step "把轩辕镜像写入 Docker 加速器"
  info "配置文件：$cfg"
  info "镜像列表：$mirrors"
  warn "重启 docker 会停止正在运行的容器（确认当前没有比赛进程）"

  if [ "$DRY" = 1 ]; then
    info "(dry-run) 合并 registry-mirrors 并重启 docker"
    return 0
  fi

  backup="${cfg}.bak.$(date +%Y%m%d%H%M%S)"
  if $SUDO cp -a "$cfg" "$backup" 2>/dev/null; then ok "已备份 $backup"; fi

  tmp_json="$(mktemp)"
  cat >"$tmp_json" <<'PYEOF'
import json, sys

path, mirrors_json = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
data["registry-mirrors"] = json.loads(mirrors_json)
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=4, ensure_ascii=False)
    fh.write("\n")
print(json.dumps(data, ensure_ascii=False))
PYEOF
  if $SUDO /usr/bin/python3 "$tmp_json" "$cfg" "$mirrors"; then
    ok "daemon.json 已更新"
  else
    err "写入失败（权限或 JSON 问题）"
    rm -f "$tmp_json"
    return 1
  fi
  rm -f "$tmp_json"

  if restart_docker; then
    ok "docker 已重启"
  else
    err "docker 重启失败，检查：sudo snap logs docker -n 30"
    return 1
  fi
  local i ready=0
  for i in $(seq 1 15); do
    if "${DOCKER[@]}" info >/dev/null 2>&1; then ready=1; break; fi
    sleep 1
  done
  if [ "$ready" = 0 ]; then
    err "docker 重启后 15 秒仍不可用，检查：sudo snap logs docker -n 30"
    return 1
  fi
  ok "docker 已就绪"
  local shown
  shown="$("${DOCKER[@]}" info 2>/dev/null | grep -A3 -i "Registry Mirrors" | tail -3 | tr '\n' ' ')"
  if [ -n "$shown" ]; then ok "生效：$shown"; else warn "未读到 Registry Mirrors（部分版本仅在 pull 日志里显示）"; fi
}

if [ "$DAEMON_MIRROR" = 1 ]; then
  configure_daemon_mirror "$(daemon_config_path)" || exit 2
fi

# ------------------------------------------------------------------ 登录轩辕
if [ "$LOGIN" = 1 ]; then
  step "登录轩辕镜像（docker.xuanyuan.run）"
  info "账号为轩辕镜像官网注册的用户名/密码；成功后凭据存于 ~/.docker/config.json"
  if [ "$DRY" = 1 ]; then
    info "(dry-run) ${DOCKER[*]} login docker.xuanyuan.run"
  else
    "${DOCKER[@]}" login docker.xuanyuan.run || { err "登录失败"; exit 2; }
    ok "已登录"
  fi
  [ -z "$BASE" ] && BASE="docker.xuanyuan.run/${REPO_NAME}:${TAG}"
fi

# ------------------------------------------------------------------ 离线导入
if [ -n "$TAR" ]; then
  step "离线导入 $TAR"
  [ -f "$TAR" ] || { err "文件不存在"; exit 2; }
  if [ "${TAR##*.}" = "gz" ]; then
    gunzip -c "$TAR" | "${DOCKER[@]}" load || { err "docker load 失败"; exit 2; }
  else
    "${DOCKER[@]}" load -i "$TAR" || { err "docker load 失败"; exit 2; }
  fi
  ok "已导入"
fi

# ------------------------------------------------------- 已有镜像直接复用
if [ "$FORCE" = 0 ] && [ "$DOCKER_OK" = 1 ] \
   && "${DOCKER[@]}" image inspect "$IMAGE" >/dev/null 2>&1; then
  step "镜像已存在：$IMAGE"
  if "${DOCKER[@]}" run --rm --network host "$IMAGE" bash -lc \
      'source /opt/ros/humble/setup.bash; python3 -c "import rclpy, requests; print(\"deps ok\")"' >/dev/null 2>&1; then
    ok "自检通过，跳过构建（要重建加 --force）"
    exit 0
  fi
  warn "镜像存在但自检失败，重新构建"
fi

# ------------------------------------------------------------------ 探测/选站
# 输出：ok / auth / busy / timeout / deny / httpNNN
probe_mirror() {
  local host="$1" url body code tmp attempt max
  url="https://${host}/v2/${REPO_NAME}/manifests/${TAG}"
  code=""
  body=""
  max=$((PROBE_RETRY + 1))
  attempt=0
  while [ "$attempt" -lt "$max" ]; do
    attempt=$((attempt + 1))
    tmp="$(mktemp)"
    # 先强制 IPv4：本机 IPv6 出网不稳，会把自己可用的站点误判成超时
    code="$(timeout "$PROBE_TIMEOUT" curl -4 -s -o "$tmp" -w '%{http_code}' \
        -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json' \
        "$url" 2>/dev/null)"
    body="$(head -c 400 "$tmp" 2>/dev/null)"
    rm -f "$tmp"
    # 明确可用/被拒 → 不再重试；超时或限流才按 --retry 补试
    case "$code" in
      000|"") [ "$attempt" -lt "$max" ] && sleep 1 && continue ;;
      403|429)
        if printf '%s' "$body" | grep -qi "TOOMANYREQUESTS\|繁忙\|rate"; then
          [ "$attempt" -lt "$max" ] && sleep 2 && continue
        fi ;;
    esac
    break
  done
  case "$code" in
    200) echo "ok" ;;
    401) echo "auth" ;;
    403|429)
      if printf '%s' "$body" | grep -qi "TOOMANYREQUESTS\|繁忙\|rate"; then echo "busy"; else echo "deny"; fi ;;
    000|"") echo "timeout" ;;
    *) echo "http${code}" ;;
  esac
}

explain() {
  case "$1" in
    ok) echo "可拉取" ;;
    auth) echo "可拉取（docker 自动取令牌）" ;;
    busy) echo "免费节点限流" ;;
    timeout) echo "连接超时" ;;
    deny) echo "拒绝访问" ;;
    *) echo "异常 $1" ;;
  esac
}

MIRRORS=()
if [ -n "$MIRRORS_OVERRIDE" ]; then
  IFS=',' read -r -a MIRRORS <<<"$MIRRORS_OVERRIDE"
else
  MIRRORS=("${DEFAULT_MIRRORS[@]}")
fi

if [ -z "$BASE" ]; then
  step "并行探测基础镜像来源（轩辕优先，单站 ${PROBE_TIMEOUT}s 超时）"
  probe_dir="$(mktemp -d)"
  idx=0
  for host in "${MIRRORS[@]}"; do
    ( probe_mirror "$host" >"$probe_dir/$idx" 2>/dev/null ) &
    idx=$((idx + 1))
  done
  wait
  idx=0
  for host in "${MIRRORS[@]}"; do
    st="$(cat "$probe_dir/$idx" 2>/dev/null)"
    [ -n "$st" ] || st="error"
    printf '  %-24s %-8s %s\n' "$host" "$st" "$(explain "$st")"
    if [ -z "$BASE" ] && { [ "$st" = "ok" ] || [ "$st" = "auth" ]; }; then
      BASE="${host}/${REPO_NAME}:${TAG}"
    fi
    idx=$((idx + 1))
  done
  rm -rf "$probe_dir"
fi

if [ -z "$BASE" ]; then
  err "候选镜像站均不可用。"
  info "1) 轩辕账号可用时：./docker/build_image.sh --mirror docker.xuanyuan.run --login"
  info "2) 先配守护进程加速器：./docker/build_image.sh --daemon-mirror"
  info "3) 有网机器 docker save 后拷来：./docker/build_image.sh --tar 包.tar.gz"
  exit 2
fi

# ------------------------------------------------------------------ 拉取
step "拉取基础镜像"
info "$BASE"
if [ "$DRY" = 1 ]; then
  info "(dry-run) ${DOCKER[*]} pull $BASE"
elif ! "${DOCKER[@]}" pull "$BASE"; then
  err "拉取失败：换站（--mirror）或登录轩辕账号（--login）后重试"
  exit 2
fi

# ------------------------------------------------------------------ 构建
step "构建镜像 $IMAGE"
BUILD_ARGS=(--build-arg "BASE_IMAGE=$BASE")
[ -n "$APT_MIRROR" ] && BUILD_ARGS+=(--build-arg "APT_MIRROR=$APT_MIRROR")
[ -n "$ROS_MIRROR" ] && BUILD_ARGS+=(--build-arg "ROS_MIRROR=$ROS_MIRROR")

info "基础镜像: $BASE"
[ -n "$APT_MIRROR" ] && info "Ubuntu 源: $APT_MIRROR"
[ -n "$ROS_MIRROR" ] && info "ROS 2 源 : $ROS_MIRROR"

CMD=("${DOCKER[@]}" build -t "$IMAGE" "${BUILD_ARGS[@]}" "$REPO/docker")
info "\$ ${CMD[*]}"
if [ "$DRY" -eq 1 ]; then info "(dry-run)"; exit 0; fi
"${CMD[@]}" || { err "构建失败：看上面的 apt/pip 报错行"; exit 2; }

# ------------------------------------------------------------------ 验证
step "验证"
"${DOCKER[@]}" run --rm --network host "$IMAGE" bash -lc \
  'source /opt/ros/humble/setup.bash; ros2 --version; python3 -c "import rclpy, requests; print(\"deps ok\")"' \
  || { err "镜像能构建但运行异常"; exit 2; }

ok "完成：$IMAGE"
