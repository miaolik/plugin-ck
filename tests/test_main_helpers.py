"""main.py 事件解析 / 成员缓存 / 按钮解析 / 发送编排（依赖 ElainaBot_v2 框架，找不到时跳过）。"""

import json

import pytest


class FakeEvent:
    """只带测试需要的属性；未设置的属性走 getattr 默认值。"""

    def __init__(self, **kw):
        self.content = kw.pop("content", "")
        self.user_id = kw.pop("user_id", "")
        self.username = kw.pop("username", "")
        self.group_id = kw.pop("group_id", "")
        self.guild_id = kw.pop("guild_id", "")
        self.channel_id = kw.pop("channel_id", "")
        self.message_id = kw.pop("message_id", "")
        self.appid = kw.pop("appid", "")
        self.image_url = kw.pop("image_url", "")
        self.attachments = kw.pop("attachments", [])
        self.mentions = kw.pop("mentions", [])
        self.raw = kw.pop("raw", {})
        for k, v in kw.items():
            setattr(self, k, v)
        self.replies = []

    async def reply(self, content, **kwargs):
        self.replies.append(("reply", content, kwargs))

    async def reply_ark(self, ark_type, args):
        self.replies.append(("ark", ark_type, args))


# ---- 事件字段解析 ----

def test_clean_message_strips_url_tokens(main_mod):
    ev = FakeEvent(content="签到 <https://img.example/a.jpg> 结尾")
    assert main_mod._clean_message(ev) == "签到 结尾"  # 占位符连同其前导空白一起去除
    assert main_mod._clean_message(FakeEvent(content=None)) == ""


def test_event_images_dedup_and_content_type(main_mod):
    ev = FakeEvent(
        image_url="http://a/1.jpg",
        attachments=[
            {"url": "http://a/1.jpg", "content_type": "image/jpeg"},  # 重复
            {"url": "http://a/2.png", "content_type": "image/png"},
            {"url": "http://a/v.mp4", "content_type": "video/mp4"},   # 非图片
            "notadict",
        ],
    )
    assert main_mod._event_images(ev) == ["http://a/1.jpg", "http://a/2.png"]


def test_event_ats(main_mod):
    ev = FakeEvent(mentions=[{"id": 111}, {"noid": 1}, "x", {"id": "222"}])
    assert main_mod._event_ats(ev) == ["111", "222"]


def test_event_raw_json(main_mod):
    ev = FakeEvent(raw={"d": {"k": "v"}})
    assert json.loads(main_mod._event_raw_json(ev)) == {"d": {"k": "v"}}
    assert main_mod._event_raw_json(FakeEvent(raw=None)) == ""


def test_event_get_nested_path(main_mod):
    ev = FakeEvent(raw={"d": {"author": {"username": "u"}}})
    assert main_mod._event_get(ev, "d/author/username") == "u"
    assert main_mod._event_get(ev, "d/missing/x") == ""


@pytest.mark.parametrize("roles,expected", [
    (["4"], "owner"),
    (["2"], "admin"),
    (["5"], "admin"),
    (["1"], "member"),
    ([], ""),
])
def test_channel_role(main_mod, roles, expected):
    ev = FakeEvent(raw={"d": {"member": {"roles": roles}}})
    assert main_mod._channel_role(ev) == expected


def test_avatar_url_prefers_raw_then_qlogo(main_mod):
    ev = FakeEvent(raw={"d": {"author": {"avatar": "http://av"}}})
    assert main_mod._avatar_url(ev) == "http://av"
    ev2 = FakeEvent(appid="app1", user_id="u1")
    assert main_mod._avatar_url(ev2) == "https://q.qlogo.cn/qqapp/app1/u1/640"
    assert main_mod._avatar_url(FakeEvent()) == ""


def test_event_extras_mapping(main_mod):
    ev = FakeEvent(chat_id="c1", timestamp=123, event_type="GROUP_MESSAGE_CREATE",
                   message_reference_id="ref1", is_at_self=True,
                   message_scene={"source": "src"})
    extras = main_mod._event_extras(ev, bot_role="admin")
    assert extras["会话ID"] == "c1"
    assert extras["时间戳"] == "123"
    assert extras["EventType"] == "GROUP_MESSAGE_CREATE"
    assert extras["引用ID"] == "ref1"
    assert extras["是否艾特机器人"] == "1"
    assert extras["机器人身份"] == "admin"
    assert extras["消息来源"] == "src"


# ---- 成员缓存 ----

