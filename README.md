# RMTC 自主决策机器人（ROS 2 Humble）

> 面向 RMTC 机甲大师图灵对决赛的完整自主决策程序：`9×9` 视野下边探索边规划，把规则计分公式折算成"分/步"的效用函数，用一次 Dijkstra 代价场统一比较所有候选目标。
> A fully autonomous ROS 2 contestant bot for the RMTC Turing Challenge: utility-based target selection, incremental occupancy mapping, and asynchronous LLM event understanding.

---

## 特性

- **统一效用，不写状态机**：资源、任务点、敌车、天使点、堡垒、未知区域全部折成"分/步"，每 Tick 取期望收益最高者；新增规则只改权重，不改控制流。
- **一次 Dijkstra 算全图**：四邻接代价场（24×24 约 0.2 ms），所有候选目标共用同一张距离表，才能横向比较。
- **异步大模型**：随机事件公告只交给 LLM 理解，后台单线程、单并发、4.5 s 超时、逐级降级、调用预算封顶；控制回路里没有网络。
- **四档决策类型**：`balanced / greedy / aggressive / conservative`，共享地图与资源记忆，现场只改一个字段即可切换（每场硬限 2 次）。
- **可复现的离线评测**：自带按规则复现的裁判（`tools/sim.py`）、10 张合规地图、4×10×3 共 120 局对照实验。
- **异常只降级不崩**：解析失败丢帧、策略异常退化为 `wait`、额度耗尽停请求、阵亡只发 `wait`。

## 实测（离线仿真，按规则复现）

| 场景 | 结果 |
| --- | --- |
| 第一阶段·练习图（100 Tick） | **40.00 / 40**（资源 200/200） |
| 第二阶段·练习图（500 Tick） | **47.50 / 60**（任务 3/3） |
| 十张合规图 × 3 种子（综合型） | **均分 43.67 / 60**，最高 60.00 |
| 同条件对照（贪婪 / 侵略 / 保守） | 32.83 / 34.97 / 30.33 |
| 无效停滞（四档） | ≤ 2 Tick |
| 单 Tick 决策耗时 | 3–5 ms（含一次代价场） |

> 仿真器是按规则文本复现的，不是官方引擎；绝对分数只用于相对比较。官方练习裁判端的链路（感知/动作/回执）已单独验证。

## 架构

```
/team_X/perception ──► protocol.py（字段/类型容错，失败丢帧）
        │
        ▼
  world.py  占据栅格 + 资源/敌车记忆 + 临时墙
        │
        ├──► brain.py  代价场（Dijkstra）──► collector.py（一阶段）
        │                              └──► battle.py / strategies/（二阶段）
        │                                        ├── combat.py 攻击收益
        │                                        └── events.py 事件约束
        ▼
/team_X/action ◄── protocol.py（回填 tick / session_id）

event_text ──► llm.py（后台线程，可降级）──► JSON 校验 ──► events.py
```

## 快速开始

要求：Ubuntu 22.04、ROS 2 Humble、Python 3.10+（仅标准库 + `rclpy`，无第三方依赖）。

```bash
sudo apt install ros-humble-rclpy ros-humble-std-msgs ros-humble-rmw-fastrtps-cpp

source /opt/ros/humble/setup.bash
cd contestant_ws
colcon build --symlink-install --packages-select rmtc_bot
source install/setup.bash
ros2 pkg prefix rmtc_bot

export ROS_DOMAIN_ID=27
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RMTC_TEAM_TOKEN='<本场令牌>'
export RMTC_AI_URL='http://<裁判机IP>:8080/v1/chat/completions'
cd .. && ./run_contestant.sh A '<队名>'
```

或使用启动器（自动选网关、必要时编译）：

```bash
./start_bot.sh --team A --name '<队名>' --gateway http://<裁判机IP>:8080/v1/chat/completions
```

| 环境变量 | 作用 | 默认 |
| --- | --- | --- |
| `ROS_DOMAIN_ID` | ROS 域编号 | `27` |
| `RMTC_TEAM_TOKEN` | 本场令牌（AI 鉴权） | 空（AI 降级） |
| `RMTC_AI_URL` | AI 网关完整地址 | `http://127.0.0.1:8080/v1/chat/completions` |
| `RMTC_MODEL` | 指定模型名 | `deepseek-flash` |
| `RMTC_DECISION_CONFIG` | 决策档位文件 | `./decision_config.json` |
| `RMTC_LLM_BUDGET` | 每场 LLM 真实 HTTP 次数上限 | `80` |

