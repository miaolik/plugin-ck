"""引擎执行流程：find_block / _skip_to / handle / 控制语句 / _call_func。"""

import pytest
from pathlib import Path

from ck_engine import CKError, Ctx, CKEngine, parse_dict_text


def make_engine(dict_text):
    eng = CKEngine()
    blocks, init, _ = parse_dict_text(dict_text, "t.txt")
    eng.blocks = blocks
    eng.init_lines = init
    return eng


def run(eng, message, **kw):
    """触发 message，返回合并后的纯文本输出。"""
    import asyncio
    ctx = Ctx(message=message, **kw)
    asyncio.run(eng.handle(ctx))
    return "".join(o["content"] for o in ctx.outputs if o["type"] == "text"), ctx


def make_farm_engine():
    text = (Path(__file__).resolve().parent.parent / "dicts" / "农场游戏.txt").read_text(encoding="utf-8")
    return make_engine(text)


def make_join_review_engine():
    text = (Path(__file__).resolve().parent.parent / "dicts" / "入群审核.txt").read_text(encoding="utf-8")
    return make_engine(text)


# ---- find_block / _skip_to ----

def test_find_block_matches_and_respects_internal():
    eng = make_engine("公开\nx\n\n[内部]私有\ny")
    assert eng.find_block("公开") is not None
    assert eng.find_block("公开", internal=True) is None
    assert eng.find_block("私有", internal=True) is not None
    assert eng.find_block("私有") is None


def test_find_block_none_when_no_match():
    eng = make_engine("abc\nx")
    assert eng.find_block("zzz") is None


def test_skip_to_matches_close_keyword():
    eng = CKEngine()
    lines = ["如果:1==1", "a", "如果尾", "b"]
    assert eng._skip_to(lines, 0, "如果:", "如果尾") == 2


def test_skip_to_handles_nesting():
    eng = CKEngine()
    lines = ["如果:1==1", "如果:2==2", "x", "如果尾", "如果尾", "tail"]
    assert eng._skip_to(lines, 0, "如果:", "如果尾") == 4


# ---- handle + 控制语句 ----

def test_handle_returns_false_when_no_block():
    import asyncio
    eng = make_engine("abc\nx")
    ctx = Ctx(message="nomatch")
    assert asyncio.run(eng.handle(ctx)) is False


def test_simple_echo_with_param():
    eng = make_engine("echo.*\n你好%参数1%")
    out, _ = run(eng, "echo world")
    assert out == "你好world"


def test_if_true_branch():
    eng = make_engine("测试\n如果:1==1\nyes\n如果尾\nend")
    out, _ = run(eng, "测试")
    assert out == "yesend"


def test_if_false_branch_skipped():
    eng = make_engine("测试\n如果:1==2\nno\n如果尾\nend")
    out, _ = run(eng, "测试")
    assert out == "end"


def test_return_stops_execution():
    eng = make_engine("测试\na\n返回\nb")
    out, _ = run(eng, "测试")
    assert out == "a"


def test_local_var_assignment_and_arith():
    eng = make_engine("测试\nx:5\ny:[%x%+3]\n%y%")
    out, _ = run(eng, "测试")
    assert out == "8"


def test_loop_accumulates():
    eng = make_engine("测试\ni:1\ns:\n循环:i<=3\ns:%s%%i%\ni:[%i%+1]\n结束\n%s%")
    out, _ = run(eng, "测试")
    assert out == "123"


def test_loop_break():
    eng = make_engine("测试\ni:1\n循环:i<=10\n%i%\n如果:%i%==2\n跳出\n如果尾\ni:[%i%+1]\n结束")
    out, _ = run(eng, "测试")
    assert out == "12"


def test_switch_case_match():
    eng = make_engine("菜单.*\n分支:%参数1%\n情况:a\nA选项\n情况:b\nB选项\ndefault:\n未知\n分支尾")
    assert run(eng, "菜单 a")[0] == "A选项"
    assert run(eng, "菜单 b")[0] == "B选项"
    assert run(eng, "菜单 z")[0] == "未知"


def test_foreach_over_csv():
    eng = make_engine("遍历.*\n循环遍历:%参数1% x\n-%x%\n结束")
    out, _ = run(eng, "遍历 a,b,c")
    assert out == "-a-b-c"


def test_foreach_with_index_over_json_array():
    # 词库行首尾空白会被解析器 strip，故连接处无空格
    eng = make_engine('遍历\n循环遍历:[10,20] v i\n[%i%=%v%]\n结束')
    out, _ = run(eng, "遍历")
    assert out == "[0=10][1=20]"


