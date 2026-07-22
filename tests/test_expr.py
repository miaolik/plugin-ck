"""算式与条件求值：calc_expr / _eval_arith_brackets / eval_condition。"""

import pytest

from ck_engine import _eval_arith_brackets, calc_expr, eval_condition


@pytest.mark.parametrize("expr,expected", [
    ("1+1", "2"),
    ("10-4", "6"),
    ("2*3", "6"),
    ("(1+2)*3", "9"),
    ("7%3", "1"),
    ("10/4", "2.5"),
    ("10/2", "5"),          # 整数结果去掉小数
    (" 3 + 4 ", "7"),       # 允许空格
])
def test_calc_expr_valid(expr, expected):
    assert calc_expr(expr) == expected


@pytest.mark.parametrize("expr", [
    "",
    "abc",
    "123",            # 无运算符
    "1/0",            # 除零
    "__import__('os')",
    "1+",             # 语法错误
])
def test_calc_expr_invalid_returns_none(expr):
    assert calc_expr(expr) is None


def test_eval_arith_brackets_replaces_only_valid():
    assert _eval_arith_brackets("金额=[10+5]元") == "金额=15元"
    # 非算式内容原样保留
    assert _eval_arith_brackets("[hello]") == "[hello]"
    # 多个算式
    assert _eval_arith_brackets("[1+1] 和 [2*2]") == "2 和 4"


@pytest.mark.parametrize("cond,expected", [
    ("1==1", True),
    ("1==2", False),
    ("1!=2", True),
    ("3>2", True),
    ("2>=2", True),
    ("1<2", True),
    ("2<=1", False),
    ("abc==abc", True),
    ("abc==def", False),
])
def test_eval_condition_operators(cond, expected):
    assert eval_condition(cond) is expected


def test_eval_condition_numeric_string_compare():
    # "10" > "9" 数字比较应为 True（若按字符串则为 False）
    assert eval_condition("10>9") is True


def test_eval_condition_bare_variable_substitution():
    local = {"i": "5"}
    assert eval_condition("i<=30", local) is True
    assert eval_condition("i>30", local) is False


def test_eval_condition_arith_in_operands():
    # calc_expr 会先把纯算术运算数求值再比较（[算式] 语法在此之前的展开阶段处理）
    assert eval_condition("1+1==2") is True
    assert eval_condition("2*3==5") is False


@pytest.mark.parametrize("cond,expected", [
    ("", False),
    ("0", False),
    ("false", False),
    ("False", False),
    ("假", False),
    ("否", False),
    ("null", False),
    ("NULL", False),
    ("1", True),
    ("任意文本", True),
])
def test_eval_condition_truthiness_without_operator(cond, expected):
    assert eval_condition(cond) is expected
