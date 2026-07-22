"""parse_dict_text / Block 的解析行为。"""

from ck_engine import Block, parse_dict_text


def test_single_block_trigger_and_lines():
    text = "签到\n签到成功\n$写 x y z$"
    blocks, init, errors = parse_dict_text(text, "a.txt")
    assert errors == []
    assert init == []
    assert len(blocks) == 1
    blk = blocks[0]
    assert blk.trigger == "签到"
    assert blk.lines == ["签到成功", "$写 x y z$"]
    assert blk.source == "a.txt"
    assert blk.lineno == 1
    assert blk.internal is False


def test_blank_line_separates_blocks():
    text = "aa\n1\n\nbb\n2\n"
    blocks, _, _ = parse_dict_text(text, "s")
    assert [b.trigger for b in blocks] == ["aa", "bb"]
    assert blocks[0].lines == ["1"]
    assert blocks[1].lines == ["2"]


def test_comment_lines_are_dropped():
    text = "cmd\n// a comment\n## another\n&& third\nreal line"
    blocks, _, _ = parse_dict_text(text, "s")
    assert blocks[0].lines == ["real line"]


def test_init_block_goes_to_init_lines():
    text = "#INITROBOT#\n$全局写 a 1$\n$全局写 b 2$"
    blocks, init, _ = parse_dict_text(text, "s")
    assert blocks == []
    assert init == ["$全局写 a 1$", "$全局写 b 2$"]


def test_internal_prefix_marks_block_internal():
    for prefix in ("[内部]", "#内部#"):
        blocks, _, _ = parse_dict_text(f"{prefix}子程序\nbody", "s")
        assert blocks[0].internal is True
        assert blocks[0].trigger == "子程序"


def test_invalid_regex_trigger_records_error_and_none_pattern():
    blocks, _, errors = parse_dict_text("(未闭合\nbody", "bad.txt")
    assert blocks[0].pattern is None
    assert len(errors) == 1
    assert "正则无效" in errors[0]


def test_js_block_is_skipped_with_error():
    text = "#JAVASCRIPTSTART#\nvar a=1;\n#JAVASCRIPTEND#\ncmd\nout"
    blocks, _, errors = parse_dict_text(text, "s")
    assert [b.trigger for b in blocks] == ["cmd"]
    assert any("不支持 JS" in e for e in errors)


def test_block_pattern_fullmatch():
    blk = Block(r"数字(\d+)", ["x"], False, "s", 1)
    assert blk.pattern.fullmatch("数字123")
    assert blk.pattern.fullmatch("前缀数字123") is None