@pytest.fixture
def members(tmp_path, monkeypatch, main_mod):
    monkeypatch.setattr(main_mod, "_MEMBERS_FILE", tmp_path / "members.json")
    monkeypatch.setattr(main_mod, "_members_cache", {})
    monkeypatch.setattr(main_mod, "_members_dirty", False)
    monkeypatch.setattr(main_mod, "_members_last_save", 0.0)
    return main_mod


def test_members_record_and_get(members):
    ev = FakeEvent(group_id="g1", user_id="u1",
                   raw={"d": {"author": {"username": "nick", "member_role": "admin"}}})
    members._members_record(ev)
    rec = members._members_get("g1", "u1")
    assert rec["username"] == "nick"
    assert rec["member_role"] == "admin"
    assert rec["member_openid"] == "u1"
    assert "first_seen" in rec and "last_seen" in rec


def test_members_record_bot_role(members):
    ev = FakeEvent(group_id="g1", user_id="", raw={}, bot_member_role="owner")
    members._members_record(ev)
    assert members._members_get("g1", "__bot__")["member_role"] == "owner"


def test_members_record_ignores_non_group(members):
    members._members_record(FakeEvent(group_id="", user_id="u1"))
    assert members._members_cache == {}


def test_members_save_and_load_roundtrip(members):
    ev = FakeEvent(group_id="g1", user_id="u1",
                   raw={"d": {"author": {"username": "n"}}})
    members._members_record(ev)
    members._members_save(force=True)
    members._members_cache.clear()
    members._members_load()
    assert members._members_get("g1", "u1")["username"] == "n"


def test_members_get_missing_returns_none(members):
    assert members._members_get("g", "u") is None


# ---- _bot_role ----

async def test_bot_role_non_group_empty(main_mod):
    assert await main_mod._bot_role(FakeEvent(group_id="")) == ""


async def test_bot_role_from_mentions(main_mod):
    ev = FakeEvent(group_id="g1", appid="a", is_group=True, bot_member_role="admin")
    assert await main_mod._bot_role(ev) == "admin"


async def test_bot_role_from_persisted_cache(members, monkeypatch):
    monkeypatch.setattr(members, "_BOT_ROLE_CACHE", {})
    members._members_cache["g1"] = {"__bot__": {"member_role": "owner"}}
    ev = FakeEvent(group_id="g1", appid="a", is_group=True, bot_member_role="")
    assert await members._bot_role(ev, cache_only=True) == "owner"


# ---- 按钮解析 ----

def test_parse_buttons_types(main_mod):
    rows = main_mod._parse_buttons("链接;https://x|填入;/cmd|回调;data^下一行")
    assert rows == [
        [{"text": "链接", "link": "https://x"},
         {"text": "填入", "data": "/cmd", "type": 2},
         {"text": "回调", "data": "data", "type": 1}],
        [{"text": "下一行", "data": "下一行", "type": 1}],
    ]


def test_redis_actions(main_mod, monkeypatch):
    import asyncio

    class FakeRedis:
        def __init__(self):
            self.store = {}
            self.last_set = None

        async def get(self, key):
            return self.store.get(key)

        async def set(self, key, value, ex=None):
            self.store[key] = value
            self.last_set = (key, value, ex)

        async def delete(self, key):
            return 1 if self.store.pop(key, None) is not None else 0

        async def incr(self, key, amount=1):
            self.store[key] = int(self.store.get(key, 0)) + amount
            return self.store[key]

    fake = FakeRedis()
    monkeypatch.setattr(main_mod, "_redis_pool", lambda: fake)

    async def go():
        assert await main_mod._redis_get_action("k 默认") == "默认"
        await main_mod._redis_set_action("k 秒=60 值 带 空格")
        assert fake.last_set == ("k", "值 带 空格", 60)
        await main_mod._redis_set_action("k2 普通值")
        assert fake.last_set == ("k2", "普通值", None)
        assert await main_mod._redis_get_action("k") == "值 带 空格"
        assert await main_mod._redis_incr_action("n 5") == "5"
        assert await main_mod._redis_incr_action("n") == "6"
        assert await main_mod._redis_del_action("k") == "1"

    asyncio.run(go())


def test_mysql_actions(main_mod, monkeypatch):
    import asyncio

    class FakeMySQL:
        async def fetch_all(self, sql, params=None):
            assert sql == "SELECT 1 AS a"
            return [{"a": 1}]

        async def execute(self, sql, params=None):
            return 3

    monkeypatch.setattr(main_mod, "_mysql_pool", lambda: FakeMySQL())

    async def go():
        assert await main_mod._mysql_query_action("SELECT 1 AS a") == '[{"a": 1}]'
        assert await main_mod._mysql_exec_action("UPDATE t SET x=1") == "3"

    asyncio.run(go())