def test_callback_merges_output():
    # $回调$ 立即把子块输出合并进 outputs，随后本行文本再追加
    eng = make_engine("[内部]sub\n子结果\n\n主\n前 $回调 sub$ 后")
    out, _ = run(eng, "主")
    assert out == "子结果前  后"


def test_callback_vars_flow_both_ways():
    # 子块可读调用方变量，子块内赋值也回传给调用方（变量延续）
    eng = make_engine("[内部]sub\ny:[%x%+1]\n\n主\nx:5\n$回调 sub$%y%")
    out, _ = run(eng, "主")
    assert out == "6"


def test_callback_break_exits_caller_loop():
    # 回调内 如果 判断不通过时 跳出，应结束调用方所在循环
    eng = make_engine(
        "[内部]检查\n如果:%i%==3\n跳出\n如果尾\n\n"
        "主\ni:1\n循环:i<=10\n$回调 检查$%i%\ni:[%i%+1]\n结束\nend")
    out, _ = run(eng, "主")
    assert out == "12end"


def test_callback_continue_skips_caller_iteration():
    eng = make_engine(
        "[内部]检查\n如果:%i%==2\n继续\n如果尾\n\n"
        "主\ni:0\n循环:i<=3\ni:[%i%+1]\n$回调 检查$%i%\n结束")
    out, _ = run(eng, "主")
    assert out == "134"


def test_loop_nested_in_foreach():
    # 循环遍历 内嵌 循环:，外层 结束 不应被内层 结束 提前截断
    eng = make_engine("遍历\n循环遍历:a,b x\nj:1\n循环:j<=2\n%x%%j%\nj:[%j%+1]\n结束\n结束\nend")
    out, _ = run(eng, "遍历")
    assert out == "a1a2b1b2end"


def test_switch_nested_in_switch():
    eng = make_engine(
        "测\n分支:a\n情况:a\n外A\n分支:b\n情况:a\n内A\n情况:b\n内B\n分支尾\n情况:b\n外B\n分支尾")
    out, _ = run(eng, "测")
    assert out == "外A内B"


def test_farm_dictionary_supports_new_player_purchase_and_planting(data_dir):
    from ck_engine import store_write

    eng = make_farm_engine()
    base = {
        "user_id": "u1", "group_id": "g1", "chat_type": "group",
        "extras": {"会话ID": "g1"},
    }

    menu, menu_ctx = run(eng, "农场", **base)
    assert "田园农场" in menu
    assert menu_ctx.md_mode is True
    assert menu_ctx.errors == []

    locked, locked_ctx = run(eng, "购买种子 番茄", **base)
    assert "等级不足" in locked
    assert locked_ctx.errors == []

    bought, bought_ctx = run(eng, "购买种子 小麦", **base)
    assert "购买成功" in bought
    assert bought_ctx.errors == []

    planted, planted_ctx = run(eng, "种植 1 小麦", **base)
    assert "播种完成" in planted
    assert planted_ctx.errors == []

    growing, growing_ctx = run(eng, "收获 1", **base)
    assert "作物生长中" in growing
    assert growing_ctx.errors == []

    store_write("农场/g1/档", "u1_种植时间1", "0")
    harvested, harvested_ctx = run(eng, "收获 1", **base)
    assert "收获成功" in harvested
    assert harvested_ctx.errors == []

    sold, sold_ctx = run(eng, "出售 小麦", **base)
    assert "出售完成" in sold
    assert sold_ctx.errors == []

    store_write("农场/g1/档", "u1_等级", "3")
    store_write("农场/g1/档", "u1_金币", "100")
    unlocked, unlocked_ctx = run(eng, "解锁土地 2", **base)
    assert "土地解锁" in unlocked
    assert unlocked_ctx.errors == []


