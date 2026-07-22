#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""词库插件入口：GQ 风格词库引擎 + Web 词库编辑器。"""

import hashlib
import sys
import time
import json
import re
from pathlib import Path

import aiohttp

from core.plugin.decorators import handler, on_load, on_unload
from core.base.logger import PLUGIN, get_logger, report_error

from .ck_engine import (
    BASE_DIR, DATA_DIR, CKError, Ctx, _assert_public_url, engine, fetch_bytes,
    image_size_from_bytes, is_http_url, load_json_dict, save_json_file,
    settings_load,
)
from . import ck_web  # noqa: F401  (注册 Web 页面与路由)
from .ck_cron import CronManager

logger = get_logger(PLUGIN, "词库")

__plugin_meta__ = {
    "name": "词库",
    "description": "GQ 风格词库引擎：变量/正则/如果判断/循环遍历/读写数据/排行榜/数据库/访问URL/按钮/引用/撤回/主动消息/多消息类型，含 Web 词库编辑器",
    "version": "1.3.0",
    "author": "miaolik",
}


def _get_bot(appid):
    from core.bot.manager import _bot_manager_ref
    if not _bot_manager_ref:
        return None
    try:
        return _bot_manager_ref.get_bot(appid)
    except Exception:
        logger.debug("获取 bot 实例失败 (appid=%s)", appid, exc_info=True)
        return None


def _event_d(event) -> dict:
    """事件原始数据里的 d 段（QQ 开放平台事件体），缺失时返回空 dict。"""
    return (getattr(event, "raw", None) or {}).get("d") or {}


def _append_errors(ctx) -> None:
    """把本次运行收集到的错误以 ⚠ 前缀附加到文本输出末尾。"""
    if ctx.errors:
        ctx.out_text("\n⚠ " + "\n⚠ ".join(ctx.errors))


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
    _members_cache = load_json_dict(_MEMBERS_FILE, label="成员缓存文件")


def _members_save(force: bool = False) -> None:
    global _members_dirty, _members_last_save
    now = time.time()
    if not _members_dirty or (not force and now - _members_last_save < _MEMBERS_SAVE_INTERVAL):
        return
    try:
        save_json_file(_MEMBERS_FILE, _members_cache)
        _members_dirty = False
        _members_last_save = now
    except OSError as exc:
        logger.warning("写入成员缓存文件失败，成员信息未持久化: %s (%s)", _MEMBERS_FILE, exc)


def _members_record(event) -> None:
    """从群消息体 author 里记录发言人的昵称/身份；同时持久化机器人身份。"""
    global _members_dirty
    gid = event.group_id or ""
    if not gid:
        return
    author = _event_d(event).get("author") or {}
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


async def _bot_role(event, cache_only: bool = False) -> str:
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
    if not role and not cache_only:
        try:
            member = await event.sender.get_bot_member(event.group_id)
            if isinstance(member, dict):
                role = member.get("member_role", "") or ""
        except Exception:
            logger.debug("查询机器人群身份失败 (group_id=%s)", event.group_id, exc_info=True)
            role = ""
    _BOT_ROLE_CACHE[key] = (role, now + _BOT_ROLE_TTL)
    return role


async def _fill_member_vars(event, ctx) -> None:
    """回调等不带 author 的事件里, 补全 %昵称%/%身份%：只读本地成员缓存
    (用户在群里发过言即有记录)。成员接口尚未开放, 回调路径不打接口,
    避免阻塞导致客户端提示请求超时。"""
    if not getattr(event, "is_group", False) or not event.group_id:
        return
    if ctx.username and ctx.role:
        return
    uid = event.user_id or ""
    rec = _members_get(event.group_id, uid)
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


