"""引擎执行流程：find_block / _skip_to / handle / 控制语句 / _call_func。"""

import pytest

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


async def test_call_func_recall_requires_support():
    with pytest.raises(CKError):
        await call("撤回")


async def test_call_func_unknown_raises():
    with pytest.raises(CKError):
        await call("彻底不存在 x")
