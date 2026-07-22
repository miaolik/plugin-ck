"""ck_web.py API 处理器（用假 request 直接调用，依赖框架，找不到时跳过）。"""

import json

import pytest

pytestmark = pytest.mark.usefixtures("web_dirs")


class FakeRequest:
    def __init__(self, query=None, body=None):
        self.query = query or {}
        self._body = body or {}

    async def json(self):
        return self._body


def body_of(resp):
    return json.loads(resp.text)


# ---- 词库名校验 ----

def test_dict_path_valid(ckweb_mod):
    p = ckweb_mod._dict_path("我的词库-1")
    assert p.name == "我的词库-1.txt"


@pytest.mark.parametrize("bad", ["", "a/b", "../etc", "a" * 65, "带 空格"])
def test_dict_path_invalid(ckweb_mod, bad):
    with pytest.raises(ValueError):
        ckweb_mod._dict_path(bad)


def test_err_response(ckweb_mod):
    resp = ckweb_mod._err("出错了", 404)
    assert resp.status == 404
    assert body_of(resp) == {"success": False, "message": "出错了"}


# ---- 词库文件管理 ----

async def test_dicts_list(ckweb_mod, web_dirs):
    (web_dirs["dicts"] / "a.txt").write_text("触发\n输出", encoding="utf-8")
    resp = await ckweb_mod.api_dicts(FakeRequest())
    data = body_of(resp)
    assert data["success"] is True
    assert [d["name"] for d in data["dicts"]] == ["a"]
    assert data["dicts"][0]["enabled"] is True


async def test_dict_get_and_404(ckweb_mod, web_dirs):
    (web_dirs["dicts"] / "a.txt").write_text("内容", encoding="utf-8")
    ok = body_of(await ckweb_mod.api_dict_get(FakeRequest(query={"name": "a"})))
    assert ok["content"] == "内容"
    missing = await ckweb_mod.api_dict_get(FakeRequest(query={"name": "ghost"}))
    assert missing.status == 404
    invalid = await ckweb_mod.api_dict_get(FakeRequest(query={"name": "../x"}))
    assert invalid.status == 400


async def test_dict_save_reloads_engine(ckweb_mod, web_dirs):
    resp = await ckweb_mod.api_dict_save(
        FakeRequest(body={"name": "新词库", "content": "你好\n世界"}))
    data = body_of(resp)
    assert data["success"] is True
    assert (web_dirs["dicts"] / "新词库.txt").read_text(encoding="utf-8") == "你好\n世界"
    assert data["blocks"] == 1


async def test_dict_rename(ckweb_mod, web_dirs):
    (web_dirs["dicts"] / "old.txt").write_text("x\ny", encoding="utf-8")
    resp = await ckweb_mod.api_dict_rename(
        FakeRequest(body={"name": "old", "new_name": "new"}))
    assert body_of(resp)["success"] is True
    assert not (web_dirs["dicts"] / "old.txt").exists()
    assert (web_dirs["dicts"] / "new.txt").exists()


async def test_dict_rename_conflict(ckweb_mod, web_dirs):
    (web_dirs["dicts"] / "a.txt").write_text("x", encoding="utf-8")
    (web_dirs["dicts"] / "b.txt").write_text("x", encoding="utf-8")
    resp = await ckweb_mod.api_dict_rename(
        FakeRequest(body={"name": "a", "new_name": "b"}))
    assert body_of(resp)["success"] is False


async def test_dict_delete(ckweb_mod, web_dirs):
    (web_dirs["dicts"] / "a.txt").write_text("x\ny", encoding="utf-8")
    resp = await ckweb_mod.api_dict_delete(FakeRequest(body={"name": "a"}))
    assert body_of(resp)["success"] is True
    assert not (web_dirs["dicts"] / "a.txt").exists()


async def test_dict_toggle_disables(ckweb_mod, web_dirs):
    (web_dirs["dicts"] / "a.txt").write_text("触发\n输出", encoding="utf-8")
    resp = await ckweb_mod.api_dict_toggle(
        FakeRequest(body={"name": "a", "enabled": False}))
    assert body_of(resp)["enabled"] is False
    listed = body_of(await ckweb_mod.api_dicts(FakeRequest()))
    assert listed["dicts"][0]["enabled"] is False


# ---- 沙盒测试 ----

async def test_api_test_matches_dict(ckweb_mod, web_dirs):
    await ckweb_mod.api_dict_save(FakeRequest(body={"name": "t", "content": "你好\n世界"}))
    resp = await ckweb_mod.api_test(FakeRequest(body={"message": "你好"}))
    data = body_of(resp)
    assert data["matched"] is True
    assert data["outputs"] == [{"type": "text", "content": "世界"}]


async def test_api_test_empty_message_rejected(ckweb_mod):
    resp = await ckweb_mod.api_test(FakeRequest(body={"message": ""}))
    assert resp.status == 400


# ---- 设置 / 全局变量 ----

async def test_settings_roundtrip(ckweb_mod):
    resp = await ckweb_mod.api_settings_save(FakeRequest(body={"http_timeout": 66}))
    assert body_of(resp)["settings"]["http_timeout"] == 66
    got = body_of(await ckweb_mod.api_settings_get(FakeRequest()))
    assert got["settings"]["http_timeout"] == 66


@pytest.mark.parametrize("bad", [0, -1, 3601, "abc"])
async def test_settings_save_rejects_invalid(ckweb_mod, bad):
    resp = await ckweb_mod.api_settings_save(FakeRequest(body={"http_timeout": bad}))
    assert resp.status == 400


async def test_globals_roundtrip(ckweb_mod):
    resp = await ckweb_mod.api_globals_save(
        FakeRequest(body={"globals": {"名字": "值", "n": 1}}))
    assert body_of(resp)["success"] is True
    got = body_of(await ckweb_mod.api_globals_get(FakeRequest()))
    assert got["globals"] == {"名字": "值", "n": "1"}


async def test_globals_save_rejects_non_dict(ckweb_mod):
    resp = await ckweb_mod.api_globals_save(FakeRequest(body={"globals": [1, 2]}))
    assert resp.status == 400


# ---- data/ 文件浏览 ----

async def test_data_list_and_content(ckweb_mod, web_dirs):
    (web_dirs["data"] / "f.txt").write_text("内容", encoding="utf-8")
    listed = body_of(await ckweb_mod.api_data_list(FakeRequest()))
    paths = [f["path"] for f in listed["files"]]
    assert "f.txt" in paths
    got = body_of(await ckweb_mod.api_data_content(FakeRequest(query={"path": "f.txt"})))
    assert got["content"] == "内容"


async def test_data_content_rejects_traversal(ckweb_mod):
    resp = await ckweb_mod.api_data_content(FakeRequest(query={"path": "../../etc/passwd"}))
    assert resp.status == 400


async def test_data_delete(ckweb_mod, web_dirs):
    f = web_dirs["data"] / "del.txt"
    f.write_text("x", encoding="utf-8")
    resp = await ckweb_mod.api_data_delete(FakeRequest(body={"path": "del.txt"}))
    assert body_of(resp)["success"] is True
    assert not f.exists()


async def test_data_delete_missing_404(ckweb_mod):
    resp = await ckweb_mod.api_data_delete(FakeRequest(body={"path": "ghost.txt"}))
    assert resp.status == 404
