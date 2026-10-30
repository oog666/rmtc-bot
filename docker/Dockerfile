# RMTC 双角色镜像：比赛端与裁判端共用
# 基础镜像可替换（国内拉不到 Docker Hub 时用镜像站），见 docker/build_image.sh
ARG BASE_IMAGE=ros:humble-ros-base
FROM ${BASE_IMAGE}

# 可选：构建时换国内 apt 源（默认留空，不改变原行为）
ARG APT_MIRROR=
ARG ROS_MIRROR=

ENV DEBIAN_FRONTEND=noninteractive \
    ROS_DISTRO=humble \
    RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
    ROS_LOCALHOST_ONLY=0 \
    HOME=/tmp \
    ROS_HOME=/tmp/.ros

RUN set -eux; \
    if [ -n "${APT_MIRROR}" ] && [ -f /etc/apt/sources.list ]; then \
      sed -i "s|http://archive.ubuntu.com/ubuntu|http://${APT_MIRROR}/ubuntu|g; \
              s|http://security.ubuntu.com/ubuntu|http://${APT_MIRROR}/ubuntu|g" \
             /etc/apt/sources.list || true; \
    fi; \
    if [ -n "${ROS_MIRROR}" ] && [ -f /etc/apt/sources.list.d/ros2.list ]; then \
      sed -i "s|http://packages.ros.org/ros2/ubuntu|${ROS_MIRROR}|g" \
             /etc/apt/sources.list.d/ros2.list || true; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
      python3-colcon-common-extensions \
      python3-requests \
      ros-humble-statistics-msgs \
      ros-humble-libstatistics-collector \
      iproute2 \
      iputils-ping \
      net-tools \
      procps \
      ca-certificates \
      curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ws
CMD ["bash"]
