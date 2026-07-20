#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""词库插件入口：GQ 风格词库引擎 + Web 词库编辑器。"""

import time
import json
import re
from pathlib import Path

import aiohttp

from core.plugin.decorators import handler, on_load

from .ck_engine import BASE_DIR, DATA_DIR, Ctx, engine, http_timeout
from . import ck_web  # noqa: F401  (注册 Web 页面与路由)

__plugin_meta__ = {
    "name": "词库",
    "description": "GQ 风格词库引擎：变量/正则/如果判断/循环遍历/读写数据/排行榜/数据库/访问URL/按钮/引用/撤回/主动消息/多消息类型，含 Web 词库编辑器",
    "version": "1.2.0",
    "author": "miaolik",
}


def _get_bot(appid):
    from core.bot.manager import _bot_manager_ref
    if not _bot_manager_ref:
        return None
    try:
        return _bot_manager_ref.get_bot(appid)
    except Exception:
        return None


_BOT_ROLE_CACHE: dict = {}  # (appid, group_id) -> (role, expire_ts)
_BOT_ROLE_TTL = 300


async def _bot_role(event) -> str:
    """机器人在本群的真实身份（owner/admin/member）。

    优先用 mentions 里解析出的 bot_member_role（被@时才有），否则调用
    get_bot_member 查询并按群缓存，避免每条消息都打接口。
    """
    if not getattr(event, "is_group", False) or not event.group_id:
        return ""
    if getattr(event, "bot_member_role", ""):
        return event.bot_member_role
    key = (event.appid, event.group_id)
    cached = _BOT_ROLE_CACHE.get(key)
    now = time.time()
    if cached and cached[1] > now:
        return cached[0]
    role = ""
    try:
        member = await event.sender.get_bot_member(event.group_id)
        if isinstance(member, dict):
            role = member.get("member_role", "") or ""
    except Exception:
        role = ""
    _BOT_ROLE_CACHE[key] = (role, now + _BOT_ROLE_TTL)
    return role


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


def _event_extras(event, bot_role: str = "") -> dict:
    """框架 SDK 提供的补充变量（中英文名映射）。"""
    chat_id = getattr(event, "chat_id", "") or ""
    timestamp = str(getattr(event, "timestamp", "") or "")
    event_type = getattr(event, "event_type", "") or ""
    ref_id = getattr(event, "message_reference_id", "") or ""
    at_self = "1" if getattr(event, "is_at_self", False) else "0"
    bot_role = bot_role or getattr(event, "bot_member_role", "") or ""
    scene_source = (getattr(event, "message_scene", None) or {}).get("source", "") or ""
    return {
        "会话ID": chat_id, "ChatId": chat_id,
        "消息时间": timestamp, "时间戳": timestamp,
        "事件类型": event_type, "EventType": event_type,
        "引用ID": ref_id, "REFIDX": ref_id,
        "是否艾特机器人": at_self, "IsAtSelf": at_self,
        "机器人身份": bot_role, "BotMemberRole": bot_role,
        "消息来源": scene_source,
    }


