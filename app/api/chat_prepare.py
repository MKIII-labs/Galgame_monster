# -*- coding: utf-8 -*-
"""聊天请求入口：参数解析 + 会话定位。

从 `chat._api_chat` 拆出的第一段：只负责「这一轮要发什么」的解析与基础校验，
不碰 SSE、模型调用与工具循环。租约认领仍由调用方完成（早退走传入的 reject）。
"""
import re
import time
from dataclasses import dataclass

from app.context import compress_busy
from app.sessions import load_session

_QUESTION_POLICIES = ("auto", "smart", "ask")


def question_policy_of(cfg):
    raw = str((cfg or {}).get("autonomy_question_policy") or "ask").strip().lower()
    return raw if raw in _QUESTION_POLICIES else "ask"


@dataclass
class ChatRequest:
    """本轮请求的解析结果：字段与原来 `_api_chat` 里的局部变量一一对应。"""

    content: str
    question_policy: str
    ask_timeout: int
    plan_mode: bool
    resend: dict
    resume_req: bool
    cont_req: bool
    imgs: list
    img_b64: str
    img_mime: str
    has_img: bool
    docs: list
    has_doc: bool
    reuse_imgs: set
    reuse_docs: set
    client_imgs: list
    doc_items: list
    s: dict
    load_at: float
    parse_at: float = 0.0        # 参数解析开始（不含请求体读取）
    sess_load_ms: int = 0        # 仅 load_sessions() 的耗时


def parse_chat_request(body, sid, reject, question_policy="ask"):
    """解析参数并读取会话；任何早退都已通过 reject 写好响应并返回 None。

    question_policy 由调用方用当前配置算好传入：配置读取仍走调用方模块的引用，
    测试 / 调用方替换 load_config 时行为一致。
    """
    parse_at = time.monotonic()
    # 压缩期间不接受新请求：压缩用的是开始时的消息快照，期间编辑/追加会让
    # 摘要与现状错位。前端会把这个请求转成排队，压缩完成后自动重发。
    if compress_busy(sid):
        reject({"ok": False, "error": "busy", "reason": "compressing",
                "message": "该会话正在压缩较早的历史，消息会稍后自动发出。"}, 409)
        return None
    content = (body.get("content") or "").strip()
    # 提问卡不限时：0 = 永不超时，一直等到用户回答 / 点停止 / 让 AI 自己决定
    _ask_timeout = 0
    # /plan 规划模式：只读研究 + 产出计划卡，不执行。前端会先剥掉前缀；
    # 这里再兜一次（旧客户端 / 直接发文本协议的情况）。
    plan_mode = bool(body.get("plan_mode"))
    if re.match(r"^/plan(?:\s|$)", content, re.I):
        plan_mode = True
        content = re.sub(r"^/plan\s*", "", content, count=1, flags=re.I).strip()
        if not content:
            content = "请先规划当前任务：读必要上下文，给出里程碑计划，然后等我确认。"
    resend = body.get("resend") if isinstance(body.get("resend"), dict) else None
    resume_req = bool(body.get("resume"))        # 「继续任务」：进程重启后的断点续跑
    cont_req = bool(body.get("continue")) or resume_req   # 「继续输出」：从断点续写，不新开消息
    # 图片附件：新前端发 images 列表；旧客户端仍发单张 image
    raw_imgs = body.get("images")
    imgs = []
    if isinstance(raw_imgs, list):
        for it in raw_imgs[:4]:
            if isinstance(it, dict) and it.get("data"):
                imgs.append({"mime": it.get("mime") or "image/png",
                             "data": str(it.get("data") or "")})
    img = body.get("image") if isinstance(body.get("image"), dict) else None
    if not imgs and img and img.get("data"):
        imgs = [{"mime": img.get("mime") or "image/png",
                 "data": str(img.get("data") or "")}]
    img_b64 = imgs[0]["data"] if imgs else ""
    img_mime = imgs[0]["mime"] if imgs else "image/png"
    has_img = bool(imgs)                        # 「修改消息」也允许换图
    # 文档附件：新前端发 docs 列表（可多文件）；旧客户端仍发单个 doc
    docs = []
    raw_docs = body.get("docs")
    if isinstance(raw_docs, list):
        for it in raw_docs[:10]:
            if not isinstance(it, dict):
                continue
            nm = str(it.get("name") or "").strip()[:200]
            dt = str(it.get("data") or "")
            if nm and dt:
                docs.append({"name": nm, "mime": it.get("mime") or "", "data": dt})
    doc = body.get("doc") if isinstance(body.get("doc"), dict) else None
    if not docs and doc:
        nm = str(doc.get("name") or "").strip()[:200]
        dt = str(doc.get("data") or "")
        if nm and dt:
            docs = [{"name": nm, "mime": doc.get("mime") or "", "data": dt}]
    if resend and resend.get("mode") != "edit":
        docs = []                     # 文档附件只在「发新消息 / 修改消息」时支持
    has_doc = bool(docs)
    # 「修改消息」时附件也参与编辑：reuse_* 是「沿用原消息里的哪些附件」（落盘文件名）。
    # 不传 = 全部沿用（兼容旧客户端）；传空列表 = 全部删掉。
    _raw_reuse_imgs = body.get("reuse_images")
    _raw_reuse_docs = body.get("reuse_docs")
    reuse_imgs = (None if not isinstance(_raw_reuse_imgs, list) else
                  {str(x) for x in _raw_reuse_imgs if str(x).strip()})
    reuse_docs = (None if not isinstance(_raw_reuse_docs, list) else
                  {str(x) for x in _raw_reuse_docs if str(x).strip()})
    client_imgs = list(imgs)          # 这次新选的图：稍后落盘并挂回这条消息
    doc_items = []                    # [(原始名, base64, 落盘元信息)]：解析阶段逐个消费
    if not content and not resend and not has_img and not has_doc and not cont_req:
        reject({"ok": False, "error": "empty"}, 400)
        return None

    _load_t0 = time.monotonic()
    s = load_session(sid)
    load_at = time.monotonic()
    sess_load_ms = int((load_at - _load_t0) * 1000)
    if not s:
        reject({"ok": False, "error": "no session"}, 404)
        return None
    return ChatRequest(
        content=content, question_policy=question_policy, ask_timeout=_ask_timeout,
        plan_mode=plan_mode, resend=resend, resume_req=resume_req, cont_req=cont_req,
        imgs=imgs, img_b64=img_b64, img_mime=img_mime, has_img=has_img,
        docs=docs, has_doc=has_doc, reuse_imgs=reuse_imgs, reuse_docs=reuse_docs,
        client_imgs=client_imgs, doc_items=doc_items, s=s, load_at=load_at,
        parse_at=parse_at, sess_load_ms=sess_load_ms)
