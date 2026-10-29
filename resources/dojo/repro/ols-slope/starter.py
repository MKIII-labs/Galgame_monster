# 任务：读取同目录下的 data.csv，计算 y 对 x 的最小二乘斜率 a。
# 可以用标准库，也可以用已安装的 numpy / scipy。
# 把结果打印成一行 JSON，例如：{"slope": 2.003}

import csv
import json

xs, ys = [], []
with open("data.csv", newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        xs.append(float(row["x"]))
        ys.append(float(row["y"]))

# TODO: 计算斜率
slope = 0.0

print(json.dumps({"slope": slope}))