def test_group_mute_dictionary_uses_mentioned_member_id():
    text = (Path(__file__).resolve().parent.parent / "dicts" / "群管功能.txt").read_text(encoding="utf-8")
    eng = make_engine(text)
    calls = []

    async def mute(rest):
        calls.append(rest)
        return '{"success":true}'

    out, ctx = run(
        eng,
        "群禁言 @成员 30",
        group_id="g1",
        role="admin",
        ats=["member-openid"],
        actions={"群禁言": mute},
    )

    assert calls == ["member-openid 30"]
    assert "已禁言 member-openid" in out
    assert ctx.errors == []

    no_space_out, no_space_ctx = run(
        eng,
        "群禁言@成员 30",
        group_id="g1",
        role="admin",
        ats=["member-openid"],
        actions={"群禁言": mute},
    )

    assert calls == ["member-openid 30", "member-openid 30"]
    assert "已禁言 member-openid" in no_space_out
    assert no_space_ctx.errors == []

    stripped_out, stripped_ctx = run(
        eng,
        "群禁言 30",
        group_id="g1",
        role="admin",
        ats=["member-openid"],
        actions={"群禁言": mute},
    )

    assert calls == ["member-openid 30", "member-openid 30", "member-openid 30"]
    assert "已禁言 member-openid" in stripped_out
    assert stripped_ctx.errors == []

    batch_out, batch_ctx = run(
        eng,
        "群禁言 30",
        group_id="g1",
        role="admin",
        ats=["member-openid-1", "member-openid-2"],
        actions={"群禁言": mute},
    )

    assert calls[-1] == "member-openid-1,member-openid-2 30"
    assert "已禁言 member-openid-1,member-openid-2" in batch_out
    assert batch_ctx.errors == []

    unmute_calls = []

    async def unmute(rest):
        unmute_calls.append(rest)
        return '{"success":true}'

    unmuted_out, unmuted_ctx = run(
        eng,
        "解除群禁言",
        group_id="g1",
        role="admin",
        ats=["member-openid"],
        actions={"解除群禁言": unmute},
    )

    assert unmute_calls == ["member-openid"]
    assert "已解除 member-openid 的禁言" in unmuted_out
    assert unmuted_ctx.errors == []

    missing_target_out, missing_target_ctx = run(
        eng,
        "解除群禁言",
        group_id="g1",
        role="admin",
        actions={"解除群禁言": unmute},
    )

    assert "用法：解除群禁言" in missing_target_out
    assert missing_target_ctx.errors == []


def test_guild_management_functions_build_expected_api_requests():
    eng = CKEngine()
    calls = []

    async def api(method, path, payload):
        calls.append((method, path, payload))
        return True, {"ok": True}

    ctx = Ctx(guild_id="guild-1", channel_id="channel-1", actions={"官方API": api})

    async def run_guild_function():
        assert '"success": true' in await eng._guild_func("频道禁言", "user-1 60", ctx)
        assert '"success": true' in await eng._guild_func("频道全员禁言", "0", ctx)
        assert '"success": true' in await eng._guild_func("频道撤回", "message-1", ctx)
        assert '"success": true' in await eng._guild_func("频道拉黑", "user-2", ctx)
        assert '"success": true' in await eng._guild_func("身份组加", "user-3 role-1", ctx)

    import asyncio
    asyncio.run(run_guild_function())

    assert calls == [
        ("PATCH", "/guilds/guild-1/members/user-1/mute", {"mute_seconds": "60"}),
        ("PATCH", "/guilds/guild-1/mute", {"mute_seconds": "0"}),
        ("DELETE", "/channels/channel-1/messages/message-1?hidetip=true", None),
        ("DELETE", "/guilds/guild-1/members/user-2", {"add_blacklist": True}),
        ("PUT", "/guilds/guild-1/members/user-3/roles/role-1", {"channel": {"id": "channel-1"}}),
    ]


def test_channel_management_text_menu_lists_one_command_per_line():
    text = (Path(__file__).resolve().parent.parent / "dicts" / "频道管理.txt").read_text(encoding="utf-8")
    eng = make_engine(text)
    out, ctx = run(eng, "频道管理普通", guild_id="guild-1", channel_id="channel-1")

    assert "禁言 用户ID 秒数：禁言频道成员" in out
    assert "身份组列表：查看频道全部身份组" in out
    assert "删帖 帖子ID：删除指定帖子" in out
    assert ctx.errors == []


def test_channel_non_at_dictionary_requires_owner_and_updates_setting():
    text = (Path(__file__).resolve().parent.parent / "dicts" / "频道管理.txt").read_text(encoding="utf-8")
    eng = make_engine(text)
    calls = []

    async def set_non_at(value):
        calls.append(value)
        return value

    denied_out, denied_ctx = run(
        eng, "频道免艾特开启", guild_id="guild-1", channel_id="channel-1", role="admin",
        actions={"频道免艾特": set_non_at},
    )
    assert "仅频道主可修改" in denied_out
    assert denied_ctx.errors == []

    enabled_out, enabled_ctx = run(
        eng, "频道免艾特开启", guild_id="guild-1", channel_id="channel-1", role="owner",
        actions={"频道免艾特": set_non_at},
    )
    assert calls == ["1"]
    assert "已开启频道免艾特回复" in enabled_out
    assert enabled_ctx.errors == []


