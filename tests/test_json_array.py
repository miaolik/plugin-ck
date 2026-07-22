"""JSON / 索引数组取值辅助函数。"""

from ck_engine import (
    _index_array_to_text,
    _json_pick,
    _parse_array_or_json,
    _parse_index_array,
    array_to_text,
)


def test_json_pick_dict_and_list():
    data = {"data": [{"表头1": "v"}]}
    assert _json_pick(data, "[data[0].表头1]") == "v"
    assert _json_pick([10, 20, 30], "[1]") == 20
    assert _json_pick({"a": {"b": 1}}, "[a][b]") == 1


def test_json_pick_missing_returns_none():
    assert _json_pick({"a": 1}, "[b]") is None
    assert _json_pick([1, 2], "[9]") is None
    assert _json_pick("scalar", "[a]") is None


def test_parse_array_or_json_json_first():
    assert _parse_array_or_json('{"a": 1}') == {"a": 1}
    assert _parse_array_or_json('[1, 2, 3]') == [1, 2, 3]


def test_parse_array_or_json_index_array_fallback():
    # 不带引号 → 非合法 JSON，走索引数组解析
    assert _parse_array_or_json("[a,b,c]") == ["a", "b", "c"]


def test_parse_array_or_json_non_array_returns_none():
    assert _parse_array_or_json("plain text") is None


def test_parse_index_array_nested():
    assert _parse_index_array("[1,2,[a,b]]") == ["1", "2", ["a", "b"]]


def test_parse_index_array_chinese_comma():
    assert _parse_index_array("[甲，乙，丙]") == ["甲", "乙", "丙"]


def test_parse_index_array_scalar_wrapped():
    assert _parse_index_array("noarray") == ["noarray"]


def test_array_to_text_variants():
    assert array_to_text(None) == ""
    assert array_to_text(True) == "true"
    assert array_to_text(False) == "false"
    assert array_to_text(3.0) == "3"
    assert array_to_text(3.5) == "3.5"
    assert array_to_text("x") == "x"
    assert array_to_text([1, 2]) == "[1, 2]"
    assert array_to_text({"a": 1}) == '{"a": 1}'


def test_index_array_to_text_roundtrip():
    arr = ["1", "2", ["a", "b"]]
    assert _index_array_to_text(arr) == "[1,2,[a,b]]"
