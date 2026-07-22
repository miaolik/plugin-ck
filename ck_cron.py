#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""定时任务：cron 表达式（分 时 日 月 周）定时触发词库指令。

任务持久化在 data/定时任务.json，随插件加载自动恢复；
到点后以创建时所在会话为目标执行指令块（文本输出主动发回该会话，
块内也可用 $主动群发/$主动私聊/$主动频道/$发帖/$访问/$官方API$ 等函数）。
"""

import asyncio
import json
import time
from datetime import datetime
from typing import Callable, Dict, Optional

from .ck_engine import DATA_DIR, CKError, load_json_dict, save_json_file

CRON_FILE = DATA_DIR / "定时任务.json"
MAX_TASKS = 100

_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))  # 分 时 日 月 周(0/7=周日)
_FIELD_NAMES = ("分", "时", "日", "月", "周")


def _parse_field(expr: str, lo: int, hi: int, name: str) -> Optional[set]:
    """解析单个 cron 字段 → 允许值集合；'*' 返回 None（不限）。"""
    values: set = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            raise CKError(f"cron {name} 字段为空")
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise CKError(f"cron {name} 字段步长无效: {step_s}")
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise CKError(f"cron {name} 字段范围无效: {part}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise CKError(f"cron {name} 字段无效: {part}")
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise CKError(f"cron {name} 字段超出范围 {lo}-{hi}: {part}")
        values.update(range(start, end + 1, step))
    if values == set(range(lo, hi + 1)):
        return None
    return values


def parse_cron(expr: str) -> tuple:
    """'分 时 日 月 周' → 5 元组（每项为允许值集合，None=不限）。"""
    fields = expr.split()
    if len(fields) != 5:
        raise CKError("cron 表达式须为 5 个字段：分 时 日 月 周")
    parsed = []
    for f, (lo, hi), name in zip(fields, _FIELD_RANGES, _FIELD_NAMES):
        parsed.append(_parse_field(f, lo, hi, name))
    return tuple(parsed)


def cron_match(expr: str, dt: datetime) -> bool:
    """当前分钟是否命中 cron 表达式（日/周同时受限时按标准 cron 取或）。"""
    minute, hour, dom, month, dow = parse_cron(expr)
    if minute is not None and dt.minute not in minute:
        return False
    if hour is not None and dt.hour not in hour:
        return False
    if month is not None and dt.month not in month:
        return False
    wd = (dt.weekday() + 1) % 7  # datetime: 周一=0 → cron: 周日=0
    dow_ok = dow is None or wd in dow or (7 in dow and wd == 0)
    dom_ok = dom is None or dt.day in dom
    if dom is not None and dow is not None:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


class CronManager:
    """定时任务管理：增删改查 + 每分钟调度循环。runner(task) 由 main 注入。"""

    def __init__(self, runner: Callable):
        self._runner = runner
        self.tasks: Dict[str, dict] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._last_minute = ""
        self.load()

    def load(self) -> None:
        self.tasks = load_json_dict(CRON_FILE, label="定时任务文件")

    def save(self) -> None:
        save_json_file(CRON_FILE, self.tasks)

    def add(self, name: str, cron: str, command: str, ctx) -> None:
        parse_cron(cron)  # 校验表达式
        if not name or not command:
            raise CKError("$定时添加$ 名字与指令不能为空")
        if name not in self.tasks and len(self.tasks) >= MAX_TASKS:
            raise CKError(f"定时任务数量已达上限 {MAX_TASKS}")
        if not ctx.appid or not (ctx.group_id or ctx.channel_id or ctx.user_id):
            raise CKError("$定时添加$ 需在真实消息环境使用（以当前会话为发送目标）")
        self.tasks[name] = {
            "cron": cron, "command": command, "enabled": True,
            "appid": ctx.appid, "chat_type": ctx.chat_type,
            "group_id": ctx.group_id, "user_id": ctx.user_id,
            "guild_id": ctx.guild_id, "channel_id": ctx.channel_id,
            "creator": ctx.user_id, "created": int(time.time()),
        }
        self.save()

    def remove(self, name: str) -> None:
        if name not in self.tasks:
            raise CKError(f"定时任务不存在: {name}")
        del self.tasks[name]
        self.save()

    def toggle(self, name: str, enabled: bool) -> None:
        if name not in self.tasks:
            raise CKError(f"定时任务不存在: {name}")
        self.tasks[name]["enabled"] = enabled
        self.save()

    def list_json(self) -> str:
        items = [{"名字": name, "cron": t.get("cron", ""), "指令": t.get("command", ""),
                  "开启": bool(t.get("enabled", True)), "创建人": t.get("creator", "")}
                 for name, t in self.tasks.items()]
        return json.dumps(items, ensure_ascii=False)

    # ---- 调度 ----

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.ensure_future(self._loop())

    def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(60 - time.time() % 60)  # 对齐分钟边界
            now = datetime.now()
            stamp = now.strftime("%Y%m%d%H%M")
            if stamp == self._last_minute:
                continue
            self._last_minute = stamp
            for name, task in list(self.tasks.items()):
                if not task.get("enabled", True):
                    continue
                try:
                    if cron_match(task.get("cron", ""), now):
                        asyncio.ensure_future(self._runner(name, dict(task)))
                except CKError:
                    continue  # 表达式损坏的任务跳过
