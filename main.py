#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""词库插件入口：GQ 风格词库引擎 + Web 词库编辑器。"""

import json
import re
from pathlib import Path

import aiohttp

from core.plugin.decorators import handler, on_load

from .ck_engine import BASE_DIR, DATA_DIR, Ctx, engine, http_timeout
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


_URL_TOKEN_RE = re.compile(r"\s*<https?://[^>\s]+>")


def _clean_message(event) -> str:
    """去掉框架追加进 content 的 <图片URL> 占位，避免污染 %参数N%/%括号N%。"""
    return _URL_TOKEN_RE.sub("", event.content or "").strip()


def _event_raw_json(event) -> str:
    raw = getattr(event, "raw", None)
    if not raw:
        return ""
    try:
        return json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _event_get(event, path: str) -> str:
    raw = getattr(event, "raw", None) or {}
    data = raw
    for key in path.split("/"):
        if not isinstance(data, dict):
            return ""
        data = data.get(key)
    return str(data) if data else ""


def _avatar_url(event) -> str:
    """头像：频道消息数据自带 author.avatar；群/私聊用 https://q.qlogo.cn/qqapp/{appid}/{openid}/640"""
    avatar = _event_get(event, "d/author/avatar")
    if avatar:
        return avatar
    uid = event.user_id or ""
    if event.appid and uid:
        return f"https://q.qlogo.cn/qqapp/{event.appid}/{uid}/640"
    return ""


def build_ctx(event) -> Ctx:
    async def send(outputs, md_mode):
        await send_outputs(event, outputs, md_mode)

    return Ctx(
        message=_clean_message(event),
        user_id=event.user_id or "",
        username=event.username or _event_get(event, "d/author/username"),
        group_id=event.group_id or "",
        guild_id=event.guild_id or "",
        channel_id=event.channel_id or "",
        message_id=event.message_id or _event_get(event, "d/id"),
        appid=event.appid or "",
        robot_name=getattr(getattr(event, "sender", None), "_bot_name", "") or "",
        avatar=_avatar_url(event),
        role=getattr(event, "member_role", "") or _event_get(event, "d/author/member_role"),
        ats=_event_ats(event),
        images=_event_images(event),
        raw_json=_event_raw_json(event),
        send=send,
    )


def _resolve_local_media(path_str: str):
    """本地媒体路径 → 字节；依次尝试绝对路径 / 插件 data/ / 插件目录。"""
    candidates = []
    p = Path(path_str)
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.extend([DATA_DIR / path_str, BASE_DIR / path_str])
    for c in candidates:
        try:
            c = c.resolve()
            if c.exists() and c.is_file():
                return c.read_bytes(), c.name
        except OSError:
            continue
    return None, None


async def _download_bytes(url: str):
    timeout = aiohttp.ClientTimeout(total=http_timeout())
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            return await resp.content.read(20 * 1024 * 1024)


async def _send_media(event, kind: str, content: str) -> None:
    reply = {"image": event.reply_image, "video": event.reply_video,
             "voice": event.reply_voice, "file": event.reply_file}[kind]
    if content.startswith(("http://", "https://")):
        if kind == "file":
            # 框架 reply_file 的 URL 分支存在 kwargs 问题，改为自行下载后以字节发送
            try:
                data = await _download_bytes(content)
            except (aiohttp.ClientError, OSError) as exc:
                await event.reply(f"⚠ 文件下载失败: {exc}")
                return
            name = Path(content.split("?")[0]).name or "file"
            await reply(data, file_name=name)
        else:
            await reply(content)
        return
    data, name = _resolve_local_media(content)
    if data is None:
        await event.reply(f"⚠ 本地文件不存在: {content}")
        return
    if kind == "file":
        await reply(data, file_name=name)
    else:
        await reply(data)


async def send_outputs(event, outputs, md_mode) -> None:
    """按片段顺序发送：文本合并成一条，媒体分条发送。"""
    for seg in outputs:
        kind = seg["type"]
        content = seg.get("content", "")
        if kind == "text":
            if content.strip("\n"):
                await event.reply(content, msg_type=2 if md_mode else None)
        elif kind in ("image", "video", "voice", "file") and content:
            await _send_media(event, kind, content)
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
