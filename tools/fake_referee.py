"""本地假裁判：验证参赛节点的 ROS 链路（不参与比赛，也不替代官方裁判）。

它复用 tools/sim.py 的感知生成逻辑，按官方话题与 QoS 发布 /team_A/perception，
并订阅 /team_A/action 校验 tick / session_id 回填与动作合法性。

用法：
  # 终端 1
  source /opt/ros/humble/setup.bash   # 本机为 jazzy 也可
  python3 tools/fake_referee.py --team A
  # 终端 2
  PYTHONPATH=contestant_ws/src/rmtc_bot python3 -m rmtc_bot.node --ros-args -p team:=A
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

from sim import DIR_VEC, build_practice_sim


class FakeReferee(Node):
    def __init__(self, team: str = "A", tick_ms: int = 100, total: int = 100, stage: int = 1) -> None:
        super().__init__("fake_referee")
        self.team = team
        qos = QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                         reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.VOLATILE)
        self.pub = self.create_publisher(String, f"/team_{team}/perception", qos)
        self.sub = self.create_subscription(String, f"/team_{team}/action", self.on_action, qos)

        self.stage = stage
        self.sim = build_practice_sim(stage)
        self.sim.session = f"local-{int(time.time())}"
        self.robot = self.sim.robots[0]
        self.sim.robots = [self.robot]
        self.tick = 0
        self.total = total
        self.received = 0
        self.accepted = 0
        self.rejected = []
        self.moves = 0
        self.last_action = None
        self.pending = None
        self.published_tick = -1
        self.timer = self.create_timer(tick_ms / 1000.0, self.step)
        self.get_logger().info(f"假裁判就绪：/team_{team}/perception → /team_{team}/action，"
                               f"{tick_ms}ms/Tick，共 {total} Tick")

    def on_action(self, msg: String) -> None:
        self.received += 1
        try:
            obj = json.loads(msg.data)
        except ValueError:
            self.rejected.append(("E_PARSE", msg.data[:80]))
            return
        if not isinstance(obj, dict):
            self.rejected.append(("E_SCHEMA", msg.data[:80]))
            return
        if obj.get("tick") != self.published_tick:
            self.rejected.append(("E_TICK", f"got {obj.get('tick')} want {self.tick}"))
            return
        if obj.get("session_id") != self.sim.session:
            self.rejected.append(("E_SESSION", str(obj.get("session_id"))))
            return
        act = obj.get("action")
        if act not in ("move", "attack", "defend", "wait"):
            self.rejected.append(("E_SCHEMA", str(act)))
            return
        if act in ("move", "attack") and obj.get("direction") not in DIR_VEC:
            self.rejected.append(("E_SCHEMA", "direction"))
            return
        self.accepted += 1
        self.pending = obj

    def step(self) -> None:
        if self.tick >= self.total:
            self.timer.cancel()
            self.get_logger().info(
                f"结束：感知 {self.total} Tick | 收到动作 {self.received} | 合法 {self.accepted} | "
                f"移动 {self.moves} | 采集合计 {self.robot.collected_value} | 拒绝 {len(self.rejected)}")
            if self.rejected:
                self.get_logger().warn(f"前 5 条拒绝：{self.rejected[:5]}")
            self.get_logger().info(f"最终位置 {self.robot.pos}")
            return

        # 应用上一 Tick 的动作
        act = self.pending or {"action": "wait"}
        self.pending = None
        self.robot.last_action = None
        if act.get("action") == "move":
            vx, vy = DIR_VEC[act["direction"]]
            target = (self.robot.pos[0] + vx, self.robot.pos[1] + vy)
            blocked = target in self.sim.obstacles or not (
                0 <= target[0] < self.sim.w and 0 <= target[1] < self.sim.h)
            if not blocked:
                self.robot.pos = target
                self.robot.last_action = {"tick": self.tick - 1, "action": "move",
                                          "moved": True, "effective_direction": act["direction"]}
                self.moves += 1
            else:
                self.robot.last_action = {"tick": self.tick - 1, "action": "move",
                                          "moved": False, "error": "E_BLOCKED"}
        elif act.get("action"):
            self.robot.last_action = {"tick": self.tick - 1, "action": act["action"], "moved": False}

        for res in list(self.sim.resources):
            if res.pos == self.robot.pos:
                self.robot.collected_value += res.value
                self.sim.resources.remove(res)

        self.sim.tick = self.tick
        if self.stage == 2:
            self.sim._announce()
        self.pub.publish(String(data=self.sim.observation(self.robot)))
        self.published_tick = self.tick
        if self.tick % 20 == 0:
            self.get_logger().info(f"t={self.tick} pos={self.robot.pos} "
                                   f"R={self.robot.collected_value} moves={self.moves}")
        self.tick += 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", default="A")
    ap.add_argument("--tick-ms", type=int, default=100)
    ap.add_argument("--total", type=int, default=100)
    ap.add_argument("--stage", type=int, default=1)
    args = ap.parse_args()

    rclpy.init()
    node = FakeReferee(args.team, args.tick_ms, args.total, args.stage)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
