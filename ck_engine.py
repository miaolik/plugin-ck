#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GQ 风格词库引擎：解析词库文本并按 GQ 语法执行。

词库结构：语句块之间以空行分隔，块首行为指令（正则触发词），其余为语句。
支持：局部/全局变量、系统变量、捕获变量、如果/分支/循环/返回、
$函数$（读写/文本处理/随机/排行榜/数据库/访问URL/下载/调用/回调等）、
[算式] 运算、@数组/JSON 取值、±img/video/voice/at/emoji/md/ark± 发送语句。
"""

import ast
import asyncio
import datetime
import ipaddress
import json
import random
import re
import socket
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import aiohttp

from core.base.logger import PLUGIN, get_logger, report_error

logger = get_logger(PLUGIN, "词库")

BASE_DIR = Path(__file__).resolve().parent
DICT_DIR = BASE_DIR / "dicts"
DATA_DIR = BASE_DIR / "data"
DB_DIR = DATA_DIR / "db"
GLOBAL_FILE = DATA_DIR / "全局变量.json"
SETTINGS_FILE = DATA_DIR / "设置.json"

MAX_LOOP = 1000
MAX_CALL_DEPTH = 10
DEFAULT_HTTP_TIMEOUT = 300
HTTP_MAX_BYTES = 200 * 1024

COMMENT_PREFIXES = ("//", "##", "&&")
INTERNAL_PREFIXES = ("[内部]", "#内部#")


class ReturnSignal(Exception):
    """`返回` 语句：提前结束当前指令。"""


class BreakSignal(Exception):
    """`跳出` 语句：结束当前循环。"""


class ContinueSignal(Exception):
    """`继续` 语句：跳到当前循环下一轮。"""


class CKError(Exception):
    """词库运行错误（附带给用户看的消息）。"""


def _ensure_dirs() -> None:
    DICT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)


def _safe_rel_path(base: Path, rel: str) -> Path:
    rel = rel.replace("\\", "/").strip().lstrip("/")
    if rel.startswith("data/"):
        rel = rel[5:]
    target = (base / rel).resolve()
    if base.resolve() not in target.parents and target != base.resolve():
        raise CKError(f"非法路径: {rel}")
    return target


def _ip_is_blocked(ip: str) -> bool:
    """判断解析出的 IP 是否指向内网/环回/链路本地/保留地址（SSRF 防护）。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


async def _assert_public_url(url: str) -> None:
    """校验 URL 仅指向公网地址，阻断访问内网/云元数据等 SSRF 目标。

    解析主机名的全部地址；任一命中内网/环回/链路本地/保留段即拒绝，
    可缓解 DNS 轮询绕过（无法完全消除 DNS rebinding，但显著提高门槛）。
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise CKError(f"URL 无效: {url}")
    host = parsed.hostname
    if not host:
        raise CKError(f"URL 无效: {url}")
    try:
        infos = await asyncio.get_event_loop().getaddrinfo(host, parsed.port,
                                                           proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise CKError(f"域名解析失败: {host} ({exc})")
    for info in infos:
        if _ip_is_blocked(info[4][0]):
            raise CKError(f"禁止访问内网/保留地址: {host}")


# ---------------------------------------------------------------------------
# 共享工具：JSON 文件读写 / HTTP 请求 / URL 判断
# ---------------------------------------------------------------------------

def load_json_dict(path: Path, *, label: str = "") -> Dict:
    """读取 JSON 文件为 dict；不存在或解析失败返回空 dict。

    label 非空时，内容非对象或读取失败会记一条 warning（否则静默）。"""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            if label:
                logger.warning("%s内容非对象，已忽略: %s", label, path)
        except (json.JSONDecodeError, OSError) as exc:
            if label:
                logger.warning("读取%s失败，改用默认值: %s (%s)", label, path, exc)
    return {}


def save_json_file(path: Path, data, *, indent: Optional[int] = None) -> None:
    """将数据写入 JSON 文件（自动建目录）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")


def is_http_url(url: str) -> bool:
    return url.startswith(("http://", "https://"))


def api_result(ok, data) -> str:
    """统一的 {"success":..,"data":..} JSON 字符串（QQ 开放平台调用结果）。"""
    return json.dumps({"success": bool(ok), "data": data}, ensure_ascii=False)


async def fetch_bytes(method: str, url: str, *, max_bytes: int,
                      headers: Optional[Dict[str, str]] = None, **req_kwargs) -> bytes:
    """按当前 http_timeout() 发起请求并读取至多 max_bytes 字节。"""
    timeout = aiohttp.ClientTimeout(total=http_timeout())
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.request(method, url, **req_kwargs) as resp:
            return await resp.content.read(max_bytes)


def image_size_from_bytes(data: bytes) -> Optional[Tuple[int, int]]:
    """从图片字节解析尺寸 (宽, 高)，支持 PNG/JPEG/GIF/WEBP，失败返回 None。"""
    if len(data) < 30:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8 ":
            return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                    int.from_bytes(data[28:30], "little") & 0x3FFF)
        if chunk == b"VP8L":
            b = data[21:25]
            w = 1 + (((b[1] & 0x3F) << 8) | b[0])
            h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
            return w, h
        if chunk == b"VP8X":
            return (1 + int.from_bytes(data[24:27], "little"),
                    1 + int.from_bytes(data[27:30], "little"))
        return None
    if data[:2] == b"\xff\xd8":  # JPEG：扫描 SOF 段
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                return None
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (int.from_bytes(data[i + 7:i + 9], "big"),
                        int.from_bytes(data[i + 5:i + 7], "big"))
            i += 2 + seg_len
    return None


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

class Block:
    def __init__(self, trigger: str, lines: List[str], internal: bool, source: str, lineno: int):
        self.trigger = trigger
        self.lines = lines
        self.internal = internal
        self.source = source
        self.lineno = lineno
        try:
            self.pattern: Optional[re.Pattern] = re.compile(trigger)
        except re.error:
            self.pattern = None


def parse_dict_text(text: str, source: str) -> Tuple[List[Block], List[str], List[str]]:
    """解析词库文本，返回 (语句块列表, 初始化语句, 错误列表)。"""
    blocks: List[Block] = []
    init_lines: List[str] = []
    errors: List[str] = []
    cur: List[Tuple[int, str]] = []
    in_js = False

    def flush() -> None:
        if not cur:
            return
        lineno, first = cur[0]
        body = [ln for _, ln in cur[1:]]
        if first == "#INITROBOT#":
            init_lines.extend(body)
            return
        internal = False
        trigger = first
        for p in INTERNAL_PREFIXES:
            if trigger.startswith(p):
                internal = True
                trigger = trigger[len(p):].strip()
                break
        blk = Block(trigger, body, internal, source, lineno)
        if blk.pattern is None:
            errors.append(f"{source}:{lineno} 触发词正则无效: {trigger}")
        blocks.append(blk)

    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if stripped == "#JAVASCRIPTSTART#":
            in_js = True
            errors.append(f"{source}:{i} 不支持 JS 脚本块，已跳过")
            continue
        if stripped == "#JAVASCRIPTEND#":
            in_js = False
            continue
        if in_js:
            continue
        if not stripped:
            flush()
            cur = []
            continue
        if stripped.startswith(COMMENT_PREFIXES):
            continue
        cur.append((i, stripped))
    flush()
    return blocks, init_lines, errors


# ---------------------------------------------------------------------------
# 数据存储：data/路径 文件内 "键 = 值"
# ---------------------------------------------------------------------------

def store_read(path: str, key: str, default: str) -> str:
    f = _safe_rel_path(DATA_DIR, path)
    if not f.exists() or f.is_dir():
        return default
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip()
    return default


def store_write(path: str, key: str, value: str) -> None:
    f = _safe_rel_path(DATA_DIR, path)
    f.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    found = False
    if f.exists():
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            k, sep, _ = line.partition("=")
            if sep and k.strip() == key:
                lines.append(f"{key} = {value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key} = {value}")
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")


