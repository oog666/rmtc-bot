#!/usr/bin/env bash
# 构建"提交版"参赛镜像：代码打进去 + 预先 colcon build + 冒烟测试 + 导出 + 校验和
#
#   ./docker/make_submit_image.sh                    # 构建 + 冒烟 + 导出 RMTC_参赛镜像_v1.1.1.tar.gz
#   ./docker/make_submit_image.sh --no-save          # 只构建与冒烟
#   ./docker/make_submit_image.sh --base docker.xuanyuan.run/library/ros:humble-ros-base
#   ./docker/make_submit_image.sh --dry-run          # 只打印命令
#
# 产物：当前目录下 RMTC_参赛镜像_v1.1.1.tar.gz 与 .sha256（约 300–600 MB，gzip 后）
set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${RMTC_SUBMIT_IMAGE:-rmtc/contestant:v1.1}"
TAR="${RMTC_SUBMIT_TAR:-RMTC_参赛镜像_v1.1.1.tar.gz}"
BASE=""
SAVE=1
DRY=0
DOCKER=(docker)
[ "${RMTC_DOCKER_SUDO:-0}" = "1" ] && DOCKER=(sudo docker)

usage() {
  cat <<'EOF'
用法: ./docker/make_submit_image.sh [选项]

  --image TAG     产出镜像标签（默认 rmtc/contestant:v1.1）
  --tar FILE      导出文件名（默认 RMTC_参赛镜像_v1.1.1.tar.gz）
  --base IMAGE    指定基础镜像（默认 Dockerfile 内 ros:humble-ros-base）
  --no-save       不导出 tar.gz（只构建与冒烟）
  --dry-run       只打印命令，不执行
  -h, --help      帮助
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="${2:?}"; shift 2 ;;
    --tar) TAR="${2:?}"; shift 2 ;;
    --base) BASE="${2:?}"; shift 2 ;;
    --no-save) SAVE=0; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

run() {
  if [ "$DRY" -eq 1 ]; then printf '+ %s\n' "$*"; else "$@"; fi
}

if [ "$DRY" -eq 0 ]; then
  command -v docker >/dev/null 2>&1 || { echo "未找到 docker 命令" >&2; exit 2; }
  "${DOCKER[@]}" info >/dev/null 2>&1 || {
    cat >&2 <<'EOF'
当前用户无法访问 Docker（/var/run/docker.sock 权限不足）。二选一：
  A) 推荐：sudo usermod -aG docker "$USER" && newgrp docker   # 之后重开终端
  B) 临时：RMTC_DOCKER_SUDO=1 ./docker/make_submit_image.sh
EOF
    exit 2
  }
fi

BUILD_ARGS=()
[ -n "$BASE" ] && BUILD_ARGS+=(--build-arg "BASE_IMAGE=$BASE")

echo "== 1/4 构建 $IMAGE（context=$REPO）"
run "${DOCKER[@]}" build -f "$REPO/docker/Dockerfile.submit" -t "$IMAGE" "${BUILD_ARGS[@]}" "$REPO"

echo "== 2/4 冒烟：包路径 + 模块导入"
run "${DOCKER[@]}" run --rm "$IMAGE" bash -lc \
  'source /opt/ros/humble/setup.bash && source /ws/contestant_ws/install/setup.bash && ros2 pkg prefix rmtc_bot && python3 -c "import rmtc_bot.protocol as p; print(\"protocol ok\")"'

echo "== 3/4 冒烟：决策枚举与参数文件"
run "${DOCKER[@]}" run --rm "$IMAGE" bash -lc \
  'python3 -c "import json;d=json.load(open(\"/ws/decision_config.json\"));print(\"decision_type =\",d[\"decision_type\"])" && ls -l /ws/decision_config.json'

echo "== 4/4 导出与校验和"
if [ "$SAVE" -eq 1 ]; then
  if [ "$DRY" -eq 1 ]; then
    printf '+ %s\n' "${DOCKER[*]} save $IMAGE | gzip -1 > $TAR"
    printf '+ %s\n' "sha256sum $TAR | tee $TAR.sha256"
  else
    "${DOCKER[@]}" save "$IMAGE" | gzip -1 > "$TAR"
    sha256sum "$TAR" | tee "$TAR.sha256"
    echo "体积：$(du -h "$TAR" | cut -f1)"
  fi
else
  echo "（--no-save：跳过导出）"
fi

cat <<EOF

完成。交付前检查：
  1) 镜像内不应有令牌：docker run --rm $IMAGE bash -lc 'env | grep -i -E "token|key" || echo clean'
  2) 换机器导入：gunzip -c $TAR | docker load
  3) 导入后跑一次：docker run --rm --network host $IMAGE /ws/run_contestant.sh A 队名
EOF
