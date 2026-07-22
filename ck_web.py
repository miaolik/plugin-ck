#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""词库 Web 编辑器：词库文件管理 / 在线编辑 / 沙盒测试 / 全局变量 / 数据浏览。"""

import json
import re
from pathlib import Path

from aiohttp import web

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import on_load, on_unload
from core.plugin.web_pages import register_page, register_route, unregister_page

from .ck_engine import (
    BASE_DIR, DATA_DIR, DEFAULT_HTTP_TIMEOUT, DICT_DIR, Ctx, engine,
    disabled_dicts, globals_load, globals_save, http_timeout,
    set_dict_enabled, settings_load, settings_save,
)

PAGE_KEY = "ck-editor"

logger = get_logger(PLUGIN, "词库")

_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff\-]{1,64}$")


def _dict_path(name: str) -> Path:
    if not _NAME_RE.match(name):
        raise ValueError(f"词库名无效: {name}")
    return DICT_DIR / f"{name}.txt"


def _err(message: str, status: int = 400) -> web.Response:
    return web.json_response({"success": False, "message": message}, status=status)


async def _json_body(request):
    """解析请求体 JSON，返回 (data, error_response)；失败时 data 为 None。"""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return None, _err("请求体不是合法的 JSON")
    if not isinstance(body, dict):
        return None, _err("请求体必须是 JSON 对象")
    return body, None


@register_route("GET", "/api/ext/ck/dicts")
async def api_dicts(request):
    DICT_DIR.mkdir(parents=True, exist_ok=True)
    disabled = set(disabled_dicts())
    files = []
    for f in sorted(DICT_DIR.glob("*.txt")):
        text = f.read_text(encoding="utf-8", errors="replace")
        files.append({
            "name": f.stem,
            "size": f.stat().st_size,
            "mtime": int(f.stat().st_mtime),
            "lines": text.count("\n") + 1,
            "enabled": f.stem not in disabled,
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
    body, err = await _json_body(request)
    if err:
        return err
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
    body, err = await _json_body(request)
    if err:
        return err
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
    body, err = await _json_body(request)
    if err:
        return err
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


@register_route("POST", "/api/ext/ck/dict/toggle")
async def api_dict_toggle(request):
    body, err = await _json_body(request)
    if err:
        return err
    name = str(body.get("name", ""))
    try:
        path = _dict_path(name)
    except ValueError as exc:
        return _err(str(exc))
    if not path.exists():
        return _err("词库不存在", 404)
    enabled = bool(body.get("enabled", True))
    set_dict_enabled(name, enabled)
    engine.load()
    return web.json_response({"success": True, "name": name, "enabled": enabled,
                              "message": "已启用" if enabled else "已禁用"})


@register_route("POST", "/api/ext/ck/reload")
async def api_reload(request):
    engine.load()
    await engine.run_init()
    return web.json_response({"success": True, "message": "已重载",
                              "blocks": len(engine.blocks), "errors": engine.parse_errors})


@register_route("POST", "/api/ext/ck/test")
async def api_test(request):
    """沙盒测试：模拟一条消息触发词库，返回输出片段（不真正发送）。"""
    body, err = await _json_body(request)
    if err:
        return err
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


@register_route("POST", "/api/ext/ck/censor_test")
async def api_censor_test(request):
    """内容审核测试连通：可先保存百度密钥（留空则用内置接口），再实际调用一次审核。"""
    body, err = await _json_body(request)
    if err:
        return err
    key = str(body.get("baidu_key", "") or "").strip()
    secret = str(body.get("baidu_secret", "") or "").strip()
    data = globals_load()
    changed = False
    if key or secret:
        if not (key and secret):
            return _err("百度审核KEY 与 百度审核SECRET 需同时填写（或都留空用内置接口）")
        data["百度审核KEY"], data["百度审核SECRET"] = key, secret
        changed = True
    if body.get("clear_baidu"):
        data.pop("百度审核KEY", None)
        data.pop("百度审核SECRET", None)
        changed = True
    if changed:
        globals_save(data)
    text = str(body.get("text", "") or "").strip() or "你好"
    try:
        result = json.loads(await engine.censor_text(text))
    except Exception as exc:
        logger.warning("内容审核测试调用失败", exc_info=True)
        return web.json_response({"success": False, "message": f"审核调用失败: {exc}"})
    return web.json_response({"success": True, "provider": result.get("provider", ""),
                              "conclusion": result.get("conclusion", ""), "result": result})


@register_route("GET", "/api/ext/ck/settings")
async def api_settings_get(request):
    return web.json_response({"success": True, "settings": {"http_timeout": http_timeout()},
                              "defaults": {"http_timeout": DEFAULT_HTTP_TIMEOUT}})


@register_route("POST", "/api/ext/ck/settings")
async def api_settings_save(request):
    body, err = await _json_body(request)
    if err:
        return err
    try:
        value = int(body.get("http_timeout", DEFAULT_HTTP_TIMEOUT))
    except (TypeError, ValueError):
        return _err("http_timeout 必须是正整数（秒）")
    if value <= 0 or value > 3600:
        return _err("http_timeout 范围为 1-3600 秒")
    data = settings_load()
    data["http_timeout"] = value
    settings_save(data)
    return web.json_response({"success": True, "message": "已保存",
                              "settings": {"http_timeout": value}})


@register_route("GET", "/api/ext/ck/globals")
async def api_globals_get(request):
    return web.json_response({"success": True, "globals": globals_load()})


@register_route("POST", "/api/ext/ck/globals")
async def api_globals_save(request):
    body, err = await _json_body(request)
    if err:
        return err
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
    body, err = await _json_body(request)
    if err:
        return err
    rel = str(body.get("path", ""))
    target = (DATA_DIR / rel).resolve()
    if DATA_DIR.resolve() not in target.parents:
        return _err("路径无效")
    if not target.exists() or not target.is_file():
        return _err("文件不存在", 404)
    target.unlink()
    return web.json_response({"success": True, "message": "已删除"})


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
