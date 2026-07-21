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
    "version": "1.2.1",
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

# 群成员信息缓存（平台未开放群成员查询接口，从消息体 author 里积累）
_MEMBERS_FILE = DATA_DIR / "members.json"
_members_cache: dict = {}  # group_id -> {user_id: {username, member_role, first_seen, last_seen}}
_members_dirty = False
_members_last_save = 0.0
_MEMBERS_SAVE_INTERVAL = 30


def _members_load() -> None:
    global _members_cache
    if _MEMBERS_FILE.exists():
        try:
            data = json.loads(_MEMBERS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _members_cache = data
        except (json.JSONDecodeError, OSError):
            _members_cache = {}


def _members_save(force: bool = False) -> None:
    global _members_dirty, _members_last_save
    now = time.time()
    if not _members_dirty or (not force and now - _members_last_save < _MEMBERS_SAVE_INTERVAL):
        return
    try:
        _MEMBERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MEMBERS_FILE.write_text(json.dumps(_members_cache, ensure_ascii=False), encoding="utf-8")
        _members_dirty = False
        _members_last_save = now
    except OSError:
        pass


def _members_record(event) -> None:
    """从群消息体 author 里记录发言人的昵称/身份；同时持久化机器人身份。"""
    global _members_dirty
    gid = event.group_id or ""
    if not gid:
        return
    author = ((getattr(event, "raw", None) or {}).get("d") or {}).get("author") or {}
    uid = event.user_id or ""
    if uid and isinstance(author, dict) and (author.get("username") or author.get("member_role")):
        grp = _members_cache.setdefault(gid, {})
        rec = grp.setdefault(uid, {"first_seen": int(time.time())})
        if author.get("username"):
            rec["username"] = author["username"]
        if author.get("member_role"):
            rec["member_role"] = author["member_role"]
        rec["last_seen"] = int(time.time())
        _members_dirty = True
    bot_role = getattr(event, "bot_member_role", "") or ""
    if bot_role:
        grp = _members_cache.setdefault(gid, {})
        if grp.get("__bot__", {}).get("member_role") != bot_role:
            grp["__bot__"] = {"member_role": bot_role, "last_seen": int(time.time())}
            _members_dirty = True
    _members_save()


def _members_get(gid: str, uid: str):
    rec = (_members_cache.get(gid) or {}).get(uid)
    if isinstance(rec, dict) and rec:
        out = dict(rec)
        out.setdefault("member_openid", uid)
        return out
    return None


_members_load()


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
    persisted = _members_get(event.group_id, "__bot__")
    if persisted:
        role = persisted.get("member_role", "") or ""
    if not role:
        try:
            member = await event.sender.get_bot_member(event.group_id)
            if isinstance(member, dict):
                role = member.get("member_role", "") or ""
        except Exception:
            role = ""
    _BOT_ROLE_CACHE[key] = (role, now + _BOT_ROLE_TTL)
    return role


async def _fill_member_vars(event, ctx) -> None:
    """回调等不带 author 的事件里, 补全 %昵称%/%身份%：优先本地成员缓存
    (用户在群里发过言即有记录), 接口仅作尝试项, 失败静默。"""
    if not getattr(event, "is_group", False) or not event.group_id:
        return
    if ctx.username and ctx.role:
        return
    uid = event.user_id or ""
    rec = _members_get(event.group_id, uid)
    if not rec:
        try:
            rec = await event.sender.get_group_member(event.group_id, uid)
        except Exception:
            rec = None
    if isinstance(rec, dict):
        ctx.username = ctx.username or rec.get("username", "") or ""
        ctx.role = ctx.role or rec.get("member_role", "") or ""


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

    async def send_to_channel(cid, content):
        await event.sender.send_to_channel(cid, content)

    async def wakeup(uid, content):
        await event.send_wakeup(uid, content)

    async def force_wakeup(uid, content):
        await event.sender.force_wakeup(uid, content)

    async def share_link(data):
        return await event.sender.get_share_link(data)

    async def group_member(uid):
        if not event.group_id:
            return None
        # 平台未开放群成员查询接口，以消息体/本地缓存为主，接口作为尝试项
        if uid == (event.user_id or ""):
            author = ((getattr(event, "raw", None) or {}).get("d") or {}).get("author") or {}
            if isinstance(author, dict) and (author.get("username") or author.get("member_role")):
                out = dict(author)
                out.setdefault("member_openid", uid)
                return out
        cached = _members_get(event.group_id, uid)
        if cached:
            return cached
        try:
            member = await event.sender.get_group_member(event.group_id, uid)
        except Exception:
            member = None
        if member:
            return member
        if uid == (event.user_id or "") and event.username:
            return {"member_openid": uid, "username": event.username,
                    "member_role": getattr(event, "member_role", "") or ""}
        return None

    async def bot_member():
        if not event.group_id:
            return None
        # 消息体 mentions 里的机器人身份最准，其次本地持久缓存，最后尝试接口
        role = getattr(event, "bot_member_role", "") or ""
        if role:
            return {"member_role": role}
        cached = _members_get(event.group_id, "__bot__")
        if cached:
            return cached
        try:
            member = await event.sender.get_bot_member(event.group_id)
        except Exception:
            member = None
        return member or None

    async def open_api(method, path, payload):
        kwargs = {}
        if payload is not None:
            kwargs["json"] = payload
        return await event.sender._request(method, path, **kwargs)

    actions = {
        "主动私聊": send_to_user,
        "主动群发": send_to_group,
        "主动频道": send_to_channel,
        "召回": wakeup,
        "强制召回": force_wakeup,
        "邀请链接": share_link,
        "群成员": group_member,
        "机器人成员": bot_member,
        "官方API": open_api,
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
    """本地媒体路径 → 字节；依次尝试绝对路径 / 插件 data/ / 插件目录。

    Windows 反斜杠路径统一转 /（Path 在 Windows 上也接受 /），两平台通用。"""
    path_str = path_str.strip().strip('"').strip("'").replace("\\", "/")
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
    if getattr(event, "is_group", False):
        _members_record(event)
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


async def _ack_interaction(event) -> bool:
    """向QQ官方确认收到 interaction 事件，防止客户端显示'第三方请求失败'。

    QQ 官方要求：收到 INTERACTION_CREATE 后，必须通过 PUT /interactions/{id}
    发送 code=0 的确认，否则客户端会一直处于 loading 状态直到超时。
    """
    try:
        # 从事件对象获取 interaction_id
        interaction_id = getattr(event, "message_id", "") or ""
        if not interaction_id:
            # 尝试从原始数据中获取
            raw = getattr(event, "raw", None) or {}
            interaction_id = (raw.get("d", {}) or {}).get("id", "") or ""
        if not interaction_id:
            return False

        # 使用框架 sender 发送 PUT 请求到 QQ OpenAPI
        sender = getattr(event, "sender", None)
        if sender and hasattr(sender, "_request"):
            await sender._request(
                "PUT",
                f"/interactions/{interaction_id}",
                json={"code": 0}
            )
            return True
        return False
    except Exception:
        # ack 失败不应阻塞主逻辑，静默处理
        return False


@handler(r"^[\s\S]*$", name="词库按钮回调", desc="回调按钮触发词库", priority=-100,
         event_types=["INTERACTION_CREATE"])
async def ck_interaction(event, match):
    """回调按钮(type=1)点击：按钮 data 作为触发词走词库。同一用户同一按钮短时间内只响应一次。"""
    # 【关键修复】立即向QQ官方确认收到事件，防止客户端显示"第三方请求失败"
    # 必须在业务逻辑之前调用，因为QQ官方有超时限制（通常3秒内必须回应）
    await _ack_interaction(event)

    data = (event.content or "").strip()
    if not data:
        return
    key = (event.user_id or "", data)
    now = time.monotonic()
    last = _btn_last_click.get(key, 0.0)
    if now - last < _BTN_DEBOUNCE_SECONDS:
        return
    _btn_last_click[key] = now
    if len(_btn_last_click) > 10000:
        _btn_last_click.clear()

    # 回调事件不带 author/mentions, 昵称身份与机器人身份从本地缓存/接口补全
    hit = engine.find_block(data) is not None
    bot_role = await _bot_role(event) if hit else ""
    ctx = build_ctx(event, bot_role)
    ctx.message = data
    if hit:
        await _fill_member_vars(event, ctx)
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
