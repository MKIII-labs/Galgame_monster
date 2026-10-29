"""CUMCM 历年真题 → 6 步建模案例（写到 data/dojo/cases）。

用法：
    python -m scripts.gen_paper_cases --limit 1
    python -m scripts.gen_paper_cases --all
    python -m scripts.gen_paper_cases --all --shard 1/2

默认每年选 A 题；2023、2024 已有案例，自动跳过。
"""
import argparse
import json
import os
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")

from app import llm, prompts
from app.config import get_api_conf
from app.dojo import cases
from app.store import DATA_DIR, RESOURCES_DIR

OUT_DIR = os.path.join(DATA_DIR, "dojo", "cases")
INDEX_PATH = os.path.join(RESOURCES_DIR, "dojo", "index", "cumcm.json")


def load_index():
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def picked_problems(idx):
    rows = []
    for paper in idx.get("papers") or []:
        for prob in paper.get("problems") or []:
            rows.append((paper, prob))
    return rows


def build_messages(paper, prob, item_id):
    system = prompts.part("dojo_case", "system")
    user = prompts.part(
        "dojo_case", "user",
        year=paper.get("year") or "",
        problem=prob.get("no") or "",
        title=prob.get("title") or "",
        topic=prob.get("topic") or "",
        url=paper.get("url") or "",
        item_id=item_id,
    )
    if not system or not user:
        raise SystemExit("提示词模板缺失：resources/prompts/dojo_case.md")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def generate_one(paper, prob):
    item_id = "cumcm-%s%s" % (paper.get("year"), str(prob.get("no")).lower())
    path = os.path.join(OUT_DIR, item_id, "case.json")
    if os.path.isfile(path):
        print("SKIP", item_id)
        return "skip"

    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        raise SystemExit("还没有配置聊天 API 密钥")

    messages = build_messages(paper, prob, item_id)
    doc, last_err = None, None
    for attempt in (1, 2):
        res = llm.stream_chat(api_key, api_url, api_model, messages, allow_tool=False)
        visible = str(res.get("visible") or "").strip()
        if not visible:
            last_err = "模型没有返回内容"
            continue
        try:
            text = visible
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("没有找到 JSON 对象")
            doc = json.loads(text[start:end + 1])
            doc["id"] = item_id
            doc["subtitle"] = "改编自 %s 年 CUMCM %s 题" % (paper.get("year"), prob.get("no"))
            doc["grade"] = "official"
            doc["source"] = {
                "contest": "CUMCM",
                "year": int(paper.get("year") or 0),
                "problem": str(prob.get("no")),
                "topic": prob.get("title") or prob.get("topic") or "",
                "url": paper.get("url") or "",
                "note": "题干为教学改写，数值为合成算例，非官方附件数据",
            }
            for i, step in enumerate(doc.get("steps") or []):
                if isinstance(step, dict):
                    step["id"] = "%s-s%d" % (item_id, i + 1)
                    step.setdefault("minutes", 4)
            fd, tmp = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False)
            try:
                cases._load_one(tmp)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            break
        except Exception as e:
            last_err = e
            doc = None
            if attempt == 1:
                messages = messages + [{"role": "user", "content":
                                        "这份草稿未通过校验：%s\n请只修正这些问题并重新输出完整 JSON；仍然必须 6 步，不要解释。"
                                        % e}]
    if doc is None:
        print("FAIL", item_id, last_err)
        return "fail"

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print("OK  ", item_id, doc.get("title"), "chars", len(json.dumps(doc, ensure_ascii=False)))
    return "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--shard", default="", help="k/n，按序号取模分片")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    idx = load_index()
    rows = [(p, q) for p, q in picked_problems(idx) if str(q.get("no")) == "A"]
    rows = [(p, q) for p, q in rows if int(p.get("year") or 0) not in (2023, 2024)]
    if args.only:
        rows = [(p, q) for p, q in rows
                if "cumcm-%s%s" % (p.get("year"), str(q.get("no")).lower()) == args.only]
    rows.sort(key=lambda x: int(x[0].get("year") or 0))
    if args.shard:
        k, n = [int(x) for x in args.shard.split("/")]
        rows = [x for i, x in enumerate(rows) if i % n == k - 1]
    if args.limit:
        rows = rows[:args.limit]
    if not (args.all or args.limit or args.only):
        print("共 %d 道待生成：%s" % (len(rows), "、".join(
            "%s%s" % (p.get("year"), q.get("no")) for p, q in rows)))
        return

    stat = {}
    for paper, prob in rows:
        r = generate_one(paper, prob)
        stat[r] = stat.get(r, 0) + 1
        time.sleep(0.8)
    print("STAT", stat)


if __name__ == "__main__":
    main()
