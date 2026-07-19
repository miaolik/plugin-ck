#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""词库插件入口：GQ 风格词库引擎 + Web 词库编辑器。"""

from core.plugin.decorators import handler, on_load

from .ck_engine import Ctx, engine
from . import ck_web  # noqa: F401  (注册 Web 页面与路由)

__plugin_meta__ = {
    "name": "词库",
    "description": "GQ 风格词库引擎：变量/正则/如果判断/读写数据/排行榜/数据库/访问URL/多消息类型，含 Web 词库编辑器",
    "version": "1.0.0",
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


def build_ctx(event) -> Ctx:
    async def send(outputs, md_mode):
        await send_outputs(event, outputs, md_mode)

    return Ctx(
        message=event.content or "",
        user_id=event.user_id or "",
        username=event.username or "",
        group_id=event.group_id or "",
        guild_id=event.guild_id or "",
        channel_id=event.channel_id or "",
        message_id=event.message_id or "",
        appid=event.appid or "",
        robot_name="",
        avatar="",
        ats=_event_ats(event),
        images=_event_images(event),
        raw_json="",
        send=send,
    )


async def send_outputs(event, outputs, md_mode) -> None:
    """按片段顺序发送：文本合并成一条，媒体分条发送。"""
    for seg in outputs:
        kind = seg["type"]
        content = seg.get("content", "")
        if kind == "text":
            if content.strip("\n"):
                await event.reply(content, msg_type=2 if md_mode else None)
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
