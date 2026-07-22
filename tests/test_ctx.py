"""Ctx 输出收集与变量替换（_sub_vars / _var_value / _format_time / _sub_array_access）。"""

import re

import pytest

from ck_engine import Ctx, CKEngine


def test_ctx_out_text_merges_consecutive_text():
    ctx = Ctx()
    ctx.out_text("a")
    ctx.out_text("b")
    assert ctx.outputs == [{"type": "text", "content": "ab"}]


def test_ctx_out_non_text_not_merged():
    ctx = Ctx()
    ctx.out_text("a")
    ctx.out("image", "url")
    ctx.out_text("b")
    assert [o["type"] for o in ctx.outputs] == ["text", "image", "text"]


@pytest.fixture
def eng():
    return CKEngine()


def test_sub_vars_system_variables(eng):
    ctx = Ctx(user_id="u1", username="nick", group_id="g1")
    assert eng._sub_vars("%ID%-%昵称%-%群ID%", ctx) == "u1-nick-g1"
    assert eng._sub_vars("%UserId% %UserName%", ctx) == "u1 nick"


def test_sub_vars_local_var_takes_priority(eng):
    ctx = Ctx(user_id="u1")
    ctx.vars["ID"] = "override"
    assert eng._sub_vars("%ID%", ctx) == "override"


def test_sub_vars_unknown_var_left_intact(eng):
    ctx = Ctx()
    assert eng._sub_vars("%不存在%", ctx) == "%不存在%"


def test_var_value_counts_and_lists(eng):
    ctx = Ctx(ats=["a", "b"], images=["i1"])
    assert eng._var_value("AT数量", ctx) == "2"
    assert eng._var_value("图片数量", ctx) == "1"
    assert eng._var_value("AT0", ctx) == "a"
    assert eng._var_value("AT9", ctx) == ""
    assert eng._var_value("IMG0", ctx) == "i1"


def test_var_value_message_params(eng):
    ctx = Ctx(message="cmd foo bar")
    assert eng._var_value("参数0", ctx) == "cmd"
    assert eng._var_value("参数2", ctx) == "bar"
    assert eng._var_value("参数-1", ctx) == "cmd foo bar"
    assert eng._var_value("参数9", ctx) == ""


def test_var_value_capture_groups(eng):
    ctx = Ctx(message="数字42")
    ctx.match = re.fullmatch(r"数字(\d+)", ctx.message)
    assert eng._var_value("括号1", ctx) == "42"


def test_var_value_guild_falls_back_to_group(eng):
    ctx = Ctx(group_id="g1")
    assert eng._var_value("频道号", ctx) == "g1"
    ctx2 = Ctx(group_id="g1", guild_id="guild1")
    assert eng._var_value("频道号", ctx2) == "guild1"


def test_var_value_random_number_in_range(eng):
    ctx = Ctx()
    for _ in range(20):
        val = int(eng._var_value("随机数1-3", ctx))
        assert 1 <= val <= 3


def test_var_value_returns_none_for_unknown(eng):
    assert eng._var_value("彻底不存在的变量名", Ctx()) is None


def test_var_value_identity_fields(eng):
    ctx = Ctx(avatar="http://a", role="admin", chat_type="group",
              robot_qq="10001", robot_name="小助手", message_id="m1",
              appid="app1", channel_id="c1", raw_json='{"k":1}')
    assert eng._var_value("头像", ctx) == "http://a"
    assert eng._var_value("身份", ctx) == "admin"
    assert eng._var_value("场景", ctx) == "group"
    assert eng._var_value("机器人QQ", ctx) == "10001"
    assert eng._var_value("robotName", ctx) == "小助手"
    assert eng._var_value("消息ID", ctx) == "m1"
    assert eng._var_value("robot", ctx) == "app1"
    assert eng._var_value("子频道号", ctx) == "c1"
    assert eng._var_value("JSON", ctx) == '{"k":1}'


def test_var_value_time_stamps(eng):
    ctx = Ctx()
    assert eng._var_value("秒戳", ctx).isdigit()
    assert len(eng._var_value("Time", ctx)) >= 12  # 毫秒戳


def test_var_value_extras(eng):
    ctx = Ctx(extras={"自定义": "v"})
    assert eng._var_value("自定义", ctx) == "v"


def test_var_value_random_letter(eng):
    ctx = Ctx()
    val = eng._var_value("随机数a-c", ctx)
    assert val in ("a", "b", "c")


def test_var_value_global(eng, data_dir):
    import ck_engine
    ck_engine.globals_save({"公告": "hi"})
    assert eng._var_value("全局.公告", Ctx()) == "hi"
    assert eng._var_value("全局.没有", Ctx()) == ""


def test_format_time_tokens():
    out = CKEngine._format_time("yyyy-MM-dd E")
    assert re.match(r"\d{4}-\d{2}-\d{2} 星期[一二三四五六日]", out)


def test_sub_array_access_index_array(eng):
    assert eng._sub_array_access("@[1,2,3] [1]") == "2"


def test_sub_array_access_json(eng):
    assert eng._sub_array_access('@{"a": 5} [a]') == "5"


def test_sub_array_access_no_at_unchanged(eng):
    assert eng._sub_array_access("no at here") == "no at here"