## 现场只改一个文件

```json
{"decision_type": "balanced"}
```

| 档位 | 行为 | 十图均分 |
| --- | --- | --- |
| `balanced` | 资源·任务·对抗统一打分（默认） | **43.67** |
| `greedy` | 只采集 + 任务，从不主动攻击 | 32.83 |
| `aggressive` | 主动接敌，资源只捡近处 | 34.97 |
| `conservative` | 拉开距离、边躲边搜 | 30.33 |

程序每 2 s 读一次文件 `mtime`，值变化即热切换，**记忆不清空**；每场硬限 2 次。不做任何自动切档——规则只允许人工申请 + 裁判登记。

## 离线工具

```bash
python3 tools/validate_maps.py                                   # 10 张地图合规校验
python3 tools/sim.py --stage 1 --preset practice --matches 1     # 一阶段
python3 tools/sim.py --stage 2 --preset practice --matches 1     # 二阶段
python3 tools/sim.py --maps maps --decision balanced             # 十图批量
python3 tools/experiment.py --seeds 3                            # 4×10×3 对照实验
python3 tools/render_maps.py                                     # 生成 HTML 总览
python3 tools/test_llm_payload.py                                # LLM 请求体断言（20 项）
```

ROS 链路自测（两个终端）：

```bash
python3 tools/fake_referee.py --team A --stage 2 --total 60 --tick-ms 200
PYTHONPATH=contestant_ws/src/rmtc_bot python3 -m rmtc_bot.node --ros-args -p team:=A
```

## 目录结构

```
.
├── contestant_ws/src/rmtc_bot/   # ROS 2 功能包（参赛程序本体）
│   ├── rmtc_bot/                 # 协议/世界/战斗/事件/LLM/采集/四策略/中枢/节点/参数
│   ├── launch/  config/
├── tools/                        # 离线仿真、批量实验、地图生成与校验、假裁判、网关替身
├── docker/                       # 容器镜像（开发用 + 提交用）
├── maps/                         # 10 张 24×24 合规地图（geometry + practice 两种格式）
├── experiments/                  # 120 局对照实验原始结果
├── docs/                         # 代码说明、策略说明、算法图解、实验报告
├── decision_config.json          # 现场唯一允许修改的文件
├── run_contestant.sh             # 一键构建 + 启动
└── start_bot.sh                  # 比赛机启动器
```

## 设计要点

1. **计分即目标函数**：资源 `30×min(R/120,1)`、对抗 `15×min(D/500,1)`、任务 `5×M`；封顶后边际收益为 0，权重直接压到 0.12/0.35。
2. **死亡是最贵的动作**：一次阵亡扣 60 有效伤害（≈1.8 分）+ 4 Tick 复活，所以 `HP≤28` 且不能斩杀时禁止换血，改防御（20→10）。
3. **事件只做约束**：LLM 输出经白名单校验后编译成禁区/时限/增益窗口；模型只能调整 ±22% 权重，不能直接给动作。
4. **防循环三层**：目标 5 Tick 无进展拉黑 22 Tick；移动失败换向 3 Tick；12 Tick 内只在 ≤2 格往返则强制挪窝。
5. **合规边界**：只订阅本席位感知、只发本队动作；不读全图/网页/练习配置；事件文本不在本地解析。

## 已知限制

- 仿真器为按规则复现，非官方引擎，绝对分数仅供相对比较。
- 随机事件理解依赖 LLM；额度耗尽后事件不响应、任务分按 0 计，程序仍可跑完。
- 任务类型覆盖 5 类中的 4 类（`resource_value` 未单独加权）。
- 仅在 Ubuntu 22.04 + ROS 2 Humble 上验证；跨机需同域、同网段，Docker 需 `--network host`。

## 文档

| 文档 | 内容 |
| --- | --- |
| [`docs/代码说明.md`](docs/代码说明.md) | 模块职责、算法细节、参数表、常见问题 |
| [`docs/三种策略说明.md`](docs/三种策略说明.md) | 四档人格的阈值、事件响应与实测 |
| [`docs/算法图解.html`](docs/算法图解.html) | 决策流程图示 |
| [`docs/十图实验与规则微调报告.md`](docs/十图实验与规则微调报告.md) | 120 局实验设计与结论 |
| [`docs/十图实验总览.html`](docs/十图实验总览.html) | 十图可视化总览 |

## 许可

本仓库未附开源许可证（默认保留所有权利）。如用于课程作业或竞赛复现，请注明来源。
