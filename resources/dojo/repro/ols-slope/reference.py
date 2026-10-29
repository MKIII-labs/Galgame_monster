# 参考解：只用于自检与生成 expected，不返回给前端。
import csv
import json

xs, ys = [], []
with open("data.csv", newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        xs.append(float(row["x"]))
        ys.append(float(row["y"]))

n = len(xs)
mx = sum(xs) / n
my = sum(ys) / n
num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
den = sum((x - mx) ** 2 for x in xs)
print(json.dumps({"slope": num / den}))
