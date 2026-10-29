# -*- coding: utf-8 -*-
"""研究道场：与学科无关的训练引擎。

分三层：
- primitives：8 个训练原语与判分器（L1 可验证 / L2 结构化 / L3 仅诊断）
- packs：从 resources/dojo/packs 加载 JSON 场景包，并做 schema 校验
- engine：会话与提交（进度、画像、复习由前端按同一套结构聚合）

新增一个场景只需加一个包目录，不改这里的代码。
"""
from app.dojo import engine, packs, primitives

__all__ = ["engine", "packs", "primitives"]