def _channel_role(event) -> str:
    """频道消息身份：d.member.roles 里 4=频道主 2=管理员 5=子频道管理，其余为成员。"""
    d = _event_d(event)
    roles = (d.get("member") or {}).get("roles") or []
    roles = {str(r) for r in roles}
    if "4" in roles:
        return "owner"
    if "2" in roles or "5" in roles:
        return "admin"
    return "member" if roles else ""


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

    async def send_guild_dm(uid, content):
        # 频道私信：先创建私信会话, 再向会话 guild 发消息
        gid = event.guild_id or str(_event_d(event).get("guild_id", "") or "")
        ok, data = await event.sender._request(
            "POST", "/users/@me/dms",
            json={"recipient_id": uid, "source_guild_id": gid})
        dms_gid = str((data or {}).get("guild_id", "") or "") if ok else ""
        if not dms_gid:
            raise RuntimeError(f"创建频道私信会话失败: {data}")
        await event.sender._request("POST", f"/dms/{dms_gid}/messages", json={"content": content})

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
            author = _event_d(event).get("author") or {}
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
            logger.debug("查询群成员失败 (group_id=%s, uid=%s)", event.group_id, uid, exc_info=True)
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
            logger.debug("查询机器人成员失败 (group_id=%s)", event.group_id, exc_info=True)
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
        "频道私聊": send_guild_dm,
        "召回": wakeup,
        "强制召回": force_wakeup,
        "邀请链接": share_link,
        "群成员": group_member,
        "机器人成员": bot_member,
        "官方API": open_api,
    }
    actions.update(_media_actions(event.sender))

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
        role=getattr(event, "member_role", "") or _event_get(event, "d/author/member_role")
             or _channel_role(event),
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
        except OSError as exc:
            logger.debug("读取本地媒体候选失败: %s (%s)", c, exc)
            continue
    return None, None


async def _download_bytes(url: str):
    await _assert_public_url(url)
    return await fetch_bytes("GET", url, max_bytes=20 * 1024 * 1024)


async def _send_media(event, kind: str, content: str) -> None:
    reply = {"image": event.reply_image, "video": event.reply_video,
             "voice": event.reply_voice, "file": event.reply_file}[kind]
    if is_http_url(content):
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


# ---------------------------------------------------------------------------
# 渲染 / 画图 / 图床（基于框架 Playwright 渲染模块与频道图床）
# ---------------------------------------------------------------------------

RENDER_DIR = DATA_DIR / "渲染"
_RENDER_KEEP_SECONDS = 3600
_SIZE_RE = re.compile(r"^(\d{2,4})[xX\*](\d{2,4})$")

