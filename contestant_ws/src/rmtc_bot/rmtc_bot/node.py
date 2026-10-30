"""ROS 2 节点：只做"搬运"，不做策略。

* 订阅 /team_X/perception（RELIABLE / VOLATILE / KEEP_LAST(1)）；
* 发布 /team_X/action（同样 QoS），每个 Tick 至多一条；
* 发布 /team_X/registration（TRANSIENT_LOCAL 锁存队名）；
* 决策在回调里同步完成（亚毫秒级），LLM 请求在后台线程，互不阻塞。

现场只需要改环境变量（席位/域/令牌/网关），不需要改代码。
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

from .brain import Brain
from .config import DECISION_TYPES, DEFAULT_DECISION, Params
from .llm import LLMClient
from .protocol import encode_action
from .strategies import ALIASES


def _load_decision(path: str) -> str:
    """规则 §2.3：现场只允许改 decision_type，枚举必须已登记。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        val = obj.get("decision_type") if isinstance(obj, dict) else None
        if isinstance(val, str):
            val = ALIASES.get(val, val)
            if val in DECISION_TYPES:
                return val
    except (OSError, ValueError):
        pass
    return DEFAULT_DECISION


class RmtcBot(Node):
    def __init__(self) -> None:
        super().__init__("rmtc_bot")

        self.declare_parameter("team", os.environ.get("RMTC_TEAM", "A"))
        self.declare_parameter("team_name", os.environ.get("RMTC_TEAM_NAME", "图灵队"))
        self.declare_parameter("ai_gateway", os.environ.get(
            "RMTC_AI_URL", "http://127.0.0.1:8080/v1/chat/completions"))
        self.declare_parameter("ai_model", os.environ.get("RMTC_MODEL", ""))
        self.declare_parameter("decision_config", os.environ.get(
            "RMTC_DECISION_CONFIG", os.path.join(os.getcwd(), "decision_config.json")))
        self.declare_parameter("log_every", 25)

        team = str(self.get_parameter("team").value).strip().upper()
        if team not in ("A", "B", "C", "D"):
            self.get_logger().error(f"非法席位 {team!r}，回退为 A")
            team = "A"
        self.team = team
        self.team_name = str(self.get_parameter("team_name").value)
        gateway = str(self.get_parameter("ai_gateway").value)
        model = str(self.get_parameter("ai_model").value)
        token = os.environ.get("RMTC_TEAM_TOKEN", "").strip()
        decision_path = str(self.get_parameter("decision_config").value)
        self.decision = _load_decision(decision_path)
        self.log_every = int(self.get_parameter("log_every").value)

        qos_data = QoSProfile(
            depth=1,
            history=HistoryPolicy.KEEP_LAST,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        qos_latch = QoSProfile(
            depth=1,
            history=HistoryPolicy.KEEP_LAST,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )

        self.pub_action = self.create_publisher(String, f"/team_{team}/action", qos_data)
        self.pub_reg = self.create_publisher(String, f"/team_{team}/registration", qos_latch)
        self.sub = self.create_subscription(
            String, f"/team_{team}/perception", self._on_perception, qos_data)

        self.params = Params()
        self.llm: Optional[LLMClient] = None
        if gateway and token:
            self.llm = LLMClient(
                gateway, token,
                model=model or self.params.llm_model,
                thinking_disabled=self.params.llm_disable_thinking,
                json_mode=self.params.llm_json_mode,
                max_tokens=self.params.llm_max_tokens,
                temperature=self.params.llm_temperature,
            )
            self.llm.start()
        else:
            self.get_logger().warn("未配置 AI 网关或令牌，事件理解将降级（不影响移动/攻击）")

        self.brain = Brain(self.params, decision=self.decision, llm=self.llm,
                           logger=self.get_logger().info)

        self.sent = 0
        self.max_latency = 0.0
        self._last_log_tick = -999
        self._reg_timer = self.create_timer(3.0, self._publish_registration)
        self._publish_registration()

        # 策略热切换：只监听 decision_config.json 的 decision_type，记忆不清空
        self._decision_path = decision_path
        self._decision_mtime = self._file_mtime(decision_path)
        self._switch_count = 0
        self._last_session = ""
        self._watch_timer = self.create_timer(2.0, self._watch_decision)

        self.get_logger().info(
            f"席位 {team} | 队名 {self.team_name} | 决策档位 {self.decision} | "
            f"感知 /team_{team}/perception | 网关 {'已配置' if token else '未配置'}"
        )
        if self.llm is not None:
            self.get_logger().info(
                f"模型 {self.llm.model} | thinking="
                f"{'disabled' if self.params.llm_disable_thinking else 'default'} | "
                f"json_mode={'on' if self.params.llm_json_mode else 'off'} | "
                f"max_tokens={self.params.llm_max_tokens}"
            )

    # ------------------------------------------------------------------
    @staticmethod
    def _file_mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    def _watch_decision(self) -> None:
        """检测 decision_type 变化并热切换（规则：需裁判批准，仅暂停期间修改）。"""
        if self.brain.session and self.brain.session != self._last_session:
            self._last_session = self.brain.session
            self._switch_count = 0

        mtime = self._file_mtime(self._decision_path)
        if mtime == 0.0 or mtime == self._decision_mtime:
            return
        self._decision_mtime = mtime
        name = _load_decision(self._decision_path)
        if name == self.brain.current_decision:
            return
        if self._switch_count >= 2:
            self.get_logger().warn("本场已切换 2 次策略，忽略本次修改（规则 §2.3）")
            return
        if self.brain.set_strategy(name):
            self._switch_count += 1
            self.get_logger().info(
                f"策略切换 {self._switch_count}/2 → {name}（需在暂停期间并经裁判批准）")

    # ------------------------------------------------------------------
    def _publish_registration(self) -> None:
        payload = json.dumps({"team": self.team, "name": self.team_name}, ensure_ascii=False)
        self.pub_reg.publish(String(data=payload))

    def _on_perception(self, msg: String) -> None:
        started = time.time()
        try:
            action = self.brain.step(msg.data)
        except Exception as exc:                      # 任何异常都不能让节点退出
            self.get_logger().error(f"决策异常: {exc}")
            return

        latency = time.time() - started
        self.max_latency = max(self.max_latency, latency)
        if action is None:
            return

        kind, direction = action
        payload = encode_action(self.brain.tick, self.brain.session, kind, direction)
        self.pub_action.publish(String(data=payload))
        self.sent += 1

        if self.brain.tick - self._last_log_tick >= self.log_every:
            self._last_log_tick = self.brain.tick
            self.get_logger().info(
                f"{self.brain.status()} | 决策延迟 {latency * 1000:.1f}ms"
            )

    def destroy_node(self) -> bool:
        if self.llm is not None:
            stats = self.llm.stats()
            self.llm.stop()
            try:
                self.get_logger().info(f"LLM 统计: {stats}")
            except Exception:
                print(f"[rmtc_bot] LLM 统计: {stats}")
        return super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = RmtcBot()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, Exception) as exc:      # Ctrl+C / 上下文关闭都优雅退出
        if isinstance(exc, KeyboardInterrupt) or type(exc).__name__ == "ExternalShutdownException":
            pass
        else:
            node.get_logger().error(f"节点异常退出: {exc}")
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