def build_ctx(event, bot_role: str = "") -> Ctx:
    async def send(ctx):
        await send_ctx(event, ctx)

    async def recall(message_id: str = ""):
        if message_id:
            return await event.recall(message_id=message_id)
        return await event.recall()

    async def send_to_user(uid, content):
        await event.send_to_user(uid, content)

    async def send_to_group(gid, content):
        await event.send_to_group(gid, content)

    async def wakeup(uid, content):
        await event.send_wakeup(uid, content)

    async def force_wakeup(uid, content):
        await event.sender.force_wakeup(uid, content)

    async def share_link(data):
        return await event.sender.get_share_link(data)

    async def group_member(uid):
        if not event.group_id:
            return None
        member = await event.sender.get_group_member(event.group_id, uid)
        if member:
            return member
        # 查自己时接口失败可降级用消息自带的 author 数据
        if uid == (event.user_id or ""):
            author = (getattr(event, "raw", None) or {}).get("d", {}).get("author", {})
            if isinstance(author, dict) and author:
                return author
            fallback = {}
            if event.username:
                fallback["username"] = event.username
            if getattr(event, "member_role", ""):
                fallback["member_role"] = event.member_role
            if fallback:
                fallback["member_openid"] = uid
                return fallback
        return None

    async def bot_member():
        if not event.group_id:
            return None
        member = await event.sender.get_bot_member(event.group_id)
        if member:
            return member
        # 降级：被@时 mentions 解析出的机器人身份
        role = getattr(event, "bot_member_role", "") or ""
        return {"member_role": role} if role else None

    actions = {
        "主动私聊": send_to_user,
        "主动群发": send_to_group,
        "召回": wakeup,
        "强制召回": force_wakeup,
        "邀请链接": share_link,
        "群成员": group_member,
        "机器人成员": bot_member,
    }

    bot = _get_bot(event.appid)
    return Ctx(
        message=_clean_message(event),
        user_id=event.user_id or "",
        username=event.username or _event_get(event, "d/author/username"),
        group_id=event.group_id or "",
        guild_id=event.guild_id or "",
        channel_id=event.channel_id or "",
        message_id=event.message_id or _event_get(event, "d/id"),
        appid=event.appid or "",
        robot_name=(getattr(bot, "name", "") or "") if bot else
                   (getattr(getattr(event, "sender", None), "_bot_name", "") or ""),
        avatar=_avatar_url(event),
        role=getattr(event, "member_role", "") or _event_get(event, "d/author/member_role"),
        ats=_event_ats(event),
        images=_event_images(event),
        chat_type=getattr(event, "chat_type", "") or "",
        robot_qq=str((getattr(bot, "robot_qq", "") or "") if bot else "")
                 or str(getattr(getattr(event, "sender", None), "_bot_qq", "") or ""),
        raw_json=_event_raw_json(event),
        extras=_event_extras(event, bot_role),
        send=send,
        recall=recall,
        actions=actions,
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


def _parse_buttons(spec: str, small: bool = False):
    """±btn=文本;值|文本;值^下一行± → 框架按钮结构。

    值为 URL → 链接按钮；以 / 开头 → 填充输入框(type=2)；其余 → 回调(type=1)。省略值时用文本。
    small=True 时返回小字号键盘（font_size=small）。"""
    rows = []
    for row in spec.split("^"):
        btns = []
        for item in row.split("|"):
            item = item.strip()
            if not item:
                continue
            text, _, value = item.partition(";")
            text, value = text.strip(), (value or text).strip()
            if value.startswith(("http://", "https://")):
                btns.append({"text": text, "link": value})
            elif value.startswith("/"):
                btns.append({"text": text, "data": value, "type": 2})
            else:
                btns.append({"text": text, "data": value, "type": 1})
        if btns:
            rows.append(btns)
    if not rows:
        return []
    return {"rows": rows, "font_size": "small"} if small else rows


async def send_outputs(event, outputs, md_mode, *, text_mode=False,
                       skip_suffix=False, auto_delete=0) -> None:
    """按片段顺序发送：文本合并成一条，媒体分条发送；按钮/引用附加到首条文本。"""
    buttons = None
    small_rows = None
    quote_ref = ""
    for seg in outputs:
        if seg["type"] == "buttons" and seg.get("content"):
            parsed = _parse_buttons(seg["content"])
            if parsed:
                buttons = (buttons or []) + parsed
        elif seg["type"] == "buttons_small" and seg.get("content"):
            parsed = _parse_buttons(seg["content"], small=True)
            if parsed:
                small_rows = (small_rows or []) + parsed["rows"]
        elif seg["type"] == "quote":
            quote_ref = getattr(event, "message_reference_id", "") or ""
    if small_rows:
        buttons = {"rows": (buttons or []) + small_rows, "font_size": "small"}
    delete_after = auto_delete if auto_delete > 0 else None
    for seg in outputs:
        kind = seg["type"]
        content = seg.get("content", "")
        if kind == "text":
            if content.strip("\n"):
                kwargs = {}
                if buttons:
                    kwargs["buttons"] = buttons
                    buttons = None
                if quote_ref:
                    kwargs["message_reference_id"] = quote_ref
                    quote_ref = ""
                # QQ 开放平台要求键盘按钮必须挂在原生 Markdown 消息上
                if md_mode or kwargs.get("buttons"):
                    msg_type = 2
                elif text_mode:
                    msg_type = 0
                else:
                    msg_type = None
                await event.reply(content, msg_type=msg_type, skip_suffix=skip_suffix,
                                  auto_delete_time=delete_after, **kwargs)
        elif kind in ("image", "video", "voice", "file") and content:
            await _send_media(event, kind, content)
        elif kind == "ark" and content:
            await _send_ark(event, content)
    # 只有按钮/引用而无文本时，单独发一条
    if buttons or quote_ref:
        kwargs = {}
        if buttons:
            kwargs["buttons"] = buttons
            kwargs["msg_type"] = 2
        if quote_ref:
            kwargs["message_reference_id"] = quote_ref
        await event.reply(" ", skip_suffix=skip_suffix,
                          auto_delete_time=delete_after, **kwargs)


async def send_ctx(event, ctx) -> None:
    await send_outputs(event, ctx.outputs, ctx.md_mode, text_mode=ctx.text_mode,
                       skip_suffix=ctx.skip_suffix, auto_delete=ctx.auto_delete)


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


@handler(r"^[\s\S]*$", name="词库", desc="GQ 风格词库触发", priority=-100,
         event_types=["GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE",
                      "C2C_MESSAGE_CREATE", "AT_MESSAGE_CREATE",
                      "DIRECT_MESSAGE_CREATE", "MESSAGE_CREATE"])
async def ck_dispatch(event, match):
    message = _clean_message(event)
    if not message:
        return
    # 仅在确有词库块命中时才查机器人身份，避免每条群消息都打接口
    bot_role = await _bot_role(event) if engine.find_block(message) else ""
    ctx = build_ctx(event, bot_role)
    matched = await engine.handle(ctx)
    if not matched:
        return
    if ctx.errors:
        ctx.out_text("\n⚠ " + "\n⚠ ".join(ctx.errors))
    await send_ctx(event, ctx)
    return True


_BTN_DEBOUNCE_SECONDS = 2.0
_btn_last_click: dict = {}


@handler(r"^[\s\S]*$", name="词库按钮回调", desc="回调按钮触发词库", priority=-100,
         event_types=["INTERACTION_CREATE"])
async def ck_interaction(event, match):
    """回调按钮(type=1)点击：按钮 data 作为触发词走词库。同一用户同一按钮短时间内只响应一次。"""
    data = (event.content or "").strip()
    if not data:
        return
    key = (event.user_id or "", data)
    now = time.monotonic()
    last = _btn_last_click.get(key, 0.0)
    if now - last < _BTN_DEBOUNCE_SECONDS:
        event.set_callback_code(0)
        return
    _btn_last_click[key] = now
    if len(_btn_last_click) > 10000:
        _btn_last_click.clear()
    # 先应答交互，再执行词库；否则耗时操作（如接口查询）会导致客户端提示“第三方请求失败”
    event.set_callback_code(0)
    ctx = build_ctx(event)
    ctx.message = data
    matched = await engine.handle(ctx)
    if not matched:
        return
    if ctx.errors:
        ctx.out_text("\n⚠ " + "\n⚠ ".join(ctx.errors))
    await send_ctx(event, ctx)
    return True


@on_load
async def _init():
    engine.load()
    await engine.run_init()
