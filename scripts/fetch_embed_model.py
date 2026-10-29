# -*- coding: utf-8 -*-
"""下载语义检索用的本地 embedding 模型（BAAI/bge-small-zh-v1.5，约 95MB，一次性）。

用法：
    python scripts/fetch_embed_model.py

模型会缓存到 `data/models/fastembed/`；之后离线也能用。
没下载时 search_code 会自动退回关键词检索，不会报错。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import codeindex          # noqa: E402


def main():
    try:
        from fastembed import TextEmbedding
    except Exception as e:
        print("缺少依赖 fastembed，请先运行：pip install fastembed\n%s" % e)
        return 1
    print("模型：%s" % codeindex.MODEL_NAME)
    print("缓存：%s" % codeindex.MODEL_CACHE)
    os.makedirs(codeindex.MODEL_CACHE, exist_ok=True)
    model = TextEmbedding(model_name=codeindex.MODEL_NAME,
                          cache_dir=codeindex.MODEL_CACHE)
    vec = list(model.embed(["warmup"]))[0]
    print("完成，向量维度 %d" % len(vec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
