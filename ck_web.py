#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""词库 Web 编辑器：词库文件管理 / 在线编辑 / 沙盒测试 / 全局变量 / 数据浏览。"""

import json
import re
from pathlib import Path

from aiohttp import web

from core.plugin.decorators import on_load, on_unload
from core.plugin.web_pages import register_page, register_route, unregister_page

from .ck_engine import (
    BASE_DIR, DATA_DIR, DICT_DIR, DEFAULT_HTTP_TIMEOUT, Ctx, engine,
    globals_load, globals_save, settings_load, settings_save,
)

PAGE_KEY = "ck-editor"

_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff\-]{1,64}$")


def _dict_path(name: str) -> Path:
    if not _NAME_RE.match(name):
        raise ValueError(f"词库名无效: {name}")
    return DICT_DIR / f"{name}.txt"


def _err(message: str, status: int = 400) -> web.Response:
    return web.json_response({"success": False, "message": message}, status=status)


@register_route("GET", "/api/ext/ck/dicts")
async def api_dicts(request):
    DICT_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for f in sorted(DICT_DIR.glob("*.txt")):
        text = f.read_text(encoding="utf-8", errors="replace")
        files.append({
            "name": f.stem,
            "size": f.stat().st_size,
            "mtime": int(f.stat().st_mtime),
            "lines": text.count("\n") + 1,
        })
    return web.json_response({
        "success": True,
        "dicts": files,
        "blocks": len(engine.blocks),
        "errors": engine.parse_errors,
    })


@register_route("GET", "/api/ext/ck/dict")
async def api_dict_get(request):
    name = request.query.get("name", "")
    try:
        path = _dict_path(name)
    except ValueError as exc:
        return _err(str(exc))
    if not path.exists():
        return _err("词库不存在", 404)
    return web.json_response({"success": True, "name": name,
                              "content": path.read_text(encoding="utf-8", errors="replace")})


@register_route("POST", "/api/ext/ck/dict/save")
async def api_dict_save(request):
    body = await request.json()
    name = str(body.get("name", "")).strip()
    content = str(body.get("content", ""))
    try:
        path = _dict_path(name)
    except ValueError as exc:
        return _err(str(exc))
    DICT_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    engine.load()
    await engine.run_init()
    return web.json_response({"success": True, "message": "已保存并重载",
                              "blocks": len(engine.blocks), "errors": engine.parse_errors})


@register_route("POST", "/api/ext/ck/dict/rename")
async def api_dict_rename(request):
    body = await request.json()
    try:
        old = _dict_path(str(body.get("name", "")).strip())
        new = _dict_path(str(body.get("new_name", "")).strip())
    except ValueError as exc:
        return _err(str(exc))
    if not old.exists():
        return _err("词库不存在", 404)
    if new.exists():
        return _err("目标名称已存在")
    old.rename(new)
    engine.load()
    await engine.run_init()
    return web.json_response({"success": True, "message": "已重命名"})


@register_route("POST", "/api/ext/ck/dict/delete")
async def api_dict_delete(request):
    body = await request.json()
    try:
        path = _dict_path(str(body.get("name", "")).strip())
    except ValueError as exc:
        return _err(str(exc))
    if not path.exists():
        return _err("词库不存在", 404)
    path.unlink()
    engine.load()
    await engine.run_init()
    return web.json_response({"success": True, "message": "已删除"})


@register_route("POST", "/api/ext/ck/reload")
async def api_reload(request):
    engine.load()
    await engine.run_init()
    return web.json_response({"success": True, "message": "已重载",
                              "blocks": len(engine.blocks), "errors": engine.parse_errors})


@register_route("POST", "/api/ext/ck/test")
async def api_test(request):
    """沙盒测试：模拟一条消息触发词库，返回输出片段（不真正发送）。"""
    body = await request.json()
    message = str(body.get("message", "")).strip()
    if not message:
        return _err("请输入测试消息")
    ctx = Ctx(
        message=message,
        user_id=str(body.get("user_id", "") or "TEST_USER"),
        username=str(body.get("username", "") or "测试用户"),
        group_id=str(body.get("group_id", "") or "TEST_GROUP"),
        guild_id=str(body.get("guild_id", "") or ""),
        channel_id=str(body.get("channel_id", "") or ""),
        message_id="TEST_MSG",
        appid="TEST_BOT",
        chat_type="group",
    )
    matched = await engine.handle(ctx)
    return web.json_response({
        "success": True,
        "matched": matched,
        "md_mode": ctx.md_mode,
        "outputs": ctx.outputs,
        "errors": ctx.errors,
    })


@register_route("GET", "/api/ext/ck/globals")
async def api_globals_get(request):
    return web.json_response({"success": True, "globals": globals_load()})


@register_route("POST", "/api/ext/ck/globals")
async def api_globals_save(request):
    body = await request.json()
    data = body.get("globals")
    if not isinstance(data, dict):
        return _err("globals 必须是对象")
    globals_save({str(k): str(v) for k, v in data.items()})
    return web.json_response({"success": True, "message": "已保存"})


@register_route("GET", "/api/ext/ck/data")
async def api_data_list(request):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for f in sorted(DATA_DIR.rglob("*")):
        if f.is_file():
            files.append({"path": str(f.relative_to(DATA_DIR)), "size": f.stat().st_size})
    return web.json_response({"success": True, "files": files})


@register_route("GET", "/api/ext/ck/data/content")
async def api_data_content(request):
    rel = request.query.get("path", "")
    target = (DATA_DIR / rel).resolve()
    if DATA_DIR.resolve() not in target.parents:
        return _err("路径无效")
    if not target.exists() or not target.is_file():
        return _err("文件不存在", 404)
    if target.stat().st_size > 512 * 1024:
        return _err("文件过大，无法预览")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _err("二进制文件，无法预览")
    return web.json_response({"success": True, "path": rel, "content": content})


@register_route("POST", "/api/ext/ck/data/delete")
async def api_data_delete(request):
    body = await request.json()
    rel = str(body.get("path", ""))
    target = (DATA_DIR / rel).resolve()
    if DATA_DIR.resolve() not in target.parents:
        return _err("路径无效")
    if not target.exists() or not target.is_file():
        return _err("文件不存在", 404)
    target.unlink()
    return web.json_response({"success": True, "message": "已删除"})


@register_route("GET", "/api/ext/ck/settings")
async def api_settings_get(request):
    settings = settings_load()
    settings.setdefault("http_timeout", DEFAULT_HTTP_TIMEOUT)
    return web.json_response({"success": True, "settings": settings})


@register_route("POST", "/api/ext/ck/settings")
async def api_settings_save(request):
    body = await request.json()
    try:
        timeout = int(str(body.get("http_timeout", "")).strip())
    except (TypeError, ValueError):
        return _err("超时必须是整数（秒）")
    if not 1 <= timeout <= 3600:
        return _err("超时范围 1-3600 秒")
    data = settings_load()
    data["http_timeout"] = timeout
    settings_save(data)
    return web.json_response({"success": True, "message": "已保存"})


@on_load
def _register_panel():
    register_page(
        key=PAGE_KEY,
        label="词库编辑器",
        source="plugin",
        source_name="ck",
        html_file=str(BASE_DIR / "web" / "page.html"),
        icon="book-open",
    )


@on_unload
def _unregister_panel():
    unregister_page(PAGE_KEY)
