#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""词库插件入口：GQ 风格词库引擎 + Web 词库编辑器。"""

import json

from core.plugin.decorators import handler, on_load

from .ck_engine import Ctx, engine
from . import ck_web  # noqa: F401  (注册 Web 页面与路由)

__plugin_meta__ = {
    "name": "词库",
    "description": "GQ 风格词库引擎：变量/正则/如果判断/循环遍历/读写数据/排行榜/数据库/访问URL/按钮/引用/撤回/多消息类型，含 Web 词库编辑器",
    "version": "1.1.0",
    "author": "miaolik",
}


def _event_images(event) -> list:
    urls = []
    if event.image_url:
        urls.append(event.image_url)
    for att in event.attachments or []:
        if isinstance(att, dict):
            url = att.get("url") or ""
            ctype = att.get("content_type") or ""
            if url and "image" in ctype and url not in urls:
                urls.append(url)
    return urls


def _event_ats(event) -> list:
    ids = []
    for m in event.mentions or []:
        if isinstance(m, dict) and m.get("id"):
            ids.append(str(m["id"]))
    return ids


def _get_bot(appid):
    from core.bot.manager import _bot_manager_ref
    if not _bot_manager_ref:
        return None
    try:
        return _bot_manager_ref.get_bot(appid)
    except Exception:
        return None


def _user_avatar(event) -> str:
    openid = event.raw_user_id or event.user_id or ""
    if event.appid and openid:
        return f"https://q.qlogo.cn/qqapp/{event.appid}/{openid}/640"
    return ""


def build_ctx(event) -> Ctx:
    async def send(outputs, md_mode):
        await send_outputs(event, outputs, md_mode)

    async def recall(message_id=""):
        if message_id:
            await event.recall(message_id=message_id)
        else:
            await event.recall()

    bot = _get_bot(event.appid)
    try:
        raw_json = json.dumps(event.raw or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        raw_json = ""
    extras = {
        "会话ID": event.chat_id or "",
        "ChatId": event.chat_id or "",
        "机器人身份": event.bot_member_role or "",
        "BotMemberRole": event.bot_member_role or "",
        "是否艾特机器人": "1" if event.is_at_self else "0",
        "IsAtSelf": "1" if event.is_at_self else "0",
        "引用ID": event.message_reference_id or "",
        "REFIDX": event.message_reference_id or "",
        "消息来源": event.scene_source or "",
        "事件类型": event.event_type or "",
        "EventType": event.event_type or "",
        "时间戳": str(event.timestamp or ""),
        "消息时间": str(event.timestamp or ""),
    }
    return Ctx(
        message=event.content or "",
        user_id=event.user_id or "",
        username=event.username or "",
        group_id=event.group_id or "",
        guild_id=event.guild_id or "",
        channel_id=event.channel_id or "",
        message_id=event.message_id or "",
        appid=event.appid or "",
        robot_name=(getattr(bot, "name", "") or "") if bot else "",
        avatar=_user_avatar(event),
        role=event.member_role or "",
        chat_type=event.chat_type or "",
        robot_qq=(getattr(bot, "robot_qq", "") or "") if bot else "",
        ats=_event_ats(event),
        images=_event_images(event),
        raw_json=raw_json,
        extras=extras,
        send=send,
        recall=recall,
    )


def _parse_buttons(spec: str):
    """±btn=文本;值|文本;值^下一行± → 框架按钮二维数组。

    值为 URL 时是链接按钮，以 / 开头是输入指令按钮，其余为回调按钮。
    """
    rows = []
    for row_spec in spec.split("^"):
        row = []
        for cell in row_spec.split("|"):
            cell = cell.strip()
            if not cell:
                continue
            text, _, data = cell.partition(";")
            data = data or text
            if data.startswith(("http://", "https://")):
                row.append({"text": text, "link": data})
            elif data.startswith("/"):
                row.append({"text": text, "data": data, "type": 2})
            else:
                row.append({"text": text, "data": data, "type": 1})
        if row:
            rows.append(row)
    return rows or None


async def send_outputs(event, outputs, md_mode) -> None:
    """按片段顺序发送：文本合并成一条，媒体分条发送；按钮/引用附加到文本消息。"""
    buttons = None
    ref_id = None
    for seg in outputs:
        if seg["type"] == "buttons":
            parsed = _parse_buttons(seg.get("content", ""))
            if parsed:
                buttons = (buttons or []) + parsed
        elif seg["type"] == "quote":
            ref_id = event.message_reference_id or None
    sent_text = False
    for seg in outputs:
        kind = seg["type"]
        content = seg.get("content", "")
        if kind == "text":
            if content.strip("\n"):
                await event.reply(content, msg_type=2 if md_mode else None,
                                  buttons=buttons if not sent_text else None,
                                  message_reference_id=ref_id if not sent_text else None)
                sent_text = True
        elif kind == "image" and content:
            await event.reply_image(content)
        elif kind == "video" and content:
            await event.reply_video(content)
        elif kind == "voice" and content:
            await event.reply_voice(content)
        elif kind == "file" and content:
            await event.reply_file(content)
        elif kind == "ark" and content:
            await _send_ark(event, content)
    if buttons and not sent_text:
        await event.reply("请选择：", buttons=buttons, message_reference_id=ref_id)


async def _send_ark(event, spec: str) -> None:
    """±ark=类型|参数1|参数2|...± → event.reply_ark。

    ark23: 标题|提示|项目1|项目2...（项目可写 文本;链接）
    ark24: 提示|标题|副标题|描述|图片URL|跳转URL|图片副标题
    ark37: 提示|标题|副标题|图片URL|跳转URL
    """
    parts = spec.split("|")
    try:
        ark_type = int(parts[0])
    except ValueError:
        await event.reply(f"ark 类型无效: {spec}")
        return
    args = parts[1:]
    if ark_type == 23:
        items = [it.split(";", 1) for it in args[2:]]
        await event.reply_ark(23, (args[0] if args else "", args[1] if len(args) > 1 else "", items))
    else:
        await event.reply_ark(ark_type, tuple(args))


@handler(r"^[\s\S]*$", name="词库", desc="GQ 风格词库触发", priority=-100)
async def ck_dispatch(event, match):
    message = (event.content or "").strip()
    if not message:
        return
    ctx = build_ctx(event)
    matched = await engine.handle(ctx)
    if not matched:
        return
    if ctx.errors:
        ctx.out_text("\n⚠ " + "\n⚠ ".join(ctx.errors))
    await send_outputs(event, ctx.outputs, ctx.md_mode)
    return True


@on_load
async def _init():
    engine.load()
    await engine.run_init()
