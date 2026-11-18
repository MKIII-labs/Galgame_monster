# -*- coding: utf-8 -*-
"""知识库节点类型：老数据按分类推导，新节点写入 kind。"""
from app import knowledge


def test_infer_kind_from_category():
    assert knowledge.infer_kind("喜好") == "preference"
    assert knowledge.infer_kind("禁忌") == "preference"
    assert knowledge.infer_kind("人物") == "person"
    assert knowledge.infer_kind("日程") == "event"
    assert knowledge.infer_kind("待办") == "task"
    assert knowledge.infer_kind("定理") == "concept"
    assert knowledge.infer_kind("随便写的") == "fact"
    assert knowledge.infer_kind("") == "fact"


def test_make_node_sets_kind():
    n = knowledge.make_node("喜欢喝乌龙茶", category="喜好")
    assert n["kind"] == "preference"
    n2 = knowledge.make_node("自定义", category="其他", kind="concept")
    assert n2["kind"] == "concept"
    n3 = knowledge.make_node("错误类型", category="其他", kind="nope")
    assert n3["kind"] == "fact"
