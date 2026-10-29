"""CMC 历年真题 → 6 幕研究任务（写到 data/dojo/inquiries/cmc-papers）。

用法：
    python -m scripts.gen_paper_inquiries --limit 1
    python -m scripts.gen_paper_inquiries --only cmc2024a-q3
    python -m scripts.gen_paper_inquiries --all              # 全量（每卷 2 题）
    python -m scripts.gen_paper_inquiries --all --shard 1/3  # 并发分片

已存在的 item 会跳过，可中断后续跑。
"""
import argparse
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from app import llm, prompts
from app.config import get_api_conf
from app.dojo import authoring
from app.store import DATA_DIR, RESOURCES_DIR

COLLECTION = "cmc-papers"
OUT_DIR = os.path.join(DATA_DIR, "dojo", "inquiries", COLLECTION)
INDEX_PATH = os.path.join(RESOURCES_DIR, "dojo", "index", "cmc.json")


def load_index():
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def pick_two(paper):
    """每卷选 2 题：分值最高的数分 + 一个不同板块。

    A 卷选解析几何、B 卷选高等代数，避免 A/B 卷共用同一道高代题导致重复改编；
    2016–2018 的不分卷按届次奇偶交替。
    """
    probs = [p for p in paper.get("problems") or [] if isinstance(p.get("no"), int)]
    ana = sorted([p for p in probs if p.get("subject") == "数学分析"],
                 key=lambda p: (p.get("points") or 0, p["no"]), reverse=True)
    name = paper.get("paper") or ""
    if name == "数学B类":
        want = "高等代数"
    elif name == "数学A类":
        want = "解析几何"
    else:
        want = "解析几何" if int(paper.get("edition") or 0) % 2 == 0 else "高等代数"
    other = sorted([p for p in probs if p.get("subject") == want],
                   key=lambda p: ((p.get("points") or 0), -p["no"]), reverse=True)
    return ana[:1] + other[:1]


def build_messages(idx, paper, prob, item_id):
    system = prompts.part("dojo_paper", "system")
    user = prompts.part(
        "dojo_paper", "user",
        contest=idx.get("contest") or "CMC",
        year=paper.get("year") or "",
        edition=paper.get("edition") or "",
        paper=paper.get("paper") or "",
        problem=prob.get("no"),
        points=prob.get("points") or "",
        subject=prob.get("subject") or "",
        topic=prob.get("topic") or "",
        head=prob.get("head") or prob.get("topic") or "",
        url=paper.get("url") or "",
        item_id=item_id,
    )
    if not system or not user:
        raise SystemExit("提示词模板缺失：resources/prompts/dojo_paper.md")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def generate_one(idx, paper, prob):
    item_id = "%s-q%s" % (paper["id"], prob["no"])
    path = os.path.join(OUT_DIR, "items", item_id + ".json")
    if os.path.isfile(path):
        print("SKIP", item_id)
        return "skip"

    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        raise SystemExit("还没有配置聊天 API 密钥")

    messages = build_messages(idx, paper, prob, item_id)
    doc, last_err = None, None
    for attempt in (1, 2):
        res = llm.stream_chat(api_key, api_url, api_model, messages, allow_tool=False)
        visible = str(res.get("visible") or "").strip()
        if not visible:
            last_err = "模型没有返回内容"
            continue
        try:
            doc = authoring._extract_object(visible)
            doc["id"] = item_id
            doc["source"] = {
                "contest": idx.get("contest") or "CMC",
                "year": int(paper.get("year") or 0),
                "paper": paper.get("paper") or "",
                "problem": str(prob.get("no")),
                "url": paper.get("url") or "",
                "note": "改编自官方真题，题干为教学改写",
            }
            doc["track"] = "竞赛"
            for i, act in enumerate(doc.get("acts") or []):
                if isinstance(act, dict):
                    act["id"] = "%s-a%d" % (item_id, i + 1)
                    act.setdefault("minutes", 5)
            authoring._validate(doc)
            break
        except Exception as e:
            last_err = e
            doc = None
            if attempt == 1:
                repair = [{"role": "user", "content":
                           "这份草稿未通过校验：%s\n请只修正这些问题并重新输出完整 JSON；仍然必须 6 幕，不要解释。"
                           % e}]
                messages = messages + repair
    if doc is None:
        print("FAIL", item_id, last_err)
        return "fail"

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print("OK  ", item_id, doc.get("title"), "chars", len(json.dumps(doc, ensure_ascii=False)))
    return "ok"


def write_collection(idx):
    col = {
        "id": COLLECTION,
        "name": "CMC 数学类真题改编",
        "standard": idx.get("standard") or "",
        "note": "按年份 / 卷别 / 题号改编自全国大学生数学竞赛初赛数学专业类真题；题干为教学改写，考点与原题一致。",
        "tracks": ["竞赛"],
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "collection.json"), "w", encoding="utf-8") as f:
        json.dump(col, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--shard", default="", help="k/n，按序号取模分片，便于并发")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    idx = load_index()
    write_collection(idx)

    picked = []
    for paper in idx.get("papers") or []:
        for prob in pick_two(paper):
            picked.append((paper, prob))
    if args.only:
        picked = [(p, q) for p, q in picked if "%s-q%s" % (p["id"], q["no"]) == args.only]
    picked.sort(key=lambda x: (x[0]["year"], x[0]["paper"]), reverse=True)
    if args.shard:
        k, n = [int(x) for x in args.shard.split("/")]
        picked = [x for i, x in enumerate(picked) if i % n == k - 1]
    if args.limit:
        picked = picked[:args.limit]
    if not (args.all or args.limit or args.only):
        print("共 %d 道待改编；加 --limit N 或 --all 才会生成" % len(picked))
        return

    stat = {"ok": 0, "skip": 0, "fail": 0}
    for paper, prob in picked:
        r = generate_one(idx, paper, prob)
        stat[r] = stat.get(r, 0) + 1
        time.sleep(0.8)
    print("STAT", stat)


if __name__ == "__main__":
    main()