def test_module_status_json(main_mod, monkeypatch):
    monkeypatch.setattr(main_mod, "_get_module", lambda name: None)
    status = json.loads(main_mod._module_status_json())
    assert status["playwright"] is False
    assert status["datastore"]["mysql"] is False
    assert status["onebot_adapter"] is False


def test_split_viewport(main_mod):
    assert main_mod._split_viewport("800x600 https://x") == ((800, 600), "https://x")
    assert main_mod._split_viewport("1280*720 html <h1>hi</h1>") == ((1280, 720), "html <h1>hi</h1>")
    assert main_mod._split_viewport("https://x") == (None, "https://x")
    assert main_mod._split_viewport("你好 世界") == (None, "你好 世界")


def test_parse_buttons_plain_command(main_mod):
    rows = main_mod._parse_buttons("直接发送;>签到|空值普通;>")
    assert rows == [
        [{"text": "直接发送", "data": "签到", "type": 2, "enter": True},
         {"text": "空值普通", "data": "空值普通", "type": 2, "enter": True}],
    ]


def test_parse_buttons_small(main_mod):
    result = main_mod._parse_buttons("a;b", small=True)
    assert result["font_size"] == "small"
    assert result["rows"][0][0]["text"] == "a"


def test_parse_buttons_empty(main_mod):
    assert main_mod._parse_buttons("|^|") == []


# ---- 本地媒体路径 ----

def test_resolve_local_media(tmp_path, monkeypatch, main_mod):
    monkeypatch.setattr(main_mod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(main_mod, "BASE_DIR", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "pic.png").write_bytes(b"IMG")
    data, name = main_mod._resolve_local_media("pic.png")
    assert data == b"IMG" and name == "pic.png"
    # 绝对路径
    data2, _ = main_mod._resolve_local_media(str(tmp_path / "data" / "pic.png"))
    assert data2 == b"IMG"
    # 不存在
    assert main_mod._resolve_local_media("ghost.png") == (None, None)


# ---- 发送编排 ----

async def test_send_outputs_text_merged_with_buttons(main_mod):
    ev = FakeEvent()
    outputs = [
        {"type": "text", "content": "你好"},
        {"type": "buttons", "content": "确认;ok", "extra": ""},
    ]
    await main_mod.send_outputs(ev, outputs, md_mode=False)
    assert len(ev.replies) == 1
    _, content, kwargs = ev.replies[0]
    assert content == "你好"
    assert kwargs["buttons"]
    assert kwargs["msg_type"] == 2  # 带按钮必须走 markdown


async def test_send_outputs_plain_text_default_type(main_mod):
    ev = FakeEvent()
    await main_mod.send_outputs(ev, [{"type": "text", "content": "hi"}], md_mode=False)
    assert ev.replies[0][2]["msg_type"] is None


async def test_send_outputs_text_mode_forces_plain(main_mod):
    ev = FakeEvent()
    await main_mod.send_outputs(ev, [{"type": "text", "content": "hi"}],
                                md_mode=False, text_mode=True)
    assert ev.replies[0][2]["msg_type"] == 0


async def test_send_outputs_buttons_only_sends_placeholder(main_mod):
    ev = FakeEvent()
    await main_mod.send_outputs(ev, [{"type": "buttons", "content": "a;b", "extra": ""}],
                                md_mode=False)
    assert len(ev.replies) == 1
    assert ev.replies[0][1] == " "


async def test_send_ark_37(main_mod):
    ev = FakeEvent()
    await main_mod._send_ark(ev, "37|提示|标题|副标题|http://img|http://link")
    kind, ark_type, args = ev.replies[0]
    assert (kind, ark_type) == ("ark", 37)
    assert args == ("提示", "标题", "副标题", "http://img", "http://link")


async def test_send_ark_23_items(main_mod):
    ev = FakeEvent()
    await main_mod._send_ark(ev, "23|描述|提示|项目1;http://a|项目2")
    kind, ark_type, args = ev.replies[0]
    assert ark_type == 23
    desc, prompt, items = args
    assert (desc, prompt) == ("描述", "提示")
    assert items == [["项目1", "http://a"], ["项目2"]]


async def test_send_ark_invalid_type_replies_error(main_mod):
    ev = FakeEvent()
    await main_mod._send_ark(ev, "abc|x")
    assert "ark 类型无效" in ev.replies[0][1]
