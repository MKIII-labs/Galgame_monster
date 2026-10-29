# -*- coding: utf-8 -*-
"""路由注册表：新功能自注册、重复注册报错"""
import uuid

import pytest

from app import registry


def test_work_and_calendar_routes_registered():
    from app import httpd  # noqa: F401  导入即完成各 api 模块的注册
    assert registry.get("POST", "/api/work/tasks") is not None
    assert registry.get("POST", "/api/work/artifacts") is not None
    assert registry.get("POST", "/api/calendar/events") is not None


def test_duplicate_route_raises():
    path = "/api/test/" + uuid.uuid4().hex

    @registry.route(path)
    def _first(h):
        return None

    with pytest.raises(RuntimeError):
        registry.route(path)(lambda h: None)


def test_non_api_path_rejected():
    with pytest.raises(ValueError):
        registry.route("/not-api")


def test_agent_prompt_lists_chat_capabilities():
    from app import agent, httpd  # noqa: F401
    prompt = agent.build_agent_prompt({})
    assert "calendar.create" in prompt
    assert "task.create" in prompt
    # 文件工具与 LLM 步骤只在任务里可用，应出现在「任务步骤专用」段
    assert "任务步骤专用" in prompt
    assert "llm.generate" in prompt
    assert "write_file" in prompt