def test_channel_management_dictionary_commands_cover_permissions_and_actions():
    text = (Path(__file__).resolve().parent.parent / "dicts" / "频道管理.txt").read_text(encoding="utf-8")
    eng = make_engine(text)
    calls = []

    async def api(method, path, payload):
        calls.append((method, path, payload))
        if path.endswith("/roles") and method == "GET":
            return True, {"roles": [{"id": "role-1", "name": "管理员", "number": 2}]}
        return True, {"ok": True}

    base = {
        "guild_id": "guild-1", "channel_id": "channel-1", "role": "owner",
        "actions": {"官方API": api},
    }

    muted_out, muted_ctx = run(eng, "禁言 user-1 60", **base)
    assert "已禁言 user-1（60 秒）" in muted_out
    assert muted_ctx.errors == []

    mentioned_out, mentioned_ctx = run(
        eng, "禁言 60", guild_id="guild-1", channel_id="channel-1", role="owner",
        ats=["user-mentioned"], actions={"官方API": api},
    )
    assert "已禁言 user-mentioned（60 秒）" in mentioned_out
    assert mentioned_ctx.errors == []

    roles_out, roles_ctx = run(eng, "身份组列表", **base)
    assert "管理员" in roles_out
    assert roles_ctx.errors == []

    posted_out, posted_ctx = run(eng, "发帖 标题 正文 内容", **base)
    assert "已发帖「标题」" in posted_out
    assert posted_ctx.errors == []

    denied_out, denied_ctx = run(
        eng, "拉黑 user-2", guild_id="guild-1", channel_id="channel-1", role="admin",
        actions={"官方API": api},
    )
    assert "仅频道主可拉黑" in denied_out
    assert denied_ctx.errors == []

    assert calls == [
        ("PATCH", "/guilds/guild-1/members/user-1/mute", {"mute_seconds": "60"}),
        ("PATCH", "/guilds/guild-1/members/user-mentioned/mute", {"mute_seconds": "60"}),
        ("GET", "/guilds/guild-1/roles", None),
        ("PUT", "/channels/channel-1/threads", {"title": "标题", "content": "正文 内容", "format": 1}),
    ]


# ---- _call_func 纯函数 ----

async def call(func_str, **kw):
    eng = CKEngine()
    return await eng._call_func(func_str, Ctx(**kw), 0)


async def test_call_func_string_length():
    assert await call("字符串长 hello") == "5"


async def test_call_func_contains():
    assert await call("字符包含 hello ll") == "true"
    assert await call("字符包含 hello zz") == "false"


async def test_call_func_calc():
    assert await call("计算 1+2*3") == "7"
    with pytest.raises(CKError):
        await call("计算 notmath")


async def test_call_func_is_number():
    assert await call("是否为数字 12.5") == "true"
    assert await call("是否为数字 abc") == "false"


async def test_call_func_md_image():
    assert await call("MD图片 http://a/1.png 120 60") == "![img #120px #60px](http://a/1.png)"
    assert await call("MD图片 http://a/1.png") == "![img](http://a/1.png)"
    with pytest.raises(CKError):
        await call("MD图片 ")


async def test_call_func_md_code():
    assert await call("MD代码 print(1)") == "```\nprint(1)\n```"
    assert await call("MD代码 语言=python a\\nb") == "```python\na\nb\n```"


async def test_call_func_md_table():
    out = await call("MD表格 @ 名次|昵称@1|甲@2|乙")
    assert out == "|名次|昵称|\n|---|---|\n|1|甲|\n|2|乙|"
    with pytest.raises(CKError):
        await call("MD表格 ")


def test_image_size_from_bytes():
    from ck_engine import image_size_from_bytes
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
           + (300).to_bytes(4, "big") + (200).to_bytes(4, "big") + b"\x00" * 10)
    assert image_size_from_bytes(png) == (300, 200)
    gif = b"GIF89a" + (64).to_bytes(2, "little") + (32).to_bytes(2, "little") + b"\x00" * 20
    assert image_size_from_bytes(gif) == (64, 32)
    jpg = (b"\xff\xd8\xff\xc0" + (17).to_bytes(2, "big") + b"\x08"
           + (240).to_bytes(2, "big") + (320).to_bytes(2, "big") + b"\x00" * 20)
    assert image_size_from_bytes(jpg) == (320, 240)
    assert image_size_from_bytes(b"not an image at all, definitely") is None