_DRAW_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{margin:0;display:inline-block;background:#fff;font-family:'PingFang SC','Microsoft YaHei',sans-serif}
.card{padding:24px 28px;font-size:20px;line-height:1.7;color:#2f3542;white-space:pre-wrap;word-break:break-all}
</style></head><body><div class="card">{{content}}</div></body></html>"""


def _get_module(name: str):
    """获取框架已启用的可选模块实例（未启用时返回 None）。"""
    from core.bot.manager import _bot_manager_ref
    if not _bot_manager_ref:
        return None
    return _bot_manager_ref.module_manager.get(name)


def _get_playwright():
    pw = _get_module("playwright")
    if pw is None or not pw.is_available():
        raise CKError("渲染引擎不可用：需启用框架的 Playwright 渲染模块")
    return pw


def _get_datastore():
    ds = _get_module("datastore")
    if ds is None:
        raise CKError("数据存储引擎未启用：需在框架模块页启用「数据存储引擎」")
    return ds


def _mysql_pool():
    ds = _get_datastore()
    if not ds.mysql_available():
        raise CKError("MySQL 不可用：需在数据存储引擎配置中开启 mysql_enabled 并配置连接")
    return ds.mysql


def _redis_pool():
    ds = _get_datastore()
    if not ds.redis_available():
        raise CKError("Redis 不可用：需在数据存储引擎配置中开启 redis_enabled 并配置连接")
    return ds.redis


async def _mysql_query_action(rest: str) -> str:
    """$MySQL查 SQL$ → fetch_all，返回 JSON 数组（配合 @%变% [字段] / 循环遍历）。"""
    sql = rest.strip()
    if not sql:
        raise CKError("$MySQL查$ 格式：$MySQL查 SELECT 语句$")
    rows = await _mysql_pool().fetch_all(sql)
    return json.dumps(rows or [], ensure_ascii=False, default=str)


async def _mysql_exec_action(rest: str) -> str:
    """$MySQL执行 SQL$ → execute，返回影响行数。"""
    sql = rest.strip()
    if not sql:
        raise CKError("$MySQL执行$ 格式：$MySQL执行 SQL语句$")
    result = await _mysql_pool().execute(sql)
    return str(result)


async def _redis_get_action(rest: str) -> str:
    # $Redis读 键 [默认值]$
    args = rest.split(" ", 1)
    if not args or not args[0]:
        raise CKError("$Redis读$ 格式：$Redis读 键 [默认值]$")
    default = args[1] if len(args) == 2 else ""
    value = await _redis_pool().get(args[0])
    return default if value is None else str(value)


async def _redis_set_action(rest: str) -> str:
    # $Redis写 键 [秒=N] 值$（值可含空格）
    args = rest.split(" ", 1)
    if len(args) != 2 or not args[0] or not args[1]:
        raise CKError("$Redis写$ 格式：$Redis写 键 [秒=N] 值$")
    key, value = args[0], args[1]
    ex = None
    head, _, tail = value.partition(" ")
    if head.startswith("秒=") and head[2:].isdigit() and tail:
        ex, value = int(head[2:]), tail
    await _redis_pool().set(key, value, ex=ex)
    return ""


async def _redis_del_action(rest: str) -> str:
    key = rest.strip()
    if not key:
        raise CKError("$Redis删$ 格式：$Redis删 键$")
    n = await _redis_pool().delete(key)
    return str(n)


async def _redis_incr_action(rest: str) -> str:
    # $Redis自增 键 [数量]$ → 自增后的值（负数为自减）
    args = rest.split()
    if not args or not args[0]:
        raise CKError("$Redis自增$ 格式：$Redis自增 键 [数量]$")
    amount = 1
    if len(args) >= 2:
        try:
            amount = int(args[1])
        except ValueError:
            raise CKError("$Redis自增$ 数量须为整数")
    value = await _redis_pool().incr(args[0], amount)
    return str(value)


_HOSTING_TYPES = ("qq", "cos", "bilibili", "chatglm", "ukaka", "xingye", "nature")


async def _load_image_arg(image: str) -> bytes:
    """URL 或本地路径 → 图片字节。"""
    if is_http_url(image):
        return await _download_bytes(image)
    data, _ = _resolve_local_media(image)
    if data is None:
        raise CKError(f"本地图片不存在: {image}")
    return data


def _get_mail_module():
    """查找已加载的 Agently 邮箱插件（plugin-yx）的 邮箱 模块。"""
    for mod_name, mod in list(sys.modules.items()):
        if mod_name.startswith("plugins.") and mod_name.endswith(".邮箱") and hasattr(mod, "send_mail"):
            return mod
    raise CKError("邮箱能力不可用：需先安装并加载 Agently 邮箱插件（plugin-yx）")


def _mail_require(mod, func_name: str):
    fn = getattr(mod, func_name, None)
    if fn is None:
        raise CKError("邮箱插件版本过旧：请更新 plugin-yx 到最新版")
    return fn


def _mail_slot(mod, rest: str):
    """解析可选的 槽位=名称 前缀，缺省用邮箱插件当前槽位。"""
    rest = rest.strip()
    user = mod.get_current_user()
    if rest.startswith("槽位="):
        head, _, tail = rest.partition(" ")
        user, rest = head[3:], tail.strip()
    return user, rest


async def _mail_send_action(rest: str) -> str:
    # $邮件发送 [槽位=名] 收件人|主题|正文$（多收件人逗号分隔）
    mod = _get_mail_module()
    user, rest = _mail_slot(mod, rest)
    parts = rest.split("|")
    if len(parts) < 3:
        raise CKError("$邮件发送$ 格式：$邮件发送 [槽位=名] 收件人|主题|正文$")
    to = [x.strip() for x in parts[0].replace("，", ",").split(",") if x.strip()]
    subject, body = parts[1].strip(), "|".join(parts[2:]).strip()
    if not to or not subject or not body:
        raise CKError("$邮件发送$ 收件人、主题、正文不能为空")
    ok, msg = await mod.send_mail(to, subject, body, user=user)
    if not ok:
        raise CKError(f"$邮件发送$ 失败：{msg}")
    return str(msg)


async def _mail_reply_action(rest: str) -> str:
    # $邮件回复 [槽位=名] 邮件ID|正文$
    mod = _get_mail_module()
    user, rest = _mail_slot(mod, rest)
    parts = rest.split("|", 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise CKError("$邮件回复$ 格式：$邮件回复 [槽位=名] 邮件ID|正文$")
    ok, msg = await mod.reply_mail(parts[0].strip(), parts[1].strip(), user=user)
    if not ok:
        raise CKError(f"$邮件回复$ 失败：{msg}")
    return str(msg)


async def _mail_list_action(rest: str) -> str:
    # $邮件列表 [槽位=名] [数量] [文件夹]$ → JSON 数组（配合 @%变% [字段] / 循环遍历）
    mod = _get_mail_module()
    fetch = _mail_require(mod, "fetch_mails")
    brief = _mail_require(mod, "_mail_brief")
    user, rest = _mail_slot(mod, rest)
    args = rest.split()
    count = int(args[0]) if args and args[0].isdigit() else 10
    folder_arg = args[1] if args and args[0].isdigit() and len(args) > 1 else (args[0] if args and not args[0].isdigit() else "")
    folder = mod.FOLDER_ALIAS.get(folder_arg.strip(), "inbox")
    ok, res = await fetch(count, folder, user)
    if not ok:
        raise CKError(f"$邮件列表$ 失败：{res}")
    return json.dumps([brief(m) for m in res], ensure_ascii=False)


async def _mail_read_action(rest: str) -> str:
    # $邮件读取 [槽位=名] 邮件ID$ → JSON 对象（subject/sender/to/date/body/attachments）
    mod = _get_mail_module()
    fetch = _mail_require(mod, "fetch_mail")
    user, rest = _mail_slot(mod, rest)
    mail_id = rest.strip()
    if not mail_id:
        raise CKError("$邮件读取$ 格式：$邮件读取 [槽位=名] 邮件ID$")
    ok, res = await fetch(mail_id, user)
    if not ok:
        raise CKError(f"$邮件读取$ 失败：{res}")
    return json.dumps(res, ensure_ascii=False)


async def _mail_search_action(rest: str) -> str:
    # $邮件搜索 [槽位=名] [数量] 关键词$ → JSON 数组
    mod = _get_mail_module()
    fetch = _mail_require(mod, "fetch_search")
    brief = _mail_require(mod, "_mail_brief")
    user, rest = _mail_slot(mod, rest)
    count = 10
    head, _, tail = rest.partition(" ")
    if head.isdigit() and tail.strip():
        count, rest = int(head), tail.strip()
    keyword = rest.strip()
    if not keyword:
        raise CKError("$邮件搜索$ 格式：$邮件搜索 [槽位=名] [数量] 关键词$")
    ok, res = await fetch(keyword, count, user)
    if not ok:
        raise CKError(f"$邮件搜索$ 失败：{res}")
    return json.dumps([brief(m) for m in res], ensure_ascii=False)


async def _mail_info_action(rest: str) -> str:
    # $邮箱信息 [槽位=名]$ → JSON（当前槽位/全部槽位/登录状态）
    mod = _get_mail_module()
    user, _ = _mail_slot(mod, rest)
    status = await mod.get_auth_status(user)
    users = mod.list_users()
    return json.dumps({
        "槽位": user, "全部槽位": users,
        "已登录": bool(status.get("logged_in")),
        "状态": status.get("status") or ("logged_in" if status.get("logged_in") else "logged_out"),
    }, ensure_ascii=False)


_MAIL_ACTIONS = {
    "邮件发送": _mail_send_action, "邮件回复": _mail_reply_action,
    "邮件列表": _mail_list_action, "邮件读取": _mail_read_action,
    "邮件搜索": _mail_search_action, "邮箱信息": _mail_info_action,
}


def _module_status_json() -> str:
    """$模块状态$ → 各可选模块启用/可用情况 JSON。"""
    pw = _get_module("playwright")
    ds = _get_module("datastore")
    hosting = _get_module("image_hosting")
    onebot = _get_module("onebot_adapter")
    status = {
        "playwright": bool(pw is not None and pw.is_available()),
        "datastore": {
            "启用": ds is not None,
            "mysql": bool(ds is not None and ds.mysql_available()),
            "redis": bool(ds is not None and ds.redis_available()),
        },
        "image_hosting": hosting.status() if hosting is not None else {"启用": False},
        "onebot_adapter": onebot is not None,
    }
    return json.dumps(status, ensure_ascii=False)


def _save_render(data: bytes) -> str:
    """保存渲染结果到 data/渲染/，返回相对 data/ 的路径（可直接用于 ±img=±）。"""
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for f in RENDER_DIR.glob("*.png"):
        try:
            if now - f.stat().st_mtime > _RENDER_KEEP_SECONDS:
                f.unlink()
        except OSError:
            continue
    name = f"{int(now * 1000)}_{len(data) & 0xFFFF}.png"
    (RENDER_DIR / name).write_bytes(data)
    return f"渲染/{name}"


def _split_viewport(rest: str):
    """可选首参 宽x高 → ((宽,高), 剩余内容)，未指定时为 (None, 原内容)。"""
    parts = rest.split(" ", 1)
    if len(parts) == 2:
        m = _SIZE_RE.match(parts[0])
        if m:
            return (int(m.group(1)), int(m.group(2))), parts[1].strip()
    return None, rest


async def _render_action(rest: str) -> str:
    """$渲染 [宽x高] URL$ / $渲染 [宽x高] html HTML内容$ → 截图保存，返回本地路径。"""
    rest = rest.strip()
    viewport, rest = _split_viewport(rest)
    pw = _get_playwright()
    if rest.startswith("html "):
        img = await pw.screenshot_html(rest[5:], viewport=viewport, image_format="png")
    elif is_http_url(rest):
        await _assert_public_url(rest)
        img = await pw.screenshot_url(rest, viewport=viewport, image_format="png")
    else:
        raise CKError("$渲染$ 格式：$渲染 [宽x高] URL$ 或 $渲染 [宽x高] html HTML内容$")
    return _save_render(img)


async def _draw_action(rest: str) -> str:
    """$画图 [宽x高] 文字或HTML片段$ → 文字卡片图，返回本地路径。"""
    rest = rest.strip()
    viewport, content = _split_viewport(rest)
    if not content:
        raise CKError("$画图$ 格式：$画图 [宽x高] 文字或HTML片段$")
    content = content.replace("\\n", "<br>").replace("\\r", "<br>")
    html = _DRAW_TEMPLATE.replace("{{content}}", content)
    pw = _get_playwright()
    img = await pw.screenshot_html(html, viewport=viewport, full_page=True,
                                   image_format="png", selector=".card")
    return _save_render(img)


async def _image_bed_core(url: str, sender):
    """把图片备份到图床子频道，返回 (永久链接, 图片字节)。"""
    if not is_http_url(url):
        raise CKError("$图床$ 格式：$图床 图片URL$")
    bed = str(settings_load().get("image_bed_channel") or "")
    if not bed:
        raise CKError("未配置图床子频道：先发送 $图床频道 子频道ID$ 设置（机器人需在该频道）")
    data = await _download_bytes(url)
    md5 = hashlib.md5(data).hexdigest().upper()
    ok, resp = await sender.send_to_channel(bed, f"图床备份 MD5:{md5}", image=url)
    if not ok:
        raise CKError(f"$图床$ 备份到子频道失败: {resp}")
    return f"https://gchat.qpic.cn/qmeetpic/0/0-0-{md5}/0", data


def _media_actions(sender) -> dict:
    """渲染/画图/图床类函数表（事件上下文与定时任务共用）。"""

    async def render(rest):
        return await _render_action(rest)

    async def draw(rest):
        return await _draw_action(rest)

    async def image_bed(rest):
        link, _ = await _image_bed_core(rest.strip(), sender)
        return link

    async def md_image_bed(rest):
        # $MD图床 图片URL [宽 高]$ → 备份并转永久链接的 MD 图片片段，未指定尺寸时自动读取
        args = rest.split()
        if not args or not args[0]:
            raise CKError("$MD图床$ 格式：$MD图床 图片URL [宽 高]$")
        link, data = await _image_bed_core(args[0], sender)
        if len(args) >= 3 and args[1].isdigit() and args[2].isdigit():
            return f"![img #{args[1]}px #{args[2]}px]({link})"
        size = image_size_from_bytes(data)
        if size:
            return f"![img #{size[0]}px #{size[1]}px]({link})"
        return f"![img]({link})"

    async def upload_hosting(rest):
        # $上传图床 [类型] 图片URL或本地路径$，类型默认 qq
        hosting = _get_module("image_hosting")
        if hosting is None:
            raise CKError("图床服务未启用：需在框架模块页启用「图床服务」")
        args = rest.split()
        if not args:
            raise CKError("$上传图床$ 格式：$上传图床 [类型] 图片URL或本地路径$，类型："
                          + "/".join(_HOSTING_TYPES))
        kind = "qq"
        if args[0].lower() in _HOSTING_TYPES:
            kind = args.pop(0).lower()
        if not args:
            raise CKError("$上传图床$ 缺少图片URL或本地路径")
        data = await _load_image_arg(args[0])
        if kind == "qq":
            result = await hosting.upload_qq(data, token_manager=sender._token_mgr)
        elif kind == "cos":
            result = await hosting.upload_cos_url(data, "ck.png")
        elif kind == "bilibili":
            result = await hosting.upload_bilibili(data)
        elif kind == "chatglm":
            result = await hosting.upload_chatglm(data)
        elif kind == "ukaka":
            result = await hosting.upload_ukaka(data)
        elif kind == "xingye":
            result = await hosting.upload_xingye(data)
        else:
            result = await hosting.upload_nature(data)
        if isinstance(result, tuple):
            raise CKError(f"$上传图床$ {kind} 上传失败: {result[1]}")
        if not result:
            raise CKError(f"$上传图床$ {kind} 上传失败")
        return str(result)

    async def hosting_status(rest):
        hosting = _get_module("image_hosting")
        if hosting is None:
            raise CKError("图床服务未启用：需在框架模块页启用「图床服务」")
        return json.dumps(hosting.status(), ensure_ascii=False)

    async def module_status(rest):
        return _module_status_json()

    async def mysql_query(rest):
        return await _mysql_query_action(rest)

    async def mysql_exec(rest):
        return await _mysql_exec_action(rest)

    return {
        "渲染": render, "画图": draw, "图床": image_bed, "MD图床": md_image_bed,
        "上传图床": upload_hosting, "图床状态": hosting_status, "模块状态": module_status,
        "MySQL查": mysql_query, "MySQL执行": mysql_exec,
        "Redis读": _redis_get_action, "Redis写": _redis_set_action,
        "Redis删": _redis_del_action, "Redis自增": _redis_incr_action,
        **_MAIL_ACTIONS,
    }


def _parse_buttons(spec: str, small: bool = False):
    """±btn=文本;值|文本;值^下一行± → 框架按钮结构。

    值为 URL → 链接按钮；以 / 开头 → 填充输入框(type=2)；以 > 开头 → 普通指令按钮
    (type=2+enter，点击后以用户身份直接发送该指令)；其余 → 回调(type=1)。省略值时用文本。
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
            if is_http_url(value):
                btns.append({"text": text, "link": value})
            elif value.startswith("/"):
                btns.append({"text": text, "data": value, "type": 2})
            elif value.startswith(">"):
                data = value[1:].strip() or text
                btns.append({"text": text, "data": data, "type": 2, "enter": True})
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
    _append_errors(ctx)
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
        # ack 失败不应阻塞主逻辑，记录后继续
        logger.warning("interaction ack 失败", exc_info=True)
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

    # 回调事件不带 author/mentions, 昵称身份与机器人身份只读本地缓存
    # (不打成员接口, 避免阻塞导致回调响应超时)
    hit = engine.find_block(data) is not None
    bot_role = await _bot_role(event, cache_only=True) if hit else ""
    ctx = build_ctx(event, bot_role)
    ctx.message = data
    if hit:
        await _fill_member_vars(event, ctx)
    matched = await engine.handle(ctx)
    if not matched:
        return
    _append_errors(ctx)
    await send_ctx(event, ctx)
    return True


def _forum_plain_text(content) -> str:
    """论坛富文本 content(JSON 字符串) → 纯文本；解析失败时原样返回。"""
    if not content:
        return ""
    try:
        data = json.loads(content) if isinstance(content, str) else content
    except (ValueError, TypeError):
        return str(content)
    if not isinstance(data, dict):
        return str(content)
    parts = []
    for para in data.get("paragraphs") or []:
        for elem in (para or {}).get("elems") or []:
            text = ((elem or {}).get("text") or {}).get("text", "")
            if text:
                parts.append(text)
        parts.append("\n")
    return "".join(parts).strip() or str(content)


_FORUM_BLOCKS = {
    "THREAD_CREATE": "发帖通知",
    "POST_CREATE": "帖子评论通知",
    "REPLY_CREATE": "帖子回复通知",
}


@handler(r"^[\s\S]*$", name="词库帖子事件", desc="论坛发帖/评论/回复事件触发词库", priority=-100,
         event_types=["FORUM_THREAD_CREATE", "FORUM_POST_CREATE", "FORUM_REPLY_CREATE",
                      "OPEN_FORUM_THREAD_CREATE", "OPEN_FORUM_POST_CREATE",
                      "OPEN_FORUM_REPLY_CREATE"])
async def ck_forum(event, match):
    """帖子事件：有人发帖/评论帖子/回复评论时，触发词库里同名块（发帖通知/帖子评论通知/
    帖子回复通知），块不存在则忽略。公域(OPEN_FORUM_*)事件官方不带内容，仅有人物与频道。"""
    et = event.event_type or ""
    block_name = next((v for k, v in _FORUM_BLOCKS.items() if et.endswith(k)), "")
    if not block_name or engine.find_block(block_name) is None:
        return
    d = _event_d(event)
    info = d.get("thread_info") or d.get("post_info") or d.get("reply_info") or {}
    ctx = build_ctx(event)
    ctx.message = block_name
    ctx.user_id = ctx.user_id or str(d.get("author_id", "") or "")
    ctx.guild_id = ctx.guild_id or str(d.get("guild_id", "") or "")
    ctx.channel_id = ctx.channel_id or str(d.get("channel_id", "") or "")
    ctx.group_id = ctx.group_id or ctx.guild_id
    ctx.chat_type = "channel"
    ctx.extras.update({
        "帖子ID": str(info.get("thread_id", "") or ""),
        "评论ID": str(info.get("post_id", "") or ""),
        "回复ID": str(info.get("reply_id", "") or ""),
        "帖子标题": _forum_plain_text(info.get("title", "")),
        "帖子内容": _forum_plain_text(info.get("content", "")),
        "发布时间": str(info.get("date_time", "") or ""),
    })
    matched = await engine.handle(ctx)
    if not matched:
        return
    _append_errors(ctx)
    # 帖子事件没有可回复的消息端点，文本输出改为主动发到当前子频道
    text = "".join(o["content"] for o in ctx.outputs if o["type"] == "text").strip()
    if text and ctx.channel_id:
        try:
            await event.sender.send_to_channel(ctx.channel_id, text)
        except Exception as exc:
            report_error(PLUGIN, "词库", exc, context={
                "phase": "帖子事件输出", "block": block_name, "channel_id": ctx.channel_id})
    return True


_LIFECYCLE_BLOCKS = {
    "GROUP_MEMBER_ADD": "入群欢迎",
    "GROUP_MEMBER_REMOVE": "退群提示",
    "GROUP_ADD_ROBOT": "机器人入群",
    "GROUP_DEL_ROBOT": "机器人退群",
    "FRIEND_ADD": "添加好友",
    "FRIEND_DEL": "删除好友",
    "GUILD_MEMBER_ADD": "入频道欢迎",
    "GUILD_MEMBER_REMOVE": "退频道提示",
}


@handler(r"^[\s\S]*$", name="词库进退事件", desc="入群/退群/入频道/退频道等事件触发词库", priority=-100,
         event_types=list(_LIFECYCLE_BLOCKS))
async def ck_lifecycle(event, match):
    """进退事件触发词库里同名块（入群欢迎/退群提示/入频道欢迎/退频道提示等），块不存在则忽略。
    文本输出：群事件主动发到该群；频道事件发私信给该用户（可在块里用 $主动频道$ 发到子频道）。"""
    et = event.event_type or ""
    block_name = _LIFECYCLE_BLOCKS.get(et, "")
    if not block_name or engine.find_block(block_name) is None:
        return
    d = _event_d(event)
    user = d.get("user") or {}
    ctx = build_ctx(event)
    ctx.message = block_name
    ctx.user_id = ctx.user_id or str(user.get("id", "") or d.get("member_openid", "")
                                     or d.get("op_member_openid", "") or "")
    ctx.username = ctx.username or str(user.get("username", "") or d.get("nick", "") or "")
    ctx.guild_id = ctx.guild_id or str(d.get("guild_id", "") or "")
    if et.startswith("GUILD_"):
        ctx.group_id = ctx.group_id or ctx.guild_id
        ctx.chat_type = "channel"
    ctx.extras.update({
        "操作人ID": str(d.get("op_user_id", "") or d.get("op_member_openid", "") or ""),
    })
    matched = await engine.handle(ctx)
    if not matched:
        return
    _append_errors(ctx)
    text = "".join(o["content"] for o in ctx.outputs if o["type"] == "text").strip()
    if text:
        try:
            if et.startswith("GUILD_") and ctx.user_id:
                await ctx.actions["频道私聊"](ctx.user_id, text)
            elif event.group_id and et != "GROUP_DEL_ROBOT":
                await event.send_to_group(event.group_id, text)
            elif ctx.user_id and et in ("FRIEND_ADD", "FRIEND_DEL", "GROUP_DEL_ROBOT"):
                await event.send_to_user(ctx.user_id, text)
        except Exception as exc:
            report_error(PLUGIN, "词库", exc, context={
                "phase": "进退事件输出", "block": block_name, "event_type": et})
    return True


def _sched_actions(bot, task: dict) -> dict:
    """定时任务执行环境的函数表：无事件对象，基于 bot.sender 直发。"""
    sender = bot.sender

    async def send_to_user(uid, content):
        await sender.send_to_user(uid, content)

    async def send_to_group(gid, content):
        await sender.send_to_group(gid, content)

    async def send_to_channel(cid, content):
        await sender.send_to_channel(cid, content)

    async def force_wakeup(uid, content):
        await sender.force_wakeup(uid, content)

    async def send_guild_dm(uid, content):
        gid = task.get("guild_id") or ""
        ok, data = await sender._request(
            "POST", "/users/@me/dms",
            json={"recipient_id": uid, "source_guild_id": gid})
        dms_gid = str((data or {}).get("guild_id", "") or "") if ok else ""
        if not dms_gid:
            raise RuntimeError(f"创建频道私信会话失败: {data}")
        await sender._request("POST", f"/dms/{dms_gid}/messages", json={"content": content})

    async def share_link(data):
        return await sender.get_share_link(data)

    async def open_api(method, path, payload):
        kwargs = {}
        if payload is not None:
            kwargs["json"] = payload
        return await sender._request(method, path, **kwargs)

    actions = {
        "主动私聊": send_to_user,
        "主动群发": send_to_group,
        "主动频道": send_to_channel,
        "频道私聊": send_guild_dm,
        "强制召回": force_wakeup,
        "邀请链接": share_link,
        "官方API": open_api,
    }
    actions.update(_media_actions(sender))
    return actions


async def _sched_send_text(bot, task: dict, text: str) -> None:
    """定时任务文本输出默认发回创建任务时所在的会话。"""
    if task.get("channel_id"):
        await bot.sender.send_to_channel(task["channel_id"], text)
    elif task.get("group_id"):
        await bot.sender.send_to_group(task["group_id"], text)
    elif task.get("user_id"):
        await bot.sender.send_to_user(task["user_id"], text)


async def _sched_send_image(bot, task: dict, content: str) -> None:
    """定时任务图片输出：频道直发 URL，群/私聊走主动图片接口。"""
    if task.get("channel_id"):
        if is_http_url(content):
            await bot.sender.send_to_channel(task["channel_id"], "", image=content)
        else:
            logger.warning("定时任务频道图片仅支持 URL（可先用 $图床$ 转链）: %s", content)
        return
    if is_http_url(content):
        data = await _download_bytes(content)
    else:
        data, _ = _resolve_local_media(content)
        if data is None:
            logger.warning("定时任务图片不存在: %s", content)
            return
    if task.get("group_id"):
        await bot.sender.send_image("group", task["group_id"], data)
    elif task.get("user_id"):
        await bot.sender.send_image("user", task["user_id"], data)


async def _run_cron_task(name: str, task: dict) -> None:
    """到点执行定时任务：以任务指令触发词库块，文本输出主动发回目标会话。"""
    bot = _get_bot(task.get("appid"))
    if not bot:
        logger.warning("定时任务 %s 找不到 bot 实例 (appid=%s)", name, task.get("appid"))
        return

    async def send(c):
        text = "".join(s["content"] for s in c.outputs if s["type"] == "text").strip()
        if c.errors:
            text = (text + "\n⚠ " + "\n⚠ ".join(c.errors)).strip()
        if text:
            await _sched_send_text(bot, task, text)
        for seg in c.outputs:
            if seg["type"] == "image" and seg["content"]:
                await _sched_send_image(bot, task, seg["content"])

    ctx = Ctx(
        message=task.get("command", ""),
        user_id=task.get("user_id", ""),
        group_id=task.get("group_id", ""),
        guild_id=task.get("guild_id", ""),
        channel_id=task.get("channel_id", ""),
        appid=task.get("appid", ""),
        robot_name=bot.name or "",
        robot_qq=str(bot.robot_qq or ""),
        chat_type=task.get("chat_type", ""),
        extras={"事件类型": "定时任务", "任务名": name},
        send=send,
        actions=_sched_actions(bot, task),
    )
    try:
        handled = await engine.handle(ctx)
        if not handled:
            logger.warning("定时任务 %s 未命中任何词库指令: %s", name, task.get("command"))
            return
        await send(ctx)
    except Exception as exc:
        report_error(PLUGIN, "词库", exc, context={"phase": "定时任务", "task": name})


cron_manager = CronManager(_run_cron_task)
engine.cron_manager = cron_manager


@on_load
async def _init():
    engine.load()
    await engine.run_init()
    cron_manager.start()


@on_unload
async def _cleanup():
    cron_manager.stop()
