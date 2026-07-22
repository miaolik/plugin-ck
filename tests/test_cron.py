# -*- coding: utf-8 -*-
"""ck_cron 定时任务：cron 解析/命中与任务管理。"""
from datetime import datetime

import pytest

from ck_engine import CKError, Ctx


def test_parse_cron_basic(cron_mod):
    minute, hour, dom, month, dow = cron_mod.parse_cron("0 8 * * *")
    assert minute == {0} and hour == {8}
    assert dom is None and month is None and dow is None


def test_parse_cron_step_range_list(cron_mod):
    minute, _, _, _, dow = cron_mod.parse_cron("*/15 0-6 1,15 * 1-5")
    assert minute == {0, 15, 30, 45}
    assert dow == {1, 2, 3, 4, 5}


@pytest.mark.parametrize("expr", ["0 8 * *", "60 * * * *", "* * 0 * *", "a * * * *", "* * * * 8-9"])
def test_parse_cron_invalid(cron_mod, expr):
    with pytest.raises(CKError):
        cron_mod.parse_cron(expr)


def test_cron_match_time(cron_mod):
    dt = datetime(2026, 7, 22, 8, 30)  # 周三
    assert cron_mod.cron_match("30 8 * * *", dt)
    assert cron_mod.cron_match("*/10 * * * *", dt)
    assert cron_mod.cron_match("30 8 22 7 *", dt)
    assert cron_mod.cron_match("30 8 * * 3", dt)
    assert not cron_mod.cron_match("31 8 * * *", dt)
    assert not cron_mod.cron_match("30 8 * * 0", dt)


def test_cron_match_dom_dow_or(cron_mod):
    dt = datetime(2026, 7, 22, 0, 0)  # 22日 周三
    assert cron_mod.cron_match("0 0 1 * 3", dt)   # 日不中、周中 → 标准 cron 取或
    assert cron_mod.cron_match("0 0 22 * 0", dt)  # 日中、周不中
    assert not cron_mod.cron_match("0 0 1 * 0", dt)


def _real_ctx():
    return Ctx(message="x", user_id="U1", group_id="G1", appid="APP", chat_type="group")


async def _noop_runner(name, task):
    pass


def test_cron_manager_add_remove_toggle(cron_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(cron_mod, "CRON_FILE", tmp_path / "定时任务.json")
    mgr = cron_mod.CronManager(_noop_runner)
    mgr.add("早报", "0 8 * * *", "早报推送", _real_ctx())
    assert mgr.tasks["早报"]["group_id"] == "G1"
    assert mgr.tasks["早报"]["appid"] == "APP"
    mgr.toggle("早报", False)
    assert mgr.tasks["早报"]["enabled"] is False
    assert "早报" in mgr.list_json()
    mgr.remove("早报")
    assert mgr.tasks == {}
    with pytest.raises(CKError):
        mgr.remove("不存在")


def test_cron_manager_add_validates(cron_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(cron_mod, "CRON_FILE", tmp_path / "定时任务.json")
    mgr = cron_mod.CronManager(_noop_runner)
    with pytest.raises(CKError):
        mgr.add("x", "bad cron here !", "指令", _real_ctx())
    sandbox = Ctx(message="x", user_id="U", group_id="G")  # 无 appid（沙盒）
    with pytest.raises(CKError):
        mgr.add("x", "0 8 * * *", "指令", sandbox)