async def test_call_func_cron_dispatch():
    class FakeMgr:
        def __init__(self):
            self.calls = []

        def add(self, name, cron, command, ctx):
            self.calls.append(("add", name, cron, command))

        def remove(self, name):
            self.calls.append(("remove", name))

        def toggle(self, name, enabled):
            self.calls.append(("toggle", name, enabled))

        def list_json(self):
            return "[]"

    eng = CKEngine()
    eng.cron_manager = FakeMgr()
    ctx = Ctx(message="x", appid="APP", group_id="G")

    assert await eng._call_func("定时列表", ctx, 0) == "[]"
    await eng._call_func("定时添加 早报 0 8 * * * 早报推送", ctx, 0)
    assert eng.cron_manager.calls[-1] == ("add", "早报", "0 8 * * *", "早报推送")
    await eng._call_func("定时开关 早报 0", ctx, 0)
    assert eng.cron_manager.calls[-1] == ("toggle", "早报", False)
    await eng._call_func("定时删除 早报", ctx, 0)
    assert eng.cron_manager.calls[-1] == ("remove", "早报")
    with pytest.raises(CKError):
        await eng._call_func("定时添加 名字 0 8 * * *", ctx, 0)  # 缺指令
    eng.cron_manager = None
    with pytest.raises(CKError):
        await eng._call_func("定时列表", ctx, 0)


@pytest.mark.asyncio
async def test_call_func_group_management_actions():
    calls = []

    async def mute_status(rest):
        calls.append(("status", rest))
        return '{"global_rule":{"mode":"none"},"members":[]}'

    async def mute(rest):
        calls.append(("mute", rest))
        return '{"success":true}'

    eng = CKEngine()
    ctx = Ctx(actions={"群禁言状态": mute_status, "群禁言": mute})
    assert await eng._call_func("群禁言状态", ctx, 0) == '{"global_rule":{"mode":"none"},"members":[]}'
    assert await eng._call_func("群禁言 u1 30", ctx, 0) == '{"success":true}'
    assert calls == [("status", ""), ("mute", "u1 30")]


@pytest.mark.asyncio
async def test_call_func_group_management_missing_action():
    with pytest.raises(CKError, match="当前环境不支持"):
        await CKEngine()._call_func("入群申请列表", Ctx(), 0)


async def test_call_func_url_encode_decode():
    assert await call("URLEncoder a b") == "a%20b"
    assert await call("URLDecoder a%20b") == "a b"


async def test_call_func_array_length():
    assert await call("数组长 [1,2,3]") == "3"
    assert await call("数组长 a,b") == "2"
    assert await call("数组长 ") == "0"


async def test_call_func_random_number_range():
    for _ in range(20):
        assert 1 <= int(await call("随机数 1 3")) <= 3


@pytest.mark.usefixtures("data_dir")
async def test_call_func_globals_roundtrip():
    eng = CKEngine()
    ctx = Ctx()
    await eng._call_func("全局写 名字 阿伟", ctx, 0)
    assert await eng._call_func("全局读 名字 默认", ctx, 0) == "阿伟"
    assert await eng._call_func("全局读 不存在 默认值", ctx, 0) == "默认值"


@pytest.mark.usefixtures("data_dir")
async def test_call_func_store_roundtrip():
    eng = CKEngine()
    ctx = Ctx()
    await eng._call_func("写 cfg.txt k v1", ctx, 0)
    assert await eng._call_func("读 cfg.txt k def", ctx, 0) == "v1"


async def test_call_func_random_letters():
    val = await call("随机字母 4 1")
    assert len(val) == 4 and val.isalpha() and val.isupper()
    lower = await call("随机字母 3 0")
    assert lower.islower()


async def test_call_func_random_alnum_and_hanzi():
    an = await call("随机英文数字 6")
    assert len(an) == 6 and an.isalnum()
    hz = await call("随机汉字 2 0")
    assert len(hz) == 2 and all("\u4e00" <= c <= "\u9fa5" for c in hz)


async def test_call_func_delete_and_keys(tmp_path, monkeypatch):
    import ck_engine
    monkeypatch.setattr(ck_engine, "DATA_DIR", tmp_path)
    eng = CKEngine()
    ctx = Ctx()
    await eng._call_func("写 f.txt a 1", ctx, 0)
    await eng._call_func("写 f.txt b 2", ctx, 0)
    assert await eng._call_func("读键列表 f.txt", ctx, 0) == '["a", "b"]'
    await eng._call_func("删除 f.txt", ctx, 0)
    assert await eng._call_func("读 f.txt a def", ctx, 0) == "def"


