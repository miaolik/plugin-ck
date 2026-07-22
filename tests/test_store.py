"""基于文件的数据存储：store_* / settings_* / globals_* / rank_* / http_timeout。"""

import json

import pytest

import ck_engine
from ck_engine import (
    CKError,
    DEFAULT_HTTP_TIMEOUT,
    disabled_dicts,
    globals_load,
    globals_save,
    http_timeout,
    rank_read,
    rank_write,
    set_dict_enabled,
    settings_load,
    settings_save,
    store_delete,
    store_keys,
    store_read,
    store_write,
)

pytestmark = pytest.mark.usefixtures("data_dir")


def test_store_write_then_read():
    store_write("cfg.txt", "name", "alice")
    assert store_read("cfg.txt", "name", "def") == "alice"


def test_store_read_default_when_missing():
    assert store_read("none.txt", "k", "fallback") == "fallback"
    store_write("cfg.txt", "a", "1")
    assert store_read("cfg.txt", "missing", "d") == "d"


def test_store_write_updates_existing_key():
    store_write("cfg.txt", "k", "1")
    store_write("cfg.txt", "k", "2")
    store_write("cfg.txt", "other", "x")
    assert store_read("cfg.txt", "k", "") == "2"
    assert store_read("cfg.txt", "other", "") == "x"
    assert store_keys("cfg.txt") == ["k", "other"]


def test_store_keys_empty_for_missing():
    assert store_keys("none.txt") == []


def test_store_delete():
    store_write("cfg.txt", "k", "1")
    assert store_delete("cfg.txt") is True
    assert store_delete("cfg.txt") is False


def test_safe_rel_path_rejects_traversal():
    with pytest.raises(CKError):
        store_write("../evil.txt", "k", "v")


def test_settings_load_default_empty():
    assert settings_load() == {}


def test_settings_save_and_load_roundtrip():
    settings_save({"http_timeout": 42, "disabled_dicts": ["x"]})
    assert settings_load() == {"http_timeout": 42, "disabled_dicts": ["x"]}


def test_disabled_dicts_reads_from_settings():
    settings_save({"disabled_dicts": ["a", "b"]})
    assert disabled_dicts() == ["a", "b"]


def test_disabled_dicts_non_list_returns_empty():
    settings_save({"disabled_dicts": "notalist"})
    assert disabled_dicts() == []


def test_set_dict_enabled_toggle():
    set_dict_enabled("foo", False)
    assert "foo" in disabled_dicts()
    set_dict_enabled("bar", False)
    assert sorted(disabled_dicts()) == ["bar", "foo"]
    set_dict_enabled("foo", True)
    assert disabled_dicts() == ["bar"]


def test_http_timeout_default():
    assert http_timeout() == DEFAULT_HTTP_TIMEOUT


def test_http_timeout_custom():
    settings_save({"http_timeout": 60})
    assert http_timeout() == 60


@pytest.mark.parametrize("bad", [0, -5, "abc"])
def test_http_timeout_invalid_falls_back(bad):
    settings_save({"http_timeout": bad})
    assert http_timeout() == DEFAULT_HTTP_TIMEOUT


def test_globals_roundtrip_and_stringify():
    globals_save({"a": 1, "b": "two"})
    loaded = globals_load()
    assert loaded == {"a": "1", "b": "two"}


def test_globals_load_missing_empty():
    assert globals_load() == {}


def test_rank_write_sorts_desc_and_read():
    rank_write("rk.json", "score", "alice", "10")
    rank_write("rk.json", "score", "bob", "30")
    rank_write("rk.json", "score", "carol", "20")
    # 第 0 名（最高）应为 bob=30
    assert rank_read("rk.json", "score", "参数", "0") == "bob"
    assert rank_read("rk.json", "score", "值", "0") == "30"
    assert rank_read("rk.json", "score", "参数", "2") == "alice"


def test_rank_write_updates_member():
    rank_write("rk.json", "s", "a", "5")
    rank_write("rk.json", "s", "a", "50")
    assert rank_read("rk.json", "s", "值", "0") == "50"
    # 只保留一条 a 记录
    data = json.loads((ck_engine.DATA_DIR / "rk.json").read_text(encoding="utf-8"))
    assert len(data["s"]) == 1


def test_rank_write_rejects_non_number():
    with pytest.raises(CKError):
        rank_write("rk.json", "s", "a", "notnum")


def test_rank_read_missing_returns_empty():
    assert rank_read("none.json", "s", "值", "0") == ""
    rank_write("rk.json", "s", "a", "1")
    assert rank_read("rk.json", "s", "值", "9") == ""
