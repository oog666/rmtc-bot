# 常用任务（需要先装 ROS 2 Humble）
ROS_SETUP ?= /opt/ros/humble/setup.bash
WS := contestant_ws

.PHONY: help build clean test sim1 sim2 sim10 maps experiment llm-test render

help:
	@echo "make build       编译 rmtc_bot"
	@echo "make sim1        一阶段练习图跑一局"
	@echo "make sim2        二阶段练习图跑一局"
	@echo "make sim10       十张实验图批量跑（balanced）"
	@echo "make maps        重新生成并校验十张地图"
	@echo "make experiment  4 策略 × 10 图 × 3 种子"
	@echo "make llm-test    LLM 请求体断言"
	@echo "make render      生成十图 HTML 总览"
	@echo "make clean       清理构建产物"

build:
	bash -lc 'source $(ROS_SETUP) && cd $(WS) && colcon build --symlink-install --packages-select rmtc_bot'

clean:
	rm -rf $(WS)/build $(WS)/install $(WS)/log
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

sim1:
	python3 tools/sim.py --stage 1 --preset practice --matches 1

sim2:
	python3 tools/sim.py --stage 2 --preset practice --matches 1 --verbose

sim10:
	python3 tools/sim.py --maps maps --decision balanced

maps:
	python3 tools/make_maps.py && python3 tools/validate_maps.py

experiment:
	python3 tools/experiment.py --seeds 3

llm-test:
	python3 tools/test_llm_payload.py

render:
	python3 tools/render_maps.py

test: llm-test maps
	python3 -m compileall -q contestant_ws/src/rmtc_bot tools
