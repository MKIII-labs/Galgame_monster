# -*- coding: utf-8 -*-
"""「修改历史消息」时的附件变更：沿用旧附件 / 删掉 / 追加新附件。

从 chat.py 抽出来，方便单测：这段逻辑只碰消息里的 images / docs 字段，
不关心请求与流式输出。
"""
from app.uploads import save_upload


def _img_file(x):
    return str(x.get("file") or x.get("name") or "")


def _doc_file(x):
    return str(x.get("file") or x.get("name") or "")


def _msg_imgs(msg):
    ims = msg.get("images")
    if isinstance(ims, list):
        return [x for x in ims if isinstance(x, dict)]
    im = msg.get("image")
    return [im] if isinstance(im, dict) else []


def _msg_docs(msg):
    docs = msg.get("docs")
    if isinstance(docs, list):
        return [x for x in docs if isinstance(x, dict)]
    doc = msg.get("doc")
    return [doc] if isinstance(doc, dict) else []


def reconcile_edit(msg, sid, reuse_imgs=None, reuse_docs=None,
                   new_imgs=(), new_docs=()):
    """把这次「修改」的附件变更落到消息上（就地改 msg）。

    - reuse_imgs / reuse_docs：沿用哪些落盘文件（按 file/name 匹配）；
      None = 不动旧附件（兼容旧客户端），空集合 = 全部删掉。
    - new_imgs / new_docs：这次新选的附件，落盘后追加到消息末尾。
    返回 (imgs_changed, doc_items)；doc_items 是给后面的解析流程消费的
    [(原始名, base64, 落盘元信息, 在消息里的真实下标)]。
    """
    imgs_changed = False

    if reuse_imgs is not None:
        old = _msg_imgs(msg)
        keep = [x for x in old if _img_file(x) in reuse_imgs]
        imgs_changed = [_img_file(x) for x in keep] != [_img_file(x) for x in old]
        if keep:
            msg["images"] = keep
            msg["image"] = keep[0]
        else:
            msg.pop("images", None)
            msg.pop("image", None)

    if reuse_docs is not None:
        old = _msg_docs(msg)
        keep = [x for x in old if _doc_file(x) in reuse_docs]
        if keep:
            msg["docs"] = keep
            msg["doc"] = {k: v for k, v in keep[0].items() if k != "text"}
        else:
            msg.pop("docs", None)
            msg.pop("doc", None)
        # 旧兼容字段：删掉附件后不能继续把旧正文当上下文
        if len(keep) != len(old):
            msg.pop("doc_text", None)

    if new_imgs:
        imgs_changed = True
        ups = [u for u in (save_upload(sid, im["mime"], im["data"])
                           for im in new_imgs) if u]
        if ups:
            msg["images"] = _msg_imgs(msg) + ups
            msg["image"] = msg["images"][0]

    doc_items = []
    if new_docs:
        base = len(_msg_docs(msg))
        for d in new_docs:
            up = save_upload(sid, d.get("mime"), d["data"], d["name"])
            if not up:
                continue
            meta = {"id": up.get("id"), "name": d["name"],
                    "size": up.get("size") or 0,
                    "file": up.get("name"), "mime": d.get("mime") or ""}
            doc_items.append({"name": d["name"], "data": d["data"],
                              "mime": d.get("mime") or "", "meta": meta,
                              "idx": base + len(doc_items)})
        if doc_items:
            msg["docs"] = _msg_docs(msg) + [x["meta"] for x in doc_items]
            msg["doc"] = {k: v for k, v in msg["docs"][0].items() if k != "text"}
            msg.pop("doc_text", None)

    if imgs_changed:
        msg.pop("vision", None)          # 图片变了，旧的识图描述作废
    return imgs_changed, doc_items