def test_goto_forward_skips_lines():
    eng = make_engine(
        "测\n"
        "A\n"
        "跳转:4\n"
        "B\n"
        "C\n"
    )
    text, _ = run(eng, "测")
    assert text == "AC"


def test_goto_backward_with_condition_loops():
    eng = make_engine(
        "测\n"
        "i:0\n"
        "i:[%i%+1]\n"
        "第%i%次\\n\n"
        "如果:%i%<3\n"
        "跳转:2\n"
        "如果尾\n"
        "完\n"
    )
    text, _ = run(eng, "测")
    assert text == "第1次\n第2次\n第3次\n完"


def test_goto_invalid_line_reports_error():
    eng = make_engine("测\nA\n跳转:99\nB")
    _, ctx = run(eng, "测")
    assert any("跳转 行号无效" in e for e in ctx.errors)


def test_goto_infinite_loop_guard():
    eng = make_engine("测\nA\n跳转:1")
    _, ctx = run(eng, "测")
    assert any("执行步数超限" in e for e in ctx.errors)


def test_md_combined_formats_single_message():
    """±md± 块内标题/多图/代码框/表格/引用合并成一个文本片段（一条 MD 消息），按钮独立挂载。"""
    eng = make_engine(
        "测试MD\n"
        "±md±# 标题\\n\n"
        "$MD图片 https://i.example.com/a.png 120 80$\\n\n"
        "$MD图片 https://i.example.com/b.png 200 100$\\n\n"
        '$MD代码 语言=python print("hi")$\\n\n'
        "$MD表格 @ 名次|昵称@1|甲$\\n\n"
        "> 引用 **加粗**\n"
        "±btn=按钮;>测试MD±"
    )
    text, ctx = run(eng, "测试MD")
    assert ctx.md_mode is True
    texts = [o for o in ctx.outputs if o["type"] == "text"]
    assert len(texts) == 1
    assert text == (
        "# 标题\n"
        "![img #120px #80px](https://i.example.com/a.png)\n"
        "![img #200px #100px](https://i.example.com/b.png)\n"
        '```python\nprint("hi")\n```\n'
        "|名次|昵称|\n|---|---|\n|1|甲|\n"
        "> 引用 **加粗**"
    )
    assert [o["type"] for o in ctx.outputs] == ["text", "buttons"]


@pytest.mark.asyncio
async def test_join_review_list_emits_admin_only_callbacks():
    eng = make_join_review_engine()

    async def list_requests(rest):
        assert rest == ""
        return '{"list":[{"member_openid":"u1","join_request_id":"r1","review_qa_list":[]}]}'

    ctx = Ctx(
        message="入群申请列表",
        role="admin",
        actions={"入群申请列表": list_requests},
    )
    assert await eng.handle(ctx) is True
    buttons = [output["content"] for output in ctx.outputs if output["type"] == "buttons"]
    assert buttons == ["✅ 通过;通过入群 u1 r1;管理员|⛔ 拒绝;拒绝入群 u1 r1;管理员"]


@pytest.mark.asyncio
async def test_join_review_reject_callback_uses_default_reason():
    eng = make_join_review_engine()
    calls = []

    async def review(rest):
        calls.append(rest)
        return '{"success":true}'

    ctx = Ctx(
        message="拒绝入群 u1 r1",
        role="owner",
        actions={"入群审核": review},
    )
    assert await eng.handle(ctx) is True
    assert calls == ["u1 decline r1 管理员拒绝"]
    assert "已拒绝 u1 的入群申请" in "".join(
        output["content"] for output in ctx.outputs if output["type"] == "text"
    )


@pytest.mark.asyncio
async def test_join_review_platform_authorized_callback_skips_missing_role():
    eng = make_join_review_engine()

    async def list_requests(rest):
        return '{"list":[]}'

    ctx = Ctx(
        message="入群申请列表",
        actions={"入群申请列表": list_requests},
        extras={"平台按钮授权": "1"},
    )
    assert await eng.handle(ctx) is True
    text = "".join(output["content"] for output in ctx.outputs if output["type"] == "text")
    assert text == "暂无待审核入群申请"


async def test_call_func_recall_requires_support():
    with pytest.raises(CKError):
        await call("撤回")


async def test_call_func_unknown_raises():
    with pytest.raises(CKError):
        await call("彻底不存在 x")
