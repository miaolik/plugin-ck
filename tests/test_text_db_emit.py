"""_text_func / _db_func / _emit 行为。"""

import json

import pytest

from ck_engine import CKError, Ctx, CKEngine


@pytest.fixture
def eng():
    return CKEngine()


# ---- _text_func ----

def test_text_func_replace(eng):
    assert eng._text_func("替换", "@ abcXdef@X@-") == "abc-def"


def test_text_func_middle(eng):
    assert eng._text_func("取中间", "@ [a]hello[b]@[a]@[b]") == "hello"


def test_text_func_middle_missing_returns_empty(eng):
    assert eng._text_func("取中间", "@ abc@[@]") == ""


def test_text_func_split(eng):
    assert eng._text_func("分割", "@ a,b,c@,") == "[a,b,c]"


def test_text_func_regex_match(eng):
    assert eng._text_func("正则", r"@ hello123@(\d+)") == "hello123"
    assert eng._text_func("正则", r"@ hello@(\d+)") == ""


def test_text_func_regex_invalid_raises(eng):
    with pytest.raises(CKError):
        eng._text_func("正则", "@ x@([)")


def test_text_func_array_add(eng):
    assert eng._text_func("数组处理", "add @ [1,2]@3") == "[1,2,3]"


def test_text_func_array_del(eng):
    assert eng._text_func("数组处理", "del @ [1,2,3]@2") == "[1,3]"


def test_text_func_bad_args_raise(eng):
    with pytest.raises(CKError):
        eng._text_func("替换", "@ onlytwo@parts")
    with pytest.raises(CKError):
        eng._text_func("替换", "noseparator")


# ---- _db_func ----

@pytest.mark.usefixtures("data_dir")
def test_db_declare_execute_query(eng):
    r = json.loads(eng._db_func("声明 mydb game.db"))
    assert r["status"] == 0
    eng._db_func("执行SQL mydb CREATE TABLE t(id INTEGER, name TEXT)")
    eng._db_func("执行SQL mydb INSERT INTO t VALUES (1, 'alice')")
    q = json.loads(eng._db_func("查询SQL mydb SELECT * FROM t"))
    assert q["status"] == 1
    assert q["data"] == [{"id": 1, "name": "alice"}]


@pytest.mark.usefixtures("data_dir")
def test_db_undeclared_returns_error(eng):
    r = json.loads(eng._db_func("查询SQL ghost SELECT 1"))
    assert r["status"] == -1
    assert "未声明" in r["errorMsg"]


@pytest.mark.usefixtures("data_dir")
def test_db_bad_sql_returns_error(eng):
    eng._db_func("声明 d a.db")
    r = json.loads(eng._db_func("查询SQL d NOT VALID SQL"))
    assert r["status"] == -1
    assert r["errorMsg"]


def test_db_missing_args_raises(eng):
    with pytest.raises(CKError):
        eng._db_func("声明")


# ---- _emit ----

def test_emit_plain_text(eng):
    ctx = Ctx()
    eng._emit("hello world", ctx)
    assert ctx.outputs == [{"type": "text", "content": "hello world"}]


def test_emit_newline_escape(eng):
    ctx = Ctx()
    eng._emit("a\\nb", ctx)
    assert ctx.outputs[0]["content"] == "a\nb"


def test_emit_image_between_text(eng):
    ctx = Ctx()
    eng._emit("before±img=http://x±after", ctx)
    assert [(o["type"], o.get("content")) for o in ctx.outputs] == [
        ("text", "before"), ("image", "http://x"), ("text", "after"),
    ]


def test_emit_at_specific_and_all(eng):
    ctx = Ctx()
    eng._emit("±at=123±", ctx)
    assert ctx.outputs[0]["content"] == "@123 "
    ctx2 = Ctx()
    eng._emit("±at=0±", ctx2)
    assert ctx2.outputs[0]["content"] == "@全体成员 "


def test_emit_mode_flags(eng):
    ctx = Ctx()
    eng._emit("±md±±无后缀±±文本±", ctx)
    assert ctx.md_mode is True
    assert ctx.skip_suffix is True
    assert ctx.text_mode is True


def test_emit_auto_delete(eng):
    ctx = Ctx()
    eng._emit("±自动撤回=5±", ctx)
    assert ctx.auto_delete == 5


def test_emit_auto_delete_invalid_records_error(eng):
    ctx = Ctx()
    eng._emit("±自动撤回=abc±", ctx)
    assert ctx.auto_delete == 0
    assert ctx.errors


def test_emit_buttons_and_ark_and_quote(eng):
    ctx = Ctx()
    eng._emit("±btn=文本;值±±ark=37|a±±引用±", ctx)
    types = [o["type"] for o in ctx.outputs]
    assert "buttons" in types
    assert "ark" in types
    assert "quote" in types