def store_keys(path: str) -> List[str]:
    """返回配置文件内所有键（按文件顺序）。"""
    f = _safe_rel_path(DATA_DIR, path)
    if not f.exists() or f.is_dir():
        return []
    keys = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        k, sep, _ = line.partition("=")
        if sep and k.strip():
            keys.append(k.strip())
    return keys


def store_delete(path: str) -> bool:
    f = _safe_rel_path(DATA_DIR, path)
    if f.exists() and f.is_file():
        f.unlink()
        return True
    return False


def settings_load() -> Dict[str, object]:
    return load_json_dict(SETTINGS_FILE, label="设置文件")


def settings_save(data: Dict[str, object]) -> None:
    save_json_file(SETTINGS_FILE, data, indent=2)


def disabled_dicts() -> List[str]:
    """被禁用的词库文件名列表（不含 .txt），禁用的文件不参与触发。"""
    value = settings_load().get("disabled_dicts", [])
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def set_dict_enabled(name: str, enabled: bool) -> None:
    data = settings_load()
    raw = data.get("disabled_dicts")
    disabled = {str(v) for v in raw} if isinstance(raw, list) else set()
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    data["disabled_dicts"] = sorted(disabled)
    settings_save(data)


def http_timeout() -> int:
    """URL 访问/下载超时秒数，可在 Web 端「设置」中自定义，默认 300 秒（5 分钟）。"""
    try:
        value = int(settings_load().get("http_timeout", DEFAULT_HTTP_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_HTTP_TIMEOUT
    return value if value > 0 else DEFAULT_HTTP_TIMEOUT


def globals_load() -> Dict[str, str]:
    return {str(k): str(v) for k, v in load_json_dict(GLOBAL_FILE, label="全局变量文件").items()}


def globals_save(data: Dict[str, str]) -> None:
    save_json_file(GLOBAL_FILE, data, indent=2)


# ---------------------------------------------------------------------------
# 排行榜：JSON 文件 {"键": [{"参数": 值}, ...]}，按值降序
# ---------------------------------------------------------------------------

def rank_write(path: str, key: str, member: str, value: str) -> None:
    f = _safe_rel_path(DATA_DIR, path)
    data: Dict[str, List[Dict[str, float]]] = load_json_dict(f, label="排行榜文件")
    items = data.setdefault(key, [])
    value = _eval_arith_brackets(value).strip()
    try:
        num = float(value)
    except ValueError:
        raise CKError(f"排行榜值必须是数字: {value}")
    items[:] = [it for it in items if list(it.keys()) != [member]]
    items.append({member: int(num) if num == int(num) else num})
    items.sort(key=lambda it: list(it.values())[0], reverse=True)
    save_json_file(f, data, indent=2)


def rank_read(path: str, key: str, mode: str, index: str) -> str:
    f = _safe_rel_path(DATA_DIR, path)
    items = load_json_dict(f, label="排行榜文件").get(key)
    if not isinstance(items, list):
        return ""
    try:
        idx = int(index)
        item = items[idx]
    except (ValueError, IndexError):
        return ""
    member, value = next(iter(item.items()))
    return str(member) if mode == "参数" else str(value)


# ---------------------------------------------------------------------------
# 算式与条件
# ---------------------------------------------------------------------------

_ARITH_RE = re.compile(r"^[0-9+\-*/%(). ]+$")

# 允许的算术运算（不含幂运算 **，避免 9**9**9 之类的指数爆炸导致 DoS）
_ARITH_BINOPS = {ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod}
_ARITH_UNARYOPS = {ast.UAdd, ast.USub}


def _safe_arith_eval(node: ast.AST) -> float:
    """在 AST 上安全求值纯算术表达式，只放行白名单运算符与数字字面量。"""
    if isinstance(node, ast.Expression):
        return _safe_arith_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("仅支持数字")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ARITH_BINOPS:
        left = _safe_arith_eval(node.left)
        right = _safe_arith_eval(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        return left % right
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ARITH_UNARYOPS:
        operand = _safe_arith_eval(node.operand)
        return operand if isinstance(node.op, ast.UAdd) else -operand
    raise ValueError("非法算术表达式")


def calc_expr(expr: str) -> Optional[str]:
    """计算纯算术表达式；非算式返回 None。"""
    expr = expr.strip()
    if not expr or not _ARITH_RE.match(expr) or not re.search(r"\d", expr):
        return None
    if not re.search(r"[+\-*/%]", expr):
        return None
    try:
        result = _safe_arith_eval(ast.parse(expr, "<calc>", "eval"))
    except (SyntaxError, ZeroDivisionError, ValueError, TypeError, RecursionError):
        return None
    if isinstance(result, float):
        if result == int(result):
            return str(int(result))
        return str(round(result, 10))
    if isinstance(result, int):
        return str(result)
    return None


def _eval_arith_brackets(text: str) -> str:
    """将文本中 [算式] 替换为计算结果（仅纯算术内容）。"""
    def repl(m: re.Match) -> str:
        inner = m.group(1)
        result = calc_expr(inner)
        return result if result is not None else m.group(0)
    return re.sub(r"\[([^\[\]]+)\]", repl, text)


_COND_OPS = ["==", "!=", ">=", "<=", ">", "<"]


def eval_condition(cond: str, local_vars: Optional[Dict[str, str]] = None) -> bool:
    cond = cond.strip()
    for op in _COND_OPS:
        if op in cond:
            left, _, right = cond.partition(op)
            left, right = left.strip(), right.strip()
            if local_vars:
                # GQ 允许循环/如果条件里直接写裸变量名（如 循环:i<=30）
                left = local_vars.get(left, left)
                right = local_vars.get(right, right)
            lc, rc = calc_expr(left), calc_expr(right)
            left = lc if lc is not None else left
            right = rc if rc is not None else right
            try:
                ln, rn = float(left), float(right)
                pair: Tuple[float, float] = (ln, rn)
            except ValueError:
                pair = None  # type: ignore[assignment]
            if pair is not None:
                ln, rn = pair
                return {"==": ln == rn, "!=": ln != rn, ">=": ln >= rn,
                        "<=": ln <= rn, ">": ln > rn, "<": ln < rn}[op]
            return {"==": left == right, "!=": left != right, ">=": left >= right,
                    "<=": left <= right, ">": left > right, "<": left < right}[op]
    return cond not in ("", "0", "false", "False", "假", "否", "NULL", "null")


# ---------------------------------------------------------------------------
# JSON / 数组取值：@数据 [路径]
# ---------------------------------------------------------------------------

def _json_pick(data, path: str):
    """按 [a][b]、[[1][2]] 或 [data[0].键] 形式取值。"""
    path = path.strip()
    if path.startswith("[") and path.endswith("]"):
        path = path[1:-1]
    tokens: List[str] = []
    for chunk in re.split(r"\]\s*\[", path):
        for seg in chunk.split("."):
            for tok in re.findall(r"[^\[\]\s]+", seg):
                tokens.append(tok)
    cur = data
    for key in tokens:
        if isinstance(cur, list):
            try:
                cur = cur[int(key)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if key in cur:
                cur = cur[key]
            else:
                return None
        else:
            return None
    return cur


def _parse_array_or_json(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if text.startswith("[") and text.endswith("]"):
        return _parse_index_array(text)
    return None


def _parse_index_array(text: str) -> list:
    """解析 GQ 索引数组 [1,2,[a,b]]（元素不带引号）。"""
    text = text.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return [text]
    inner = text[1:-1]
    items: List[str] = []
    depth = 0
    cur = ""
    for ch in inner:
        if ch == "[":
            depth += 1
            cur += ch
        elif ch == "]":
            depth -= 1
            cur += ch
        elif ch in ",，" and depth == 0:
            items.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip() or items:
        items.append(cur.strip())
    return [_parse_index_array(it) if it.startswith("[") else it for it in items]


def array_to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _index_array_to_text(arr) -> str:
    if isinstance(arr, list):
        return "[" + ",".join(_index_array_to_text(x) for x in arr) + "]"
    return str(arr)


# ---------------------------------------------------------------------------
# 运行上下文
# ---------------------------------------------------------------------------

class Ctx:
    """一次触发的运行上下文；outputs 收集待发送片段。"""

    def __init__(self, *, message: str = "", user_id: str = "", username: str = "",
                 group_id: str = "", guild_id: str = "", channel_id: str = "",
                 message_id: str = "", appid: str = "", robot_name: str = "",
                 avatar: str = "", role: str = "", chat_type: str = "",
                 robot_qq: str = "", ats: Optional[List[str]] = None,
                 images: Optional[List[str]] = None, raw_json: str = "",
                 extras: Optional[Dict[str, str]] = None,
                 send: Optional[Callable] = None,
                 recall: Optional[Callable] = None,
                 actions: Optional[Dict[str, Callable]] = None):
        self.message = message
        self.user_id = user_id
        self.username = username
        self.group_id = group_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message_id = message_id
        self.appid = appid
        self.robot_name = robot_name
        self.avatar = avatar
        self.role = role
        self.chat_type = chat_type
        self.robot_qq = robot_qq
        self.ats = ats or []
        self.images = images or []
        self.raw_json = raw_json
        self.extras = extras or {}
        self.send = send
        self.recall = recall
        self.actions = actions or {}
        self.vars: Dict[str, str] = {}
        self.match: Optional[re.Match] = None
        self.outputs: List[Dict[str, str]] = []
        self.md_mode = False
        self.text_mode = False      # ±文本± 强制纯文本
        self.skip_suffix = False    # ±无后缀± 跳过全局 markdown 后缀
        self.auto_delete = 0        # ±自动撤回=秒± 发送后自动撤回
        self.errors: List[str] = []

    def out_text(self, text: str) -> None:
        if self.outputs and self.outputs[-1]["type"] == "text":
            self.outputs[-1]["content"] += text
        else:
            self.outputs.append({"type": "text", "content": text})

    def out(self, kind: str, content: str, extra: str = "") -> None:
        self.outputs.append({"type": kind, "content": content, "extra": extra})

    def child(self, message: str) -> "Ctx":
        """派生子上下文（$调用$ / $回调$）：复制发送者/会话信息与发送回调。"""
        return Ctx(message=message, user_id=self.user_id, username=self.username,
                   group_id=self.group_id, guild_id=self.guild_id, channel_id=self.channel_id,
                   message_id=self.message_id, appid=self.appid, robot_name=self.robot_name,
                   avatar=self.avatar, role=self.role, chat_type=self.chat_type,
                   robot_qq=self.robot_qq, ats=self.ats, images=self.images,
                   raw_json=self.raw_json, extras=self.extras, send=self.send,
                   recall=self.recall, actions=self.actions)


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class CKEngine:
    def __init__(self):
        self.blocks: List[Block] = []
        self.init_lines: List[str] = []
        self.parse_errors: List[str] = []
        self.databases: Dict[str, Path] = {}
        self.loaded_at = 0.0
        self.cron_manager = None  # 由 main 注入（ck_cron.CronManager）

    # ---- 加载 ----

    def load(self) -> None:
        _ensure_dirs()
        blocks: List[Block] = []
        init_lines: List[str] = []
        errors: List[str] = []
        disabled = set(disabled_dicts())
        for f in sorted(DICT_DIR.glob("*.txt")):
            if f.stem in disabled:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                errors.append(f"{f.name}: 读取失败 {exc}")
                continue
            b, i, e = parse_dict_text(text, f.name)
            blocks.extend(b)
            init_lines.extend(i)
            errors.extend(e)
        self.blocks = blocks
        self.init_lines = init_lines
        self.parse_errors = errors
        self.loaded_at = time.time()

    async def run_init(self) -> None:
        if not self.init_lines:
            return
        ctx = Ctx(message="#INITROBOT#")
        try:
            await self._run_lines(self.init_lines, ctx, 0)
        except (ReturnSignal, BreakSignal, ContinueSignal):
            pass
        except CKError as exc:
            self.parse_errors.append(f"初始化失败: {exc}")
        except Exception as exc:
            report_error(PLUGIN, "词库", exc, context={"phase": "#INITROBOT#"})
            self.parse_errors.append(f"初始化异常: {exc}")

    # ---- 触发 ----

    def find_block(self, message: str, *, internal: bool = False) -> Optional[Tuple[Block, re.Match]]:
        for blk in self.blocks:
            if blk.internal != internal or blk.pattern is None:
                continue
            m = blk.pattern.fullmatch(message)
            if m:
                return blk, m
        return None

    async def handle(self, ctx: Ctx) -> bool:
        found = self.find_block(ctx.message)
        if not found:
            return False
        blk, m = found
        ctx.match = m
        try:
            await self._run_lines(blk.lines, ctx, 0)
        except (ReturnSignal, BreakSignal, ContinueSignal):
            pass
        except CKError as exc:
            ctx.errors.append(str(exc))
        except Exception as exc:
            report_error(PLUGIN, "词库", exc, context={
                "trigger": blk.trigger, "source": f"{blk.source}:{blk.lineno}",
                "message": ctx.message, "user_id": ctx.user_id})
            ctx.errors.append(f"内部错误: {exc}")
        return True

    async def run_command(self, command: str, ctx: Ctx, depth: int, *, internal_only: bool = False) -> None:
        """执行指定指令（用于 $调用$ / $回调$）。"""
        if depth > MAX_CALL_DEPTH:
            raise CKError("调用层数过深")
        found = self.find_block(command, internal=True)
        if not found and not internal_only:
            found = self.find_block(command)
        if not found:
            raise CKError(f"未找到指令: {command}")
        blk, m = found
        sub = ctx.child(command)
        sub.vars.update(ctx.vars)  # 子块可读调用方局部变量
        sub.match = m
        signal: Optional[Exception] = None
        try:
            await self._run_lines(blk.lines, sub, depth + 1)
        except ReturnSignal:
            pass
        except (BreakSignal, ContinueSignal) as exc:
            signal = exc  # 跳出/继续 穿透回调，作用于调用方所在循环
        ctx.vars.update(sub.vars)  # 子块赋值回传, [内部]块可当查表/子程序用
        ctx.errors.extend(sub.errors)
        # 合并输出（回调语义）
        for seg in sub.outputs:
            if seg["type"] == "text":
                ctx.out_text(seg["content"])
            else:
                ctx.outputs.append(seg)
        ctx.md_mode = ctx.md_mode or sub.md_mode
        ctx.text_mode = ctx.text_mode or sub.text_mode
        ctx.skip_suffix = ctx.skip_suffix or sub.skip_suffix
        ctx.auto_delete = ctx.auto_delete or sub.auto_delete
        if signal is not None:
            raise signal

    # ---- 执行 ----

    async def _run_lines(self, lines: List[str], ctx: Ctx, depth: int) -> None:
        i = 0
        n = len(lines)
        loop_guard = 0
        while i < n:
            loop_guard += 1
            if loop_guard > MAX_LOOP * 10:
                raise CKError("执行步数超限")
            raw = lines[i]

            if raw == "返回":
                raise ReturnSignal()
            if raw == "跳出":
                raise BreakSignal()
            if raw == "继续":
                raise ContinueSignal()

            if raw.startswith("跳转:") or raw.startswith("跳转："):
                # 跳转:行号 —— 跳到本层第 N 行（1 起）继续执行，配合 如果 可实现条件跳转
                target = (await self._expand(raw[3:], ctx, depth)).strip()
                if not target.isdigit() or not (1 <= int(target) <= n):
                    raise CKError(f"跳转 行号无效: {target}（本层共 {n} 行）")
                i = int(target) - 1
                continue

            if raw.startswith("如果:") or raw.startswith("如果："):
                cond_src = raw[3:]
                cond = await self._expand(cond_src, ctx, depth)
                if eval_condition(cond, ctx.vars):
                    i += 1
                else:
                    i = self._skip_to(lines, i, "如果:", "如果尾") + 1
                continue
            if raw == "如果尾":
                i += 1
                continue

            if raw.startswith("分支:") or raw.startswith("分支："):
                value = (await self._expand(raw[3:], ctx, depth)).strip()
                i = await self._run_switch(lines, i, value, ctx, depth)
                continue

            if raw.startswith("循环遍历:") or raw.startswith("循环遍历："):
                i = await self._run_foreach(lines, i, ctx, depth)
                continue
            if raw.startswith("循环:") or raw.startswith("循环："):
                i = await self._run_loop(lines, i, ctx, depth)
                continue
            if raw == "结束" or raw == "分支尾":
                i += 1
                continue

            # 局部变量赋值：第二个字符是 :
            if len(raw) >= 2 and raw[1] in ":：":
                name = raw[0]
                value = await self._expand(raw[2:], ctx, depth)
                ctx.vars[name] = _eval_arith_brackets(value)
                i += 1
                continue

            # 普通输出语句
            text = await self._expand(raw, ctx, depth)
            text = _eval_arith_brackets(text)
            self._emit(text, ctx)
            i += 1

    def _skip_to(self, lines: List[str], start: int, open_kw: str, close_kw: str) -> int:
        depth = 0
        for j in range(start + 1, len(lines)):
            line = lines[j]
            if line.startswith(open_kw) or line.startswith(open_kw.replace(":", "：")):
                depth += 1
            elif line == close_kw:
                if depth == 0:
                    return j
                depth -= 1
        return len(lines) - 1

    async def _run_switch(self, lines: List[str], start: int, value: str, ctx: Ctx, depth: int) -> int:
        end = self._skip_to(lines, start, "分支:", "分支尾")
        # 找到匹配的 情况:/default:
        j = start + 1
        chosen: List[str] = []
        current_match = False
        matched_any = False
        default_body: List[str] = []
        in_default = False
        nest = 0
        while j < end:
            line = lines[j]
            if line.startswith("分支:") or line.startswith("分支："):
                nest += 1
            elif line == "分支尾" and nest > 0:
                nest -= 1
            if nest > 0:
                if current_match:
                    chosen.append(line)
                elif in_default:
                    default_body.append(line)
                j += 1
                continue
            if line.startswith("情况:") or line.startswith("情况："):
                case_val = (await self._expand(line[3:], ctx, depth)).strip()
                current_match = (case_val == value) and not matched_any
                in_default = False
                if current_match:
                    matched_any = True
            elif line.startswith("default:") or line.startswith("default："):
                in_default = True
                current_match = False
            elif current_match:
                chosen.append(line)
            elif in_default:
                default_body.append(line)
            j += 1
        body = chosen if matched_any else default_body
        await self._run_lines(body, ctx, depth)
        return end + 1

    async def _run_loop(self, lines: List[str], start: int, ctx: Ctx, depth: int) -> int:
        end = self._skip_to(lines, start, "循环", "结束")
        cond_src = lines[start][3:]
        body = lines[start + 1:end]
        count = 0
        while True:
            cond = await self._expand(cond_src, ctx, depth)
            if not eval_condition(cond, ctx.vars):
                break
            count += 1
            if count > MAX_LOOP:
                raise CKError(f"循环超过 {MAX_LOOP} 次，已强制结束")
            try:
                await self._run_lines(body, ctx, depth)
            except BreakSignal:
                break
            except ContinueSignal:
                continue
        return end + 1

    async def _run_foreach(self, lines: List[str], start: int, ctx: Ctx, depth: int) -> int:
        """循环遍历:数据 项变量 [序变量] —— 遍历 JSON 数组/对象或逗号分隔文本。"""
        end = self._skip_to(lines, start, "循环", "结束")
        head = lines[start][5:]
        body = lines[start + 1:end]
        parts = head.rsplit(" ", 2)
        if len(parts) >= 2 and len(parts[-1]) == 1 and len(parts[-2]) == 1:
            data_src, item_var, idx_var = " ".join(parts[:-2]), parts[-2], parts[-1]
        elif len(parts) >= 2 and len(parts[-1]) == 1:
            data_src, item_var, idx_var = " ".join(parts[:-1]), parts[-1], ""
        else:
            raise CKError("循环遍历 格式：循环遍历:数据 项变量 [序变量]（变量名均为单字符）")
        data_text = (await self._expand(data_src, ctx, depth)).strip()
        items: List[Tuple[str, str]] = []
        try:
            parsed = json.loads(data_text)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            items = [(str(idx), it if isinstance(it, str) else json.dumps(it, ensure_ascii=False))
                     for idx, it in enumerate(parsed)]
        elif isinstance(parsed, dict):
            items = [(str(k), v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
                     for k, v in parsed.items()]
        elif data_text:
            items = [(str(idx), piece) for idx, piece in enumerate(data_text.split(","))]
        if len(items) > MAX_LOOP:
            raise CKError(f"循环遍历超过 {MAX_LOOP} 项，已强制结束")
        for idx, item in items:
            ctx.vars[item_var] = item
            if idx_var:
                ctx.vars[idx_var] = idx
            try:
                await self._run_lines(body, ctx, depth)
            except BreakSignal:
                break
            except ContinueSignal:
                continue
        return end + 1

    # ---- 文本展开：%变量% → $函数$ → @数组取值 ----

    async def _expand(self, text: str, ctx: Ctx, depth: int) -> str:
        text = self._sub_vars(text, ctx)
        text = await self._sub_funcs(text, ctx, depth)
        text = self._sub_vars(text, ctx)  # 函数结果中可能引用变量占位
        text = self._sub_array_access(text)
        return text

    def _sub_vars(self, text: str, ctx: Ctx) -> str:
        if "%" not in text:
            return text

        def repl(m: re.Match) -> str:
            name = m.group(1)
            val = self._var_value(name, ctx)
            return val if val is not None else m.group(0)

        prev = None
        # 循环替换支持 %%随机数1-3%% 这种“变量名里嵌变量”的写法
        for _ in range(5):
            if text == prev:
                break
            prev = text
            text = re.sub(r"%([^%\n]+?)%", repl, text)
        return text

    def _var_value(self, name: str, ctx: Ctx) -> Optional[str]:
        if name in ctx.vars:
            return ctx.vars[name]
        if name in ("ID", "UserId"):
            return ctx.user_id
        if name in ("昵称", "UserName"):
            return ctx.username
        if name in ("头像", "UserAvatar"):
            return ctx.avatar
        if name in ("群ID", "群号", "GroupId"):
            return ctx.group_id
        if name in ("频道号", "GuildId"):
            return ctx.guild_id or ctx.group_id
        if name in ("子频道号", "ChannelId"):
            return ctx.channel_id
        if name in ("robot", "ROBOT", "机器人ID"):
            return ctx.appid
        if name in ("robotName", "机器人昵称"):
            return ctx.robot_name
        if name in ("Msgbar", "newMsgID", "消息ID"):
            return ctx.message_id
        if name in ("身份", "MemberRole", "member_role"):
            return ctx.role
        if name in ("场景", "ChatType", "消息场景"):
            return ctx.chat_type
        if name in ("机器人QQ", "robotQQ"):
            return ctx.robot_qq
        if name in ("AT数量", "AT个数"):
            return str(len(ctx.ats))
        if name in ("图片数量", "IMG数量"):
            return str(len(ctx.images))
        if name == "JSON":
            return ctx.raw_json
        if name in ("Time", "NDTime", "毫秒戳"):
            return str(int(time.time() * 1000))
        if name == "秒戳":
            return str(int(time.time()))
        if name in ctx.extras:
            return ctx.extras[name]
        if name in ("时间戳", "消息时间"):
            return ""
        if name.startswith("时间"):
            return self._format_time(name[2:])
        m = re.fullmatch(r"随机数(-?\d+)-(-?\d+)", name)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            return str(random.randint(min(lo, hi), max(lo, hi)))
        m = re.fullmatch(r"随机数([a-zA-Z])-([a-zA-Z])", name)
        if m:
            lo, hi = ord(m.group(1)), ord(m.group(2))
            return chr(random.randint(min(lo, hi), max(lo, hi)))
        m = re.fullmatch(r"AT(\d+)", name)
        if m:
            idx = int(m.group(1))
            return ctx.ats[idx] if idx < len(ctx.ats) else ""
        m = re.fullmatch(r"IMG(\d+)", name)
        if m:
            idx = int(m.group(1))
            return ctx.images[idx] if idx < len(ctx.images) else ""
        m = re.fullmatch(r"参数(-?\d+)", name)
        if m:
            idx = int(m.group(1))
            if idx == -1:
                return ctx.message
            parts = ctx.message.split()
            return parts[idx] if 0 <= idx < len(parts) else ""
        m = re.fullmatch(r"括号(\d+)", name)
        if m and ctx.match:
            idx = int(m.group(1))
            try:
                return ctx.match.group(idx) or ""
            except (IndexError, re.error):
                return ""
        if name.startswith("全局."):
            return globals_load().get(name[3:], "")
        if name in ctx.extras:
            return ctx.extras[name]
        return None

    @staticmethod
    def _format_time(fmt: str) -> str:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))  # 北京时间 UTC+8
        weekdays = "一二三四五六日"
        out = fmt
        out = out.replace("yyyy", now.strftime("%Y"))
        out = out.replace("MM", now.strftime("%m"))
        out = out.replace("dd", now.strftime("%d"))
        out = out.replace("HH", now.strftime("%H"))
        out = out.replace("hh", now.strftime("%I"))
        out = out.replace("mm", now.strftime("%M"))
        out = out.replace("ss", now.strftime("%S"))
        out = out.replace("E", "星期" + weekdays[now.weekday()])
        return out

    # ---- 函数 ----

    async def _sub_funcs(self, text: str, ctx: Ctx, depth: int) -> str:
        if "$" not in text:
            return text
        result = ""
        rest = text
        guard = 0
        while "$" in rest:
            guard += 1
            if guard > 50:
                break
            start = rest.index("$")
            end = rest.find("$", start + 1)
            if end < 0:
                break
            call = rest[start + 1:end].strip()
            result += rest[:start]
            rest = rest[end + 1:]
            try:
                result += await self._call_func(call, ctx, depth)
            except CKError as exc:
                ctx.errors.append(str(exc))
        return result + rest

    async def _call_func(self, call: str, ctx: Ctx, depth: int) -> str:
        parts = call.split(" ", 1)
        name = parts[0]
        rest = parts[1] if len(parts) > 1 else ""

        if name == "读":
            args = rest.rsplit(" ", 2)
            if len(args) != 3:
                raise CKError(f"$读$ 参数错误: {rest}")
            return store_read(args[0], args[1], args[2])
        if name == "写":
            args = rest.split(" ", 2)
            if len(args) < 2:
                raise CKError(f"$写$ 参数错误: {rest}")
            path, key = args[0], args[1]
            value = args[2] if len(args) > 2 else ""
            store_write(path, key, _eval_arith_brackets(value).strip())
            return ""
        if name == "删除":
            store_delete(rest.strip())
            return ""
        if name == "读键列表":
            return json.dumps(store_keys(rest.strip()), ensure_ascii=False)
        if name == "数组长":
            data_text = rest.strip()
            try:
                parsed = json.loads(data_text)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (list, dict)):
                return str(len(parsed))
            return str(len(data_text.split(","))) if data_text else "0"
        if name == "全局读":
            args = rest.split(" ", 1)
            key = args[0]
            default = args[1] if len(args) > 1 else ""
            return globals_load().get(key, default)
        if name == "全局写":
            args = rest.split(" ", 1)
            if not args[0]:
                raise CKError("$全局写$ 缺少变量名")
            data = globals_load()
            data[args[0]] = _eval_arith_brackets(args[1] if len(args) > 1 else "").strip()
            globals_save(data)
            return ""
        if name == "全局删":
            key = rest.strip()
            if not key:
                raise CKError("$全局删$ 缺少变量名")
            data = globals_load()
            data.pop(key, None)
            globals_save(data)
            return ""
        if name == "撤回":
            if not ctx.recall:
                raise CKError("$撤回$ 当前环境不支持")
            await ctx.recall(rest.strip())
            return ""

        if name == "字符串长":
            return str(len(rest))
        if name == "字符包含":
            args = rest.rsplit(" ", 1)
            if len(args) != 2:
                raise CKError("$字符包含$ 参数错误")
            return "true" if args[1] in args[0] else "false"
        if name in ("替换", "取中间", "分割", "正则", "数组处理"):
            return self._text_func(name, rest)

        if name == "是否为数字":
            return "true" if re.fullmatch(r"-?\d+(\.\d+)?", rest.strip()) else "false"
        if name == "随机数":
            args = rest.split()
            if len(args) != 2:
                raise CKError("$随机数$ 参数错误")
            lo, hi = int(float(args[0])), int(float(args[1]))
            return str(random.randint(min(lo, hi), max(lo, hi)))
        if name == "随机字母":
            args = rest.split()
            count = int(args[0]) if args else 5
            mode = int(args[1]) if len(args) > 1 else 2
            pools = {0: "abcdefghijklmnopqrstuvwxyz",
                     1: "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                     2: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"}
            pool = pools.get(mode, pools[2])
            return "".join(random.choice(pool) for _ in range(min(count, 100)))
        if name == "随机汉字":
            args = rest.split()
            count = int(args[0]) if args else 5
            return "".join(chr(random.randint(0x4E00, 0x9FA5)) for _ in range(min(count, 100)))
        if name == "随机英文数字":
            count = int(rest.strip() or 5)
            pool = "abcdefghijklmnopqrstuvwxyz0123456789"
            return "".join(random.choice(pool) for _ in range(min(count, 100)))
        if name == "计算":
            result = calc_expr(rest)
            if result is None:
                raise CKError(f"$计算$ 表达式无效: {rest}")
            return result

        if name == "URLEncoder":
            return urllib.parse.quote(rest, safe="")
        if name == "URLDecoder":
            return urllib.parse.unquote(rest)

        if name == "MD图片":
            # $MD图片 URL [宽 高]$ → Markdown 图片语法，可在一条 ±md± 消息里放多张；
            # 不填宽高时自动下载图片读取尺寸
            args = rest.split()
            if not args or not is_http_url(args[0]):
                raise CKError("$MD图片$ 格式：$MD图片 URL [宽 高]$（URL 须为 http/https 链接）")
            url = args[0]
            if len(args) >= 3 and args[1].isdigit() and args[2].isdigit():
                return f"![img #{args[1]}px #{args[2]}px]({url})"
            size = await self._probe_image_size(url)
            if size:
                return f"![img #{size[0]}px #{size[1]}px]({url})"
            return f"![img]({url})"

        if name in ("渲染", "画图", "图床", "MD图床",
                    "上传图床", "图床状态", "模块状态",
                    "MySQL查", "MySQL执行",
                    "Redis读", "Redis写", "Redis删", "Redis自增",
                    "邮件发送", "邮件回复", "邮件列表",
                    "邮件读取", "邮件搜索", "邮箱信息"):
            action = ctx.actions.get(name)
            if not action:
                raise CKError(f"${name}$ 当前环境不支持")
            return await action(rest)

        if name == "图床频道":
            cid = rest.strip()
            if not cid:
                raise CKError("$图床频道$ 格式：$图床频道 子频道ID$")
            data = settings_load()
            data["image_bed_channel"] = cid
            settings_save(data)
            return ""
        if name == "MD表格":
            # $MD表格 @ 表头1|表头2@a|b@c|d$ → Markdown 表格（首参为行分隔符，列用 | 分隔）
            sep, _, body = rest.partition(" ")
            if not sep or not body:
                raise CKError("$MD表格$ 格式：$MD表格 @ 表头1|表头2@a|b@c|d$")
            rows = [r for r in body.split(sep) if r.strip()]
            if not rows:
                raise CKError("$MD表格$ 至少需要表头行")
            cols = len(rows[0].split("|"))
            lines = ["|" + rows[0] + "|", "|" + "|".join(["---"] * cols) + "|"]
            lines += ["|" + r + "|" for r in rows[1:]]
            return "\n".join(lines)

        if name == "MD代码":
            # $MD代码 内容$ / $MD代码 语言=python 内容$ → Markdown 代码框
            lang = ""
            content = rest
            if content.startswith("语言="):
                head, _, tail = content.partition(" ")
                lang, content = head[3:], tail
            content = content.replace("\\n", "\n").replace("\\r", "\n")
            return f"```{lang}\n{content}\n```"

        if name == "排行榜写":
            args = rest.split()
            if len(args) != 4:
                raise CKError("$排行榜写$ 参数错误")
            rank_write(args[0], args[1], args[2], args[3])
            return ""
        if name == "排行榜读":
            args = rest.split()
            if len(args) != 4:
                raise CKError("$排行榜读$ 参数错误")
            return rank_read(args[0], args[1], args[2], args[3])

        if name == "数据库":
            return self._db_func(rest)

        if name == "访问":
            return await self._http_get(rest.strip())
        if name == "POST访问":
            args = rest.split(" ", 1)
            return await self._http_post(args[0], args[1] if len(args) > 1 else "")
        if name == "百度审核":
            return await self._baidu_censor(rest.strip())
        if name == "内容审核":
            return await self.censor_text(rest.strip())
        if name == "下载":
            args = rest.split(" ", 1)
            if len(args) != 2:
                raise CKError("$下载$ 参数错误")
            return await self._download(args[0], args[1].strip())

        if name == "调用":
            args = rest.split(" ", 1)
            if len(args) != 2:
                raise CKError("$调用$ 参数错误")
            try:
                delay_ms = int(args[0])
            except ValueError:
                raise CKError(f"$调用$ 延迟无效: {args[0]}")
            command = args[1]
            asyncio.ensure_future(self._delayed_call(delay_ms / 1000, command, ctx, depth))
            return ""
        if name == "回调":
            await self.run_command(rest.strip(), ctx, depth, internal_only=True)
            return ""

        if name in ("定时添加", "定时删除", "定时列表", "定时开关"):
            mgr = self.cron_manager
            if mgr is None:
                raise CKError(f"${name}$ 当前环境不支持")
            if name == "定时列表":
                return mgr.list_json()
            if name == "定时删除":
                mgr.remove(rest.strip())
                return ""
            if name == "定时开关":
                args = rest.split()
                if len(args) != 2 or args[1] not in ("0", "1"):
                    raise CKError("$定时开关$ 格式：$定时开关 名字 1/0$")
                mgr.toggle(args[0], args[1] == "1")
                return ""
            # 定时添加 名字 分 时 日 月 周 指令
            parts = rest.split(" ", 6)
            if len(parts) != 7:
                raise CKError("$定时添加$ 格式：$定时添加 名字 分 时 日 月 周 指令$")
            mgr.add(parts[0], " ".join(parts[1:6]), parts[6].strip(), ctx)
            return ""

        if name in ("主动私聊", "主动群发", "主动频道", "频道私聊", "召回", "强制召回"):
            action = ctx.actions.get(name)
            if not action:
                raise CKError(f"${name}$ 当前环境不支持")
            args = rest.split(" ", 1)
            if len(args) != 2 or not args[0] or not args[1]:
                raise CKError(f"${name}$ 格式：${name} 目标ID 内容$")
            await action(args[0].strip(), args[1])
            return ""
        if name == "邀请链接":
            action = ctx.actions.get(name)
            if not action:
                raise CKError("$邀请链接$ 当前环境不支持")
            return str(await action(rest.strip() or ctx.user_id) or "")
        if name == "群成员":
            action = ctx.actions.get(name)
            if not action:
                raise CKError("$群成员$ 当前环境不支持")
            uid = rest.strip() or ctx.user_id
            member = await action(uid)
            if not member:
                raise CKError("$群成员$ 查询失败（需在群聊中使用；查他人需对方在本群发过言）")
            return json.dumps(member, ensure_ascii=False)
        if name == "机器人成员":
            action = ctx.actions.get(name)
            if not action:
                raise CKError("$机器人成员$ 当前环境不支持")
            member = await action()
            if not member:
                raise CKError("$机器人成员$ 查询失败（需在群聊中@过机器人一次后可用）")
            return json.dumps(member, ensure_ascii=False)
        if name in self._GUILD_FUNC_NAMES:
            return await self._guild_func(name, rest, ctx)
        if name == "官方API":
            action = ctx.actions.get(name)
            if not action:
                raise CKError("$官方API$ 当前环境不支持")
            parts = rest.strip().split(" ", 2)
            if len(parts) < 2:
                raise CKError("$官方API$ 格式：$官方API 方法 /路径 JSON体$")
            method = parts[0].upper()
            if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                raise CKError("$官方API$ 方法需为 GET/POST/PUT/PATCH/DELETE")
            path = parts[1]
            if not path.startswith("/"):
                raise CKError("$官方API$ 路径需以 / 开头，如 /v2/groups/群ID/messages")
            payload = None
            body = parts[2].strip() if len(parts) > 2 else ""
            if body:
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise CKError(f"$官方API$ JSON体无效: {exc}")
            ok, result = await action(method, path, payload)
            return api_result(ok, result)

        raise CKError(f"未知函数: ${name}$")

    async def _probe_image_size(self, url: str) -> Optional[Tuple[int, int]]:
        """下载图片头部字节并解析尺寸，失败返回 None（不报错）。"""
        if not is_http_url(url):
            return None
        try:
            await _assert_public_url(url)
            data = await fetch_bytes("GET", url, max_bytes=256 * 1024)
        except (CKError, aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return None
        return image_size_from_bytes(data)

    # 频道管理函数：基于 QQ 频道 v1 接口，仅频道场景可用，机器人需相应权限
    _GUILD_FUNC_NAMES = ("频道撤回", "频道禁言", "频道全员禁言", "频道踢人", "频道拉黑",
                         "身份组列表", "身份组加", "身份组减", "发帖", "删帖", "帖子列表", "帖子详情")

    async def _guild_func(self, name: str, rest: str, ctx: Ctx) -> str:
        """频道管理：禁言/撤回/踢人/拉黑/身份组/发帖删帖，返回 {"success":..,"data":..}。"""
        api = (ctx.actions or {}).get("官方API")
        if not api:
            raise CKError(f"${name}$ 当前环境不支持")
        gid, cid = ctx.guild_id, ctx.channel_id
        if not gid:
            raise CKError(f"${name}$ 仅频道场景可用")
        args = rest.split()

        def need(n: int, usage: str) -> None:
            if len(args) < n or any(not a for a in args[:n]):
                raise CKError(f"${name}$ 格式：{usage}")

        method: str
        payload = None
        if name == "频道撤回":
            need(1, f"${name} 消息ID$")
            method, path = "DELETE", f"/channels/{cid}/messages/{args[0]}?hidetip=true"
        elif name == "频道禁言":
            need(2, f"${name} 用户ID 秒数$（秒数 0=解除）")
            method, path = "PATCH", f"/guilds/{gid}/members/{args[0]}/mute"
            payload = {"mute_seconds": str(args[1])}
        elif name == "频道全员禁言":
            need(1, f"${name} 秒数$（秒数 0=解除）")
            method, path = "PATCH", f"/guilds/{gid}/mute"
            payload = {"mute_seconds": str(args[0])}
        elif name == "频道踢人":
            need(1, f"${name} 用户ID$")
            method, path = "DELETE", f"/guilds/{gid}/members/{args[0]}"
        elif name == "频道拉黑":
            need(1, f"${name} 用户ID$")
            method, path = "DELETE", f"/guilds/{gid}/members/{args[0]}"
            payload = {"add_blacklist": True}
        elif name == "身份组列表":
            method, path = "GET", f"/guilds/{gid}/roles"
        elif name == "身份组加":
            need(2, f"${name} 用户ID 身份组ID$")
            method, path = "PUT", f"/guilds/{gid}/members/{args[0]}/roles/{args[1]}"
            payload = {"channel": {"id": cid}}
        elif name == "身份组减":
            need(2, f"${name} 用户ID 身份组ID$")
            method, path = "DELETE", f"/guilds/{gid}/members/{args[0]}/roles/{args[1]}"
            payload = {"channel": {"id": cid}}
        elif name == "发帖":
            # $发帖 [子频道ID] [格式] 标题 内容$：格式 文本/html/md/json（默认文本）
            tokens = rest.split(" ")
            if tokens and tokens[0].isdigit() and len(tokens[0]) >= 5:
                cid = tokens.pop(0)
            fmt = 1
            fmt_map = {"文本": 1, "text": 1, "html": 2, "md": 3, "markdown": 3, "json": 4}
            if tokens and tokens[0].lower() in fmt_map:
                fmt = fmt_map[tokens.pop(0).lower()]
            title = tokens.pop(0) if tokens else ""
            content = " ".join(tokens).strip()
            if not title or not content:
                raise CKError(f"${name}$ 格式：${name} [子频道ID] [文本/html/md/json] 标题 内容$（需论坛子频道）")
            method, path = "PUT", f"/channels/{cid}/threads"
            payload = {"title": title, "content": content, "format": fmt}
        elif name == "删帖":
            need(1, f"${name} 帖子ID$（可选前置子频道ID：${name} 子频道ID 帖子ID$）")
            if len(args) >= 2 and args[0].isdigit() and len(args[0]) >= 5:
                cid, args = args[0], args[1:]
            method, path = "DELETE", f"/channels/{cid}/threads/{args[0]}"
        elif name == "帖子列表":
            if args and args[0].isdigit() and len(args[0]) >= 5:
                cid = args[0]
            method, path = "GET", f"/channels/{cid}/threads"
        elif name == "帖子详情":
            need(1, f"${name} 帖子ID$（可选前置子频道ID：${name} 子频道ID 帖子ID$）")
            if len(args) >= 2 and args[0].isdigit() and len(args[0]) >= 5:
                cid, args = args[0], args[1:]
            method, path = "GET", f"/channels/{cid}/threads/{args[0]}"
        else:
            raise CKError(f"未知函数: ${name}$")
        ok, result = await api(method, path, payload)
        return api_result(ok, result)

    def _text_func(self, name: str, rest: str) -> str:
        sep_and_payload = rest.split(" ", 1)
        if len(sep_and_payload) != 2:
            raise CKError(f"${name}$ 参数错误")
        sep, payload = sep_and_payload
        if name == "数组处理":
            # $数组处理 add 分隔符@ 数组@元素$
            mode = sep
            sub = payload.split(" ", 1)
            if len(sub) != 2:
                raise CKError("$数组处理$ 参数错误")
            sep2, payload2 = sub
            parts = payload2.split(sep2)
            if len(parts) != 2:
                raise CKError("$数组处理$ 参数错误")
            arr = _parse_index_array(parts[0].strip())
            elem = parts[1].strip()
            if mode == "add":
                arr.append(elem)
            elif mode == "del":
                for idx, item in enumerate(arr):
                    if str(item) == elem:
                        del arr[idx]
                        break
            else:
                raise CKError(f"$数组处理$ 不支持: {mode}")
            return _index_array_to_text(arr)
        parts = payload.split(sep)
        if name == "替换":
            if len(parts) != 3:
                raise CKError("$替换$ 参数错误")
            return parts[0].replace(parts[1], parts[2])
        if name == "取中间":
            if len(parts) != 3:
                raise CKError("$取中间$ 参数错误")
            src, left, right = parts
            li = src.find(left)
            if li < 0:
                return ""
            ri = src.find(right, li + len(left))
            if ri < 0:
                return ""
            return src[li + len(left):ri]
        if name == "分割":
            if len(parts) != 2:
                raise CKError("$分割$ 参数错误")
            return _index_array_to_text(parts[0].split(parts[1]))
        if name == "正则":
            if len(parts) != 2:
                raise CKError("$正则$ 参数错误")
            src, pattern = parts
            pattern = pattern.strip()
            if pattern.startswith("(") and pattern.endswith(")"):
                pattern = pattern[1:-1]
            try:
                return src if re.search(pattern, src) else ""
            except re.error as exc:
                raise CKError(f"$正则$ 表达式无效: {exc}")
        raise CKError(f"未知函数: ${name}$")

    def _db_func(self, rest: str) -> str:
        args = rest.split(" ", 2)
        if len(args) < 3:
            raise CKError("$数据库$ 参数错误")
        action, db_name, payload = args
        if action == "声明":
            path = _safe_rel_path(DB_DIR, payload.strip())
            path.parent.mkdir(parents=True, exist_ok=True)
            self.databases[db_name] = path
            return json.dumps({"data": None, "errorMsg": "", "status": 0}, ensure_ascii=False)
        if db_name not in self.databases:
            return json.dumps({"data": None, "errorMsg": f"数据库 {db_name} 未声明", "status": -1},
                              ensure_ascii=False)
        db_path = self.databases[db_name]
        if action == "执行SQL":
            try:
                with sqlite3.connect(db_path) as conn:
                    conn.execute(payload)
                    conn.commit()
                return json.dumps({"data": None, "errorMsg": "", "status": 0}, ensure_ascii=False)
            except sqlite3.Error as exc:
                return json.dumps({"data": None, "errorMsg": str(exc), "status": -1}, ensure_ascii=False)
        if action == "查询SQL":
            try:
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(payload).fetchall()
                data = [dict(r) for r in rows]
                return json.dumps({"data": data, "errorMsg": "", "status": len(data)}, ensure_ascii=False)
            except sqlite3.Error as exc:
                return json.dumps({"data": None, "errorMsg": str(exc), "status": -1}, ensure_ascii=False)
        if action in ("执行SQLP", "查询SQLP"):
            # 参数化 SQL：$数据库 执行SQLP 名 @ SQL@值1@值2$，SQL 内用 ? 占位，
            # 值以 sqlite 参数绑定传入，杜绝把变量拼进 SQL 造成的注入
            sub = payload.split(" ", 1)
            if len(sub) != 2:
                raise CKError(f"$数据库 {action}$ 格式：$数据库 {action} 名 分隔符 SQL分隔符值...$")
            sep, body = sub
            parts = body.split(sep)
            sql, params = parts[0], [p.strip() for p in parts[1:]]
            try:
                with sqlite3.connect(db_path) as conn:
                    if action == "查询SQLP":
                        conn.row_factory = sqlite3.Row
                        rows = conn.execute(sql, params).fetchall()
                        data = [dict(r) for r in rows]
                        return json.dumps({"data": data, "errorMsg": "", "status": len(data)},
                                          ensure_ascii=False)
                    conn.execute(sql, params)
                    conn.commit()
                return json.dumps({"data": None, "errorMsg": "", "status": 0}, ensure_ascii=False)
            except (sqlite3.Error, sqlite3.Warning) as exc:
                return json.dumps({"data": None, "errorMsg": str(exc), "status": -1}, ensure_ascii=False)
        raise CKError(f"$数据库$ 不支持: {action}")

    # 百度云文本审核：access_token 缓存 (token, 过期时间戳)
    _baidu_token: Optional[Tuple[str, float]] = None

    async def censor_text(self, text: str) -> str:
        """$内容审核 文本$：自动选接口——配了百度密钥走百度云，否则用内置接口。
        统一返回 {"success":..,"conclusion":"合规|不合规|疑似","provider":"baidu|elaina","data":..}。"""
        if not text:
            raise CKError("$内容审核$ 格式：$内容审核 文本$")
        conf = globals_load()
        if conf.get("百度审核KEY", "") and conf.get("百度审核SECRET", ""):
            body = await self._baidu_censor(text)
            data = json.loads(body)
            data["provider"] = "baidu"
            return json.dumps(data, ensure_ascii=False)
        body = await self._elaina_censor(text)
        return body

    _ELAINA_CENSOR_URL = "https://i.elaina.vin/api/审核系统.php"
    _ELAINA_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

    async def _elaina_censor(self, text: str) -> str:
        """内置审核接口（冷曦API）：safe=1 合规，其余不合规。"""
        url = self._ELAINA_CENSOR_URL + "?text=" + urllib.parse.quote_plus(text)
        try:
            raw = await fetch_bytes("GET", url, max_bytes=HTTP_MAX_BYTES,
                                    headers={"User-Agent": self._ELAINA_UA})
            body = raw.decode("utf-8", errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise CKError(f"$内容审核$ 内置接口访问失败: {exc}")
        try:
            data = json.loads(body)
        except ValueError:
            raise CKError(f"$内容审核$ 内置接口返回异常: {body[:200]}")
        result = data.get("result") if isinstance(data, dict) else None
        ok = isinstance(data, dict) and data.get("status") == "success" and isinstance(result, dict)
        safe = result.get("safe") if ok else None
        conclusion = "" if not ok else ("合规" if str(safe) == "1" else "不合规")
        return json.dumps({"success": ok, "conclusion": conclusion, "provider": "elaina",
                           "data": data}, ensure_ascii=False)

    async def _baidu_censor(self, text: str) -> str:
        """$百度审核 文本$：百度云文本内容审核。密钥存全局变量
        百度审核KEY / 百度审核SECRET，返回 {"success":..,"conclusion":"合规|不合规|疑似|审核失败","data":..}。"""
        if not text:
            raise CKError("$百度审核$ 格式：$百度审核 文本$")
        conf = globals_load()
        ak = conf.get("百度审核KEY", "")
        sk = conf.get("百度审核SECRET", "")
        if not ak or not sk:
            raise CKError("$百度审核$ 未配置密钥：先 $全局写 百度审核KEY 你的APIKey$ 和 $全局写 百度审核SECRET 你的SecretKey$")
        now = time.time()
        cached = CKEngine._baidu_token
        token = cached[0] if cached and cached[1] > now else ""
        if not token:
            body = await self._http_post(
                "https://aip.baidubce.com/oauth/2.0/token?grant_type=client_credentials"
                f"&client_id={urllib.parse.quote(ak)}&client_secret={urllib.parse.quote(sk)}", "")
            try:
                data = json.loads(body)
            except ValueError:
                data = {}
            token = data.get("access_token", "") if isinstance(data, dict) else ""
            if not token:
                raise CKError(f"$百度审核$ 获取token失败: {body[:200]}")
            expires = float(data.get("expires_in", 2592000))
            CKEngine._baidu_token = (token, now + expires - 60)
        body = await self._http_post(
            "https://aip.baidubce.com/rest/2.0/solution/v1/text_censor/v2/user_defined"
            f"?access_token={urllib.parse.quote(token)}",
            "text=" + urllib.parse.quote_plus(text))
        try:
            data = json.loads(body)
        except ValueError:
            raise CKError(f"$百度审核$ 返回异常: {body[:200]}")
        conclusion = data.get("conclusion", "") if isinstance(data, dict) else ""
        return json.dumps({"success": bool(conclusion), "conclusion": conclusion, "data": data},
                          ensure_ascii=False)

    async def _http_get(self, url: str) -> str:
        if not is_http_url(url):
            raise CKError(f"$访问$ URL 无效: {url}")
        await _assert_public_url(url)
        try:
            body = await fetch_bytes("GET", url, max_bytes=HTTP_MAX_BYTES)
            return body.decode("utf-8", errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise CKError(f"$访问$ 失败: {exc}")

    async def _http_post(self, url: str, data: str) -> str:
        if not is_http_url(url):
            raise CKError(f"$POST访问$ URL 无效: {url}")
        await _assert_public_url(url)
        kwargs: Dict[str, object] = {}
        data = data.strip()
        if data.startswith("{"):
            try:
                kwargs["json"] = json.loads(data)
            except json.JSONDecodeError:
                kwargs["data"] = data
        elif data:
            kwargs["data"] = dict(urllib.parse.parse_qsl(data)) or data
        try:
            body = await fetch_bytes("POST", url, max_bytes=HTTP_MAX_BYTES, **kwargs)
            return body.decode("utf-8", errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise CKError(f"$POST访问$ 失败: {exc}")

    async def _download(self, local_path: str, url: str) -> str:
        if not is_http_url(url):
            raise CKError(f"$下载$ URL 无效: {url}")
        await _assert_public_url(url)
        target = _safe_rel_path(DATA_DIR, local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            content = await fetch_bytes("GET", url, max_bytes=20 * 1024 * 1024)
            target.write_bytes(content)
            return ""
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise CKError(f"$下载$ 失败: {exc}")

    async def _delayed_call(self, delay: float, command: str, ctx: Ctx, depth: int) -> None:
        await asyncio.sleep(delay)
        sub = ctx.child(command)
        try:
            await self.run_command(command, sub, depth)
        except (ReturnSignal, BreakSignal, ContinueSignal):
            pass
        except CKError as exc:
            logger.warning("$调用$ 延迟指令执行失败 (command=%s): %s", command, exc)
            return
        except Exception as exc:
            report_error(PLUGIN, "词库", exc, context={"phase": "$调用$", "command": command})
            return
        if sub.send:
            try:
                await sub.send(sub)
            except Exception as exc:
                report_error(PLUGIN, "词库", exc,
                             context={"phase": "$调用$ 发送", "command": command})

    # ---- 数组取值与发送语句 ----

    def _sub_array_access(self, text: str) -> str:
        """处理 @数组或JSON [路径] 取值。"""
        if "@" not in text:
            return text

        def repl(m: re.Match) -> str:
            data = _parse_array_or_json(m.group(1))
            if data is None:
                return m.group(0)
            value = _json_pick(data, m.group(2))
            return array_to_text(value) if value is not None else ""

        return re.sub(r"@((?:\[[^@]*?\]|\{[^@]*?\}))\s+(\[[^\s]+\])", repl, text)

    _SEND_RE = re.compile(
        r"±(img|image|video|voice|record|file|at|emoji|ark|md|btn|按钮|小按钮|引用|quote"
        r"|文本|text|无后缀|自动撤回)(?:=([^±]*))?±")

    def _emit(self, text: str, ctx: Ctx) -> None:
        text = text.replace("\\n", "\n").replace("\\r", "\n")
        pos = 0
        for m in self._SEND_RE.finditer(text):
            before = text[pos:m.start()]
            if before:
                ctx.out_text(before)
            kind, value = m.group(1), m.group(2) or ""
            if kind in ("img", "image"):
                ctx.out("image", value)
            elif kind == "video":
                ctx.out("video", value)
            elif kind in ("voice", "record"):
                ctx.out("voice", value)
            elif kind == "file":
                ctx.out("file", value)
            elif kind == "at":
                ctx.out_text(f"@{value} " if value != "0" else "@全体成员 ")
            elif kind == "emoji":
                ctx.out_text(f"[emoji:{value}]")
            elif kind == "md":
                ctx.md_mode = True
            elif kind in ("文本", "text"):
                ctx.text_mode = True
            elif kind == "无后缀":
                ctx.skip_suffix = True
            elif kind == "自动撤回":
                try:
                    ctx.auto_delete = max(1, int(value))
                except ValueError:
                    ctx.errors.append(f"±自动撤回=秒± 秒数无效: {value}")
            elif kind == "ark":
                ctx.out("ark", value)
            elif kind == "btn":
                ctx.out("buttons", value)
            elif kind == "按钮":
                ctx.out("buttons", value)
            elif kind == "小按钮":
                ctx.out("buttons_small", value)
            elif kind in ("引用", "quote"):
                ctx.out("quote", "")
            pos = m.end()
        tail = text[pos:]
        if tail:
            ctx.out_text(tail)


engine = CKEngine()
