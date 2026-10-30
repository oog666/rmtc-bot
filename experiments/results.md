# 十图实验数据

| 地图 | greedy | aggressive | conservative | balanced | 最优 |
| --- | --- | --- | --- | --- | --- |
| 01-open-field | 40.8 | 40.0 | 38.3 | 52.5 | balanced |
| 02-center-keep | 35.0 | 41.7 | 38.3 | 43.3 | balanced |
| 03-quadrants | 26.7 | 40.0 | 26.7 | 35.0 | aggressive |
| 04-ring-corridor | 45.0 | 17.5 | 42.5 | 58.3 | balanced |
| 05-twin-towers | 32.5 | 30.0 | 30.8 | 44.2 | balanced |
| 06-zigzag | 20.0 | 33.3 | 26.7 | 38.3 | balanced |
| 07-rich-center | 45.0 | 27.5 | 23.3 | 31.7 | greedy |
| 08-edge-resources | 35.0 | 48.5 | 27.5 | 60.0 | balanced |
| 09-two-lanes | 30.8 | 39.0 | 31.7 | 44.2 | balanced |
| 10-maze-fragments | 17.5 | 32.2 | 17.5 | 29.2 | aggressive |

| 策略 | 十图均分 | 最高 | 最低 | 平均资源 | 平均伤害 | 平均任务 | 最长无效停滞 | 攻击次数 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| greedy | 32.83 | 45.0 | 17.5 | 82 | 0 | 2.83 | 2 | 0 |
| aggressive | 34.97 | 50.4 | 17.5 | 38 | 1017 | 2.17 | 1 | 1495 |
| conservative | 30.33 | 45.0 | 7.5 | 65 | 0 | 2.83 | 1 | 0 |
| balanced | 43.67 | 60.0 | 20.0 | 63 | 974 | 2.67 | 2 | 1379 |
